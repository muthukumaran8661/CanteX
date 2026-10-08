"""
services/order_service.py
=========================
Core handwritten application logic for:
- Concurrency-safe atomic order creation with row-level stock locks
- Station-wise task allocation and dynamic scheduling
- Safe order cancellation with automatic inventory restoration & refunds
- Order editing with atomic inventory diff and +5 minute preparation penalty
- Deterministic queue re-ordering by current estimated ready time
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

from config import (
    EDIT_EXTRA_MINUTES,
    STATUS_WAITING,
    STATUS_PREPARING,
    STATUS_PARTIALLY_READY,
    STATUS_READY,
    STATUS_COMPLETED,
    STATUS_SERVED,
    STATUS_CANCELLED,
    TOKEN_PREFIX,
    TOKEN_PADDING,
    NEXT_TASK_STATUS,
)
from database import (
    get_db_connection,
    _DB_LOCK,
    OutOfStockError,
    InvalidOrderStateError,
)
from core.scheduler import (
    sort_station_tasks,
    compute_initial_task_eta,
    apply_edit_penalty_to_eta,
    format_iso,
    parse_iso,
)
from services.payment_adapter import PaymentAdapter
from services.event_broker import EventBroker


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrderService:
    """Production-grade order orchestration service."""

    @staticmethod
    def get_menu() -> List[Dict[str, Any]]:
        """Return all active menu items with live stock and station info."""
        with _DB_LOCK:
            conn = get_db_connection()
            try:
                cur = conn.execute("""
                    SELECT m.id, m.name, m.price, m.category, m.station_code,
                           s.name as station_name, s.icon as station_icon,
                           m.prep_time_minutes, m.stock, m.initial_stock,
                           m.image, m.badge, m.description
                    FROM menu_items m
                    LEFT JOIN stations s ON m.station_code = s.code
                    WHERE m.active = 1
                    ORDER BY m.id ASC
                """)
                rows = [dict(r) for r in cur.fetchall()]
                return rows
            finally:
                conn.close()

    @staticmethod
    def create_order(
        items: List[Dict[str, Any]],
        student_name: str = "Student",
        payment_method: str = "UPI",
        student_email: str = "",
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Atomically creates a new order:
        1. Atomic inventory verification & stock decrement (guarantees no race conditions).
        2. Payment authorization via PaymentAdapter.
        3. Deterministic unique token sequence generation.
        4. Multi-station task distribution and dynamic ETA scheduling.
        5. Emits real-time event.
        """
        if not items:
            raise ValueError("Cannot place an order with an empty tray.")

        with _DB_LOCK:
            conn = get_db_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")

                # Step 1: Verify and lock stock atomically
                order_item_records = []
                total_amount = 0.0

                for item in items:
                    item_id = item.get("id") or item.get("menu_item_id")
                    item_name = item.get("name")
                    qty = int(item.get("qty", 1))
                    if qty <= 0:
                        continue

                    # Find menu item row
                    if item_id:
                        cur = conn.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,))
                    else:
                        cur = conn.execute("SELECT * FROM menu_items WHERE LOWER(name) = LOWER(?)", (item_name,))
                    menu_row = cur.fetchone()

                    if not menu_row:
                        raise ValueError(f"Food item '{item_name or item_id}' not found.")

                    actual_id = menu_row["id"]
                    actual_name = menu_row["name"]
                    unit_price = float(menu_row["price"])
                    station_code = menu_row["station_code"]
                    prep_time = int(menu_row["prep_time_minutes"])

                    # Atomic stock decrement check:
                    # UPDATE menu_items SET stock = stock - ? WHERE id = ? AND stock >= ?
                    upd = conn.execute("""
                        UPDATE menu_items
                        SET stock = stock - ?
                        WHERE id = ? AND stock >= ?
                    """, (qty, actual_id, qty))

                    if upd.rowcount == 0:
                        cur_stock = conn.execute("SELECT stock FROM menu_items WHERE id = ?", (actual_id,)).fetchone()
                        rem = cur_stock["stock"] if cur_stock else 0
                        raise OutOfStockError(
                            f"Sorry, '{actual_name}' is sold out! (Requested: {qty}, Available: {rem})"
                        )

                    line_total = unit_price * qty
                    total_amount += line_total

                    order_item_records.append({
                        "menu_item_id": actual_id,
                        "name": actual_name,
                        "qty": qty,
                        "price": unit_price,
                        "station_code": station_code,
                        "prep_time_minutes": prep_time,
                    })

                    # Log inventory decrement
                    conn.execute("""
                        INSERT INTO inventory_logs (menu_item_id, delta, reason, created_at)
                        VALUES (?, ?, 'ORDER_RESERVATION', datetime('now'))
                    """, (actual_id, -qty))

                # Step 2: Payment authorization
                ref_id = f"REF-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
                pay_res = PaymentAdapter.charge(total_amount, method=payment_method, ref=ref_id)
                if not pay_res.get("success"):
                    # Payment failed -> Rollback immediately, stock is not consumed!
                    conn.execute("ROLLBACK")
                    return {
                        "success": False,
                        "message": pay_res.get("message", "Payment declined."),
                        "state": pay_res.get("state", "DECLINED"),
                    }

                txn_id = pay_res.get("transaction_id", "")

                # Step 3: Generate deterministic token
                seq_row = conn.execute("SELECT next_seq FROM token_sequence WHERE id = 1").fetchone()
                seq_num = seq_row["next_seq"] if seq_row else 1
                conn.execute("UPDATE token_sequence SET next_seq = next_seq + 1 WHERE id = 1")

                token_str = f"{TOKEN_PREFIX}{str(seq_num).zfill(TOKEN_PADDING)}"
                now_str = _now_iso()

                # Step 4: Insert Order record
                ins_ord = conn.execute("""
                    INSERT INTO orders (
                        token, student_name, student_email, user_id, status, total,
                        payment_method, transaction_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    token_str, student_name, student_email or "", user_id, STATUS_WAITING, total_amount,
                    payment_method, txn_id, now_str, now_str
                ))
                order_id = ins_ord.lastrowid

                # Step 5: Insert Order Items
                for o_it in order_item_records:
                    conn.execute("""
                        INSERT INTO order_items (
                            order_id, menu_item_id, item_name, quantity, price, station_code
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        order_id, o_it["menu_item_id"], o_it["name"],
                        o_it["qty"], o_it["price"], o_it["station_code"]
                    ))

                # Step 6: Multi-Station Task Distribution & Dynamic ETA Scheduling
                # Group items by station
                station_groups: Dict[str, List[Dict[str, Any]]] = {}
                for o_it in order_item_records:
                    st = o_it["station_code"]
                    station_groups.setdefault(st, []).append(o_it)

                tasks_created = []
                for st_code, st_items in station_groups.items():
                    # Calculate max prep time for this station's items
                    max_prep = max(it["prep_time_minutes"] for it in st_items)
                    summary = ", ".join(f"{it['name']} ×{it['qty']}" for it in st_items)

                    # Fetch active tasks in this station to schedule sequentially
                    existing_cur = conn.execute("""
                        SELECT * FROM station_tasks
                        WHERE station_code = ? AND status IN (?, ?)
                    """, (st_code, STATUS_WAITING, STATUS_PREPARING))
                    existing_tasks = [dict(r) for r in existing_cur.fetchall()]

                    eta_dt = compute_initial_task_eta(
                        base_time=datetime.now(timezone.utc),
                        prep_time_minutes=max_prep,
                        existing_station_tasks=existing_tasks,
                    )
                    eta_str = format_iso(eta_dt)

                    ins_task = conn.execute("""
                        INSERT INTO station_tasks (
                            order_id, token, station_code, status,
                            estimated_ready_time, items_summary, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        order_id, token_str, st_code, STATUS_WAITING,
                        eta_str, summary, now_str, now_str
                    ))
                    tasks_created.append({
                        "task_id": ins_task.lastrowid,
                        "station_code": st_code,
                        "summary": summary,
                        "eta": eta_str,
                    })

                conn.execute("COMMIT")

                # Step 7: Broadcast real-time live events
                order_payload = {
                    "token": token_str,
                    "order_id": order_id,
                    "total": total_amount,
                    "status": STATUS_WAITING,
                    "transaction_id": txn_id,
                    "tasks": tasks_created,
                }
                EventBroker.publish("order_created", order_payload)
                EventBroker.publish("stock_updated", {"items": order_item_records})

                return {
                    "success": True,
                    "token": token_str,
                    "order_id": order_id,
                    "total": total_amount,
                    "transaction_id": txn_id,
                    "tasks": tasks_created,
                    "message": f"Order {token_str} placed successfully!",
                }

            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @staticmethod
    def cancel_order(token: str) -> Dict[str, Any]:
        """
        Cancel an active order:
        - Allowed ONLY when order status is WAITING or PREPARING.
        - Rejects cancellation if READY or COMPLETED.
        - Restores inventory.
        - Issues refund via locked payment_stub.
        - Removes tasks from active queues and re-sorts stations.
        """
        with _DB_LOCK:
            conn = get_db_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")

                ord_cur = conn.execute("SELECT * FROM orders WHERE token = ?", (token,))
                order = ord_cur.fetchone()
                if not order:
                    raise ValueError(f"Order '{token}' does not exist.")

                current_status = order["status"]

                # Enforce backend cancellation rule:
                if current_status in {STATUS_READY, STATUS_COMPLETED, STATUS_SERVED}:
                    raise InvalidOrderStateError(
                        f"Cannot cancel order {token}. Food is already {current_status}."
                    )
                if current_status == STATUS_CANCELLED:
                    raise InvalidOrderStateError(f"Order {token} is already cancelled.")

                # Mark order as cancelled
                now_str = _now_iso()
                conn.execute("""
                    UPDATE orders
                    SET status = ?, updated_at = ?
                    WHERE token = ?
                """, (STATUS_CANCELLED, now_str, token))

                # Mark all station tasks as cancelled
                conn.execute("""
                    UPDATE station_tasks
                    SET status = ?, updated_at = ?
                    WHERE order_id = ? AND status NOT IN (?, ?)
                """, (STATUS_CANCELLED, now_str, order["id"], STATUS_COMPLETED, STATUS_SERVED))

                # Restore inventory
                items_cur = conn.execute("""
                    SELECT menu_item_id, quantity FROM order_items WHERE order_id = ?
                """, (order["id"],))
                for it_row in items_cur.fetchall():
                    conn.execute("""
                        UPDATE menu_items
                        SET stock = stock + ?
                        WHERE id = ?
                    """, (it_row["quantity"], it_row["menu_item_id"]))
                    conn.execute("""
                        INSERT INTO inventory_logs (menu_item_id, delta, reason, order_token, created_at)
                        VALUES (?, ?, 'ORDER_CANCELLED_RESTORE', ?, datetime('now'))
                    """, (it_row["menu_item_id"], it_row["quantity"], token))

                # Issue refund through payment adapter
                refund_res = PaymentAdapter.refund(
                    order["transaction_id"],
                    reason=f"CANCEL_ORDER_{token}",
                )

                conn.execute("COMMIT")

                # Broadcast live updates
                EventBroker.publish("order_cancelled", {
                    "token": token,
                    "refund": refund_res,
                })
                EventBroker.publish("queue_reordered", {"reason": f"Cancellation of {token}"})

                return {
                    "success": True,
                    "token": token,
                    "status": STATUS_CANCELLED,
                    "refund": refund_res,
                    "message": f"Order {token} has been cancelled and refunded successfully.",
                }

            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @staticmethod
    def edit_order(token: str, new_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Edit an active order before it is READY / COMPLETED:
        1. Validates allowed state (WAITING or PREPARING).
        2. Calculates atomic inventory differential (reserves added, restores removed).
        3. Recalculates preparation time and adds EDIT_EXTRA_MINUTES penalty (+5 mins).
        4. Updates station tasks and re-sorts affected station queues.
        5. Preserves customer order and token identity.
        """
        if not new_items:
            raise ValueError("Edited order cannot be empty. Use cancellation instead.")

        with _DB_LOCK:
            conn = get_db_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")

                ord_cur = conn.execute("SELECT * FROM orders WHERE token = ?", (token,))
                order = ord_cur.fetchone()
                if not order:
                    raise ValueError(f"Order '{token}' does not exist.")

                current_status = order["status"]

                # Enforce backend edit cutoff rule:
                if current_status in {STATUS_READY, STATUS_COMPLETED, STATUS_SERVED}:
                    raise InvalidOrderStateError(
                        f"Cannot edit order {token}. Food is already {current_status}."
                    )
                if current_status == STATUS_CANCELLED:
                    raise InvalidOrderStateError(f"Cannot edit cancelled order {token}.")

                order_id = order["id"]

                # Old items mapping: item_id -> old_qty
                old_items_cur = conn.execute("""
                    SELECT menu_item_id, quantity FROM order_items WHERE order_id = ?
                """, (order_id,))
                old_qtys: Dict[int, int] = {}
                for r in old_items_cur.fetchall():
                    old_qtys[r["menu_item_id"]] = old_qtys.get(r["menu_item_id"], 0) + r["quantity"]

                # New items aggregated: item_id -> new_qty
                new_qtys: Dict[int, int] = {}
                for it in new_items:
                    it_id = it.get("id") or it.get("menu_item_id")
                    it_qty = int(it.get("qty", 1))
                    if it_qty <= 0:
                        continue
                    new_qtys[it_id] = new_qtys.get(it_id, 0) + it_qty

                # Atomic inventory differential check & apply
                all_involved_item_ids = set(old_qtys.keys()).union(set(new_qtys.keys()))
                for m_id in all_involved_item_ids:
                    old_q = old_qtys.get(m_id, 0)
                    new_q = new_qtys.get(m_id, 0)
                    delta = new_q - old_q

                    if delta > 0:
                        # Need to reserve MORE inventory atomically
                        upd = conn.execute("""
                            UPDATE menu_items
                            SET stock = stock - ?
                            WHERE id = ? AND stock >= ?
                        """, (delta, m_id, delta))
                        if upd.rowcount == 0:
                            item_name_cur = conn.execute("SELECT name, stock FROM menu_items WHERE id = ?", (m_id,)).fetchone()
                            name_str = item_name_cur["name"] if item_name_cur else f"Item #{m_id}"
                            avail = item_name_cur["stock"] if item_name_cur else 0
                            raise OutOfStockError(
                                f"Cannot add '{name_str}'. Insufficient stock (Requested: +{delta}, Available: {avail})."
                            )
                        conn.execute("""
                            INSERT INTO inventory_logs (menu_item_id, delta, reason, order_token, created_at)
                            VALUES (?, ?, 'EDIT_ADD_RESERVATION', ?, datetime('now'))
                        """, (m_id, -delta, token))

                    elif delta < 0:
                        # Release inventory back to kitchen
                        restore_q = abs(delta)
                        conn.execute("""
                            UPDATE menu_items
                            SET stock = stock + ?
                            WHERE id = ?
                        """, (restore_q, m_id))
                        conn.execute("""
                            INSERT INTO inventory_logs (menu_item_id, delta, reason, order_token, created_at)
                            VALUES (?, ?, 'EDIT_REMOVE_RESTORE', ?, datetime('now'))
                        """, (m_id, restore_q, token))

                # Replace order_items in DB
                conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))

                new_order_item_records = []
                new_total = 0.0

                for it_id, it_qty in new_qtys.items():
                    m_row = conn.execute("SELECT * FROM menu_items WHERE id = ?", (it_id,)).fetchone()
                    if not m_row:
                        continue
                    price = float(m_row["price"])
                    new_total += price * it_qty
                    st_code = m_row["station_code"]
                    conn.execute("""
                        INSERT INTO order_items (
                            order_id, menu_item_id, item_name, quantity, price, station_code
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """, (order_id, it_id, m_row["name"], it_qty, price, st_code))
                    new_order_item_records.append({
                        "menu_item_id": it_id,
                        "name": m_row["name"],
                        "qty": it_qty,
                        "price": price,
                        "station_code": st_code,
                        "prep_time_minutes": int(m_row["prep_time_minutes"]),
                    })

                # Update order total and timestamp
                now_str = _now_iso()
                conn.execute("""
                    UPDATE orders
                    SET total = ?, updated_at = ?
                    WHERE id = ?
                """, (new_total, now_str, order_id))

                # Update Station Tasks and Add EDIT_EXTRA_MINUTES Penalty
                station_groups: Dict[str, List[Dict[str, Any]]] = {}
                for o_it in new_order_item_records:
                    station_groups.setdefault(o_it["station_code"], []).append(o_it)

                # Fetch current station tasks for this order
                cur_tasks = conn.execute("""
                    SELECT * FROM station_tasks WHERE order_id = ?
                """, (order_id,)).fetchall()
                cur_task_map = {r["station_code"]: dict(r) for r in cur_tasks}

                updated_tasks = []

                for st_code, st_items in station_groups.items():
                    summary = ", ".join(f"{it['name']} ×{it['qty']}" for it in st_items)
                    max_prep = max(it["prep_time_minutes"] for it in st_items)

                    if st_code in cur_task_map:
                        # Existing task for station: add +5 minutes edit penalty!
                        old_task = cur_task_map[st_code]
                        new_eta = apply_edit_penalty_to_eta(
                            old_task["estimated_ready_time"],
                            extra_minutes=EDIT_EXTRA_MINUTES,
                        )
                        conn.execute("""
                            UPDATE station_tasks
                            SET estimated_ready_time = ?, items_summary = ?, updated_at = ?
                            WHERE id = ?
                        """, (new_eta, summary, now_str, old_task["id"]))
                        updated_tasks.append({
                            "station_code": st_code,
                            "summary": summary,
                            "eta": new_eta,
                            "penalty_applied": EDIT_EXTRA_MINUTES,
                        })
                    else:
                        # New station added during edit
                        existing_st_tasks = [
                            dict(r) for r in conn.execute(
                                "SELECT * FROM station_tasks WHERE station_code = ? AND status IN (?, ?)",
                                (st_code, STATUS_WAITING, STATUS_PREPARING)
                            ).fetchall()
                        ]
                        initial_eta = compute_initial_task_eta(
                            base_time=datetime.now(timezone.utc),
                            prep_time_minutes=max_prep,
                            existing_station_tasks=existing_st_tasks,
                        )
                        penalized_eta = initial_eta + timedelta(minutes=EDIT_EXTRA_MINUTES)
                        penalized_eta_str = format_iso(penalized_eta)

                        conn.execute("""
                            INSERT INTO station_tasks (
                                order_id, token, station_code, status,
                                estimated_ready_time, items_summary, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (order_id, token, st_code, STATUS_WAITING, penalized_eta_str, summary, now_str, now_str))
                        updated_tasks.append({
                            "station_code": st_code,
                            "summary": summary,
                            "eta": penalized_eta_str,
                            "penalty_applied": EDIT_EXTRA_MINUTES,
                        })

                # Cancel tasks for stations no longer in edited order
                for st_code, old_task in cur_task_map.items():
                    if st_code not in station_groups:
                        conn.execute("""
                            UPDATE station_tasks
                            SET status = ?, updated_at = ?
                            WHERE id = ?
                        """, (STATUS_CANCELLED, now_str, old_task["id"]))

                conn.execute("COMMIT")

                # Broadcast live updates
                EventBroker.publish("order_edited", {
                    "token": token,
                    "total": new_total,
                    "updated_tasks": updated_tasks,
                    "edit_penalty_minutes": EDIT_EXTRA_MINUTES,
                })
                EventBroker.publish("queue_reordered", {
                    "reason": f"Edit penalty added to {token} (+{EDIT_EXTRA_MINUTES} mins)",
                })

                return {
                    "success": True,
                    "token": token,
                    "total": new_total,
                    "tasks": updated_tasks,
                    "message": f"Order {token} updated successfully! (+{EDIT_EXTRA_MINUTES} mins prep time added)",
                }

            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @staticmethod
    def advance_station_task(task_id: int) -> Dict[str, Any]:
        """
        Move a station task to the next state:
        Waiting -> Preparing -> Ready -> Completed
        Updates the overall order status dynamically.
        """
        with _DB_LOCK:
            conn = get_db_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")

                t_cur = conn.execute("SELECT * FROM station_tasks WHERE id = ?", (task_id,))
                task = t_cur.fetchone()
                if not task:
                    raise ValueError(f"Station task #{task_id} not found.")

                curr_status = task["status"]
                if curr_status not in NEXT_TASK_STATUS:
                    raise InvalidOrderStateError(
                        f"Cannot advance station task from status '{curr_status}'."
                    )

                new_status = NEXT_TASK_STATUS[curr_status]
                now_str = _now_iso()
                actual_ready = now_str if new_status == STATUS_READY else task["actual_ready_time"]

                conn.execute("""
                    UPDATE station_tasks
                    SET status = ?, actual_ready_time = ?, updated_at = ?
                    WHERE id = ?
                """, (new_status, actual_ready, now_str, task_id))

                order_id = task["order_id"]
                token = task["token"]

                # Recalculate Overall Order Status across all its station tasks
                all_tasks = [
                    dict(r) for r in conn.execute(
                        "SELECT status FROM station_tasks WHERE order_id = ?", (order_id,)
                    ).fetchall()
                ]

                statuses = [t["status"] for t in all_tasks]
                active_statuses = [s for s in statuses if s != STATUS_CANCELLED]

                if not active_statuses or all(s == STATUS_CANCELLED for s in statuses):
                    order_status = STATUS_CANCELLED
                elif all(s in {STATUS_COMPLETED, STATUS_SERVED} for s in active_statuses):
                    order_status = STATUS_COMPLETED
                elif all(s == STATUS_READY for s in active_statuses):
                    order_status = STATUS_READY
                elif any(s == STATUS_READY for s in active_statuses):
                    order_status = STATUS_PARTIALLY_READY
                elif any(s == STATUS_PREPARING for s in active_statuses):
                    order_status = STATUS_PREPARING
                else:
                    order_status = STATUS_WAITING

                conn.execute("""
                    UPDATE orders
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                """, (order_status, now_str, order_id))

                conn.execute("COMMIT")

                EventBroker.publish("task_advanced", {
                    "task_id": task_id,
                    "token": token,
                    "station_code": task["station_code"],
                    "new_task_status": new_status,
                    "overall_order_status": order_status,
                })

                return {
                    "success": True,
                    "task_id": task_id,
                    "token": token,
                    "new_task_status": new_status,
                    "overall_order_status": order_status,
                    "message": f"Task #{task_id} ({task['station_code']}) moved to {new_status}.",
                }

            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @staticmethod
    def advance_order_by_token(token: str) -> Dict[str, Any]:
        """
        Backward-compatible advance: advances the first pending task of this token.
        """
        with _DB_LOCK:
            conn = get_db_connection()
            try:
                tasks = conn.execute("""
                    SELECT id, status FROM station_tasks
                    WHERE token = ? AND status IN (?, ?)
                    ORDER BY id ASC
                """, (token, STATUS_WAITING, STATUS_PREPARING)).fetchall()

                if tasks:
                    return OrderService.advance_station_task(tasks[0]["id"])

                # If all ready, advance first Ready task to Completed
                ready_tasks = conn.execute("""
                    SELECT id FROM station_tasks WHERE token = ? AND status = ?
                """, (token, STATUS_READY)).fetchall()
                if ready_tasks:
                    return OrderService.advance_station_task(ready_tasks[0]["id"])

                raise InvalidOrderStateError(f"No advanceable tasks found for order {token}.")
            finally:
                conn.close()

    @staticmethod
    def get_station_queues() -> Dict[str, List[Dict[str, Any]]]:
        """
        Return the dynamically sorted queue for EACH station:
        - Sorted by: estimated_ready_time ASC, created_at ASC, id ASC
        - Excludes Cancelled and Completed tasks
        """
        with _DB_LOCK:
            conn = get_db_connection()
            try:
                stations_cur = conn.execute("SELECT code, name, icon FROM stations WHERE active = 1 ORDER BY display_order ASC")
                stations = [dict(s) for s in stations_cur.fetchall()]

                result = {}
                for st in stations:
                    st_code = st["code"]
                    tasks_cur = conn.execute("""
                        SELECT t.id, t.order_id, t.token, t.station_code, t.status,
                               t.estimated_ready_time, t.actual_ready_time,
                               t.items_summary, t.created_at,
                               o.student_name, o.total
                        FROM station_tasks t
                        JOIN orders o ON t.order_id = o.id
                        WHERE t.station_code = ? AND t.status IN (?, ?)
                    """, (st_code, STATUS_WAITING, STATUS_PREPARING))
                    raw_tasks = [dict(r) for r in tasks_cur.fetchall()]
                    sorted_tasks = sort_station_tasks(raw_tasks)

                    # Add 1-based dynamic station queue position
                    for idx, t in enumerate(sorted_tasks, start=1):
                        t["station_position"] = idx

                    result[st_code] = sorted_tasks

                return result
            finally:
                conn.close()

    @staticmethod
    def get_live_board() -> Dict[str, Any]:
        """
        Return live board state for each station:
        - Now Preparing (first task in PREPARING or first active task)
        - Next (upcoming task)
        - Ready (tasks waiting for student pickup)
        """
        queues = OrderService.get_station_queues()
        board = {}

        with _DB_LOCK:
            conn = get_db_connection()
            try:
                for st_code, active_tasks in queues.items():
                    # Check ready tasks
                    ready_cur = conn.execute("""
                        SELECT token, items_summary, actual_ready_time
                        FROM station_tasks
                        WHERE station_code = ? AND status = ?
                        ORDER BY actual_ready_time DESC
                        LIMIT 5
                    """, (st_code, STATUS_READY))
                    ready_list = [dict(r) for r in ready_cur.fetchall()]

                    preparing_task = next(
                        (t for t in active_tasks if t["status"] == STATUS_PREPARING),
                        active_tasks[0] if active_tasks else None
                    )

                    next_task = None
                    if preparing_task and len(active_tasks) > 1:
                        next_task = next((t for t in active_tasks if t != preparing_task), None)

                    board[st_code] = {
                        "now_preparing": preparing_task["token"] if preparing_task else "--",
                        "now_preparing_item": preparing_task["items_summary"] if preparing_task else "",
                        "next": next_task["token"] if next_task else "--",
                        "next_item": next_task["items_summary"] if next_task else "",
                        "ready_tokens": [r["token"] for r in ready_list],
                        "active_count": len(active_tasks),
                    }
                return board
            finally:
                conn.close()

    @staticmethod
    def get_order_tracking(token: str) -> Dict[str, Any]:
        """
        Detailed multi-station tracking information for a specific order token.
        """
        with _DB_LOCK:
            conn = get_db_connection()
            try:
                ord_cur = conn.execute("SELECT * FROM orders WHERE token = ?", (token,))
                order = ord_cur.fetchone()
                if not order:
                    return {}

                tasks_cur = conn.execute("""
                    SELECT t.id, t.station_code, s.name as station_name, s.icon as station_icon,
                           t.status, t.estimated_ready_time, t.actual_ready_time,
                           t.items_summary, t.created_at
                    FROM station_tasks t
                    LEFT JOIN stations s ON t.station_code = s.code
                    WHERE t.order_id = ?
                    ORDER BY t.id ASC
                """, (order["id"],))
                tasks = [dict(r) for r in tasks_cur.fetchall()]

                # Calculate station queue position for each task
                all_queues = OrderService.get_station_queues()
                for t in tasks:
                    st_q = all_queues.get(t["station_code"], [])
                    pos = next((idx for idx, q_t in enumerate(st_q, 1) if q_t["id"] == t["id"]), -1)
                    t["queue_position"] = pos
                    t["ahead_count"] = max(0, pos - 1) if pos > 0 else 0

                items_cur = conn.execute("""
                    SELECT item_name as name, quantity as qty, price, station_code
                    FROM order_items WHERE order_id = ?
                """, (order["id"],))
                items = [dict(r) for r in items_cur.fetchall()]

                # Determine if order is editable / cancellable
                can_modify = order["status"] in {STATUS_WAITING, STATUS_PREPARING}

                return {
                    "token": order["token"],
                    "order_id": order["id"],
                    "status": order["status"],
                    "total": order["total"],
                    "created_at": order["created_at"],
                    "transaction_id": order["transaction_id"],
                    "items": items,
                    "station_tasks": tasks,
                    "can_cancel": can_modify,
                    "can_edit": can_modify,
                }
            finally:
                conn.close()

    @staticmethod
    def get_all_orders() -> List[Dict[str, Any]]:
        """Return all historical orders enriched with item details and station tasks."""
        with _DB_LOCK:
            conn = get_db_connection()
            try:
                cur = conn.execute("SELECT * FROM orders ORDER BY id DESC")
                orders = [dict(r) for r in cur.fetchall()]
                for o in orders:
                    # Fetch items for this order
                    i_cur = conn.execute("""
                        SELECT item_name, quantity, price, station_code
                        FROM order_items
                        WHERE order_id = ?
                    """, (o["id"],))
                    items = [dict(r) for r in i_cur.fetchall()]
                    o["items"] = items
                    o["items_summary"] = ", ".join(f"{it['item_name']} x{it['quantity']}" for it in items)

                    # Fetch tasks for this order
                    t_cur = conn.execute("""
                        SELECT id, station_code, status, estimated_ready_time, actual_ready_time
                        FROM station_tasks
                        WHERE order_id = ?
                        ORDER BY id ASC
                    """, (o["id"],))
                    tasks = [dict(r) for r in t_cur.fetchall()]
                    o["station_tasks"] = tasks
                    o["stations"] = list({t["station_code"] for t in tasks})

                    # Max estimated ready time
                    etas = [t["estimated_ready_time"] for t in tasks if t.get("estimated_ready_time")]
                    o["estimated_ready_time"] = max(etas) if etas else o.get("created_at")

                return orders
            finally:
                conn.close()
