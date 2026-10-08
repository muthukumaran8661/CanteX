"""
tests/test_smart_canteen.py
===========================
Comprehensive automated test suite covering all 17 requirements:
1. Normal order.
2. Token uniqueness.
3. Last-item concurrent ordering (multithreaded race test).
4. No negative stock.
5. Station separation.
6. Parallel station processing.
7. Cancellation removes queue task.
8. Cancellation allows next task to proceed.
9. Editing an active order.
10. Edit adds extra preparation time (+5 min penalty).
11. Queue reorders after edit.
12. Token number remains unchanged after edit.
13. Editing unavailable item fails safely.
14. Editing after completion is rejected.
15. Cancellation after completion is rejected.
16. Multi-station order creates multiple station tasks.
17. Live update event is emitted after order mutation.
"""

import threading
import time
import pytest
from datetime import datetime, timezone, timedelta

# Import project modules
from config import (
    STATION_MAIN_DISH,
    STATION_SNACKS,
    STATION_BEVERAGES,
    STATUS_WAITING,
    STATUS_PREPARING,
    STATUS_READY,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
    EDIT_EXTRA_MINUTES,
)
from database import (
    init_db,
    reset_menu_item_stock,
    get_db_connection,
    OutOfStockError,
    InvalidOrderStateError,
    _DB_LOCK,
)
from services.order_service import OrderService
from services.event_broker import EventBroker
from core.scheduler import parse_iso


@pytest.fixture(autouse=True)
def clean_test_database():
    """Ensure database tables are cleaned and stock restored before each test."""
    init_db()
    with _DB_LOCK:
        conn = get_db_connection()
        conn.execute("DELETE FROM station_tasks")
        conn.execute("DELETE FROM order_items")
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM inventory_logs")
        conn.execute("UPDATE menu_items SET stock = initial_stock")
        conn.close()


def place_test_order(items, student_name="Student", payment_method="UPI"):
    """Helper to place an order in tests with automatic retry for transient gateway declines."""
    for _ in range(5):
        res = OrderService.create_order(items, student_name=student_name, payment_method=payment_method)
        if res.get("success"):
            return res
        time.sleep(0.01)
    return res


# ── Test 1: Normal Order ──────────────────────────────────────────────────
def test_01_normal_order():
    """Test standard order placement with successful token generation."""
    items = [{"name": "Idli", "qty": 2}]
    res = place_test_order(items, student_name="Alice", payment_method="UPI")
    assert res["success"] is True
    assert "token" in res
    assert res["token"].startswith("T-")
    assert res["total"] > 0
    assert len(res["tasks"]) >= 1


# ── Test 2: Token Uniqueness ──────────────────────────────────────────────
def test_02_token_uniqueness():
    """Verify that multiple consecutive orders receive distinct, unique tokens."""
    tokens = set()
    for i in range(10):
        res = place_test_order([{"name": "Tea", "qty": 1}], student_name=f"Student {i}")
        assert res["success"] is True
        token = res["token"]
        assert token not in tokens, f"Duplicate token detected: {token}"
        tokens.add(token)
    assert len(tokens) == 10


# ── Test 3 & 4: Last-item concurrent ordering & No negative stock ─────────
def test_03_and_04_last_item_race_and_no_negative_stock():
    """
    CRITICAL ROUND 2 SCENARIO:
    Burger stock = 1.
    Two students attempt to buy simultaneously in separate threads.
    Expected: Exactly one succeeds, one fails with OutOfStockError.
    Inventory must never become negative!
    """
    conn = get_db_connection()
    burger = conn.execute("SELECT id FROM menu_items WHERE name = 'Burger'").fetchone()
    burger_id = burger["id"]
    conn.close()

    # Set Burger stock strictly to 1
    reset_menu_item_stock(burger_id, 1)

    results = []
    errors = []

    def attempt_purchase(student_name):
        try:
            res = OrderService.create_order(
                [{"id": burger_id, "name": "Burger", "qty": 1}],
                student_name=student_name,
                payment_method="UPI",
            )
            if res.get("success"):
                results.append((student_name, res))
            else:
                errors.append((student_name, res.get("message")))
        except OutOfStockError as ose:
            errors.append((student_name, str(ose)))
        except Exception as e:
            errors.append((student_name, str(e)))

    t1 = threading.Thread(target=attempt_purchase, args=("Student A",))
    t2 = threading.Thread(target=attempt_purchase, args=("Student B",))

    # Launch threads at almost the exact same instant
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Assert exactly ONE succeeded and ONE failed
    assert len(results) == 1, f"Expected exactly 1 success, got {len(results)}"
    assert len(errors) == 1, f"Expected exactly 1 sold out failure, got {len(errors)}"

    # Check remaining stock in database: must be exactly 0, never negative!
    conn = get_db_connection()
    final_stock = conn.execute("SELECT stock FROM menu_items WHERE id = ?", (burger_id,)).fetchone()["stock"]
    conn.close()
    assert final_stock == 0, f"Stock should be 0, but is {final_stock}"


# ── Test 5: Station Separation ────────────────────────────────────────────
def test_05_station_separation():
    """Verify that items from different categories map to distinct stations."""
    menu = OrderService.get_menu()
    item_map = {it["name"]: it["station_code"] for it in menu}

    assert item_map.get("Chicken Biriyani") == STATION_MAIN_DISH
    assert item_map.get("Tea") == STATION_BEVERAGES
    assert item_map.get("Samosa") == STATION_SNACKS


# ── Test 6: Parallel Station Processing ───────────────────────────────────
def test_06_parallel_station_processing():
    """
    Parallel queue behavior:
    Token 1 -> Biryani -> MAIN DISH
    Token 2 -> Tea -> BEVERAGES
    Token 2 does NOT wait for Token 1.
    """
    res1 = place_test_order([{"name": "Chicken Biriyani", "qty": 1}], student_name="Student 1")
    res2 = place_test_order([{"name": "Tea", "qty": 1}], student_name="Student 2")

    queues = OrderService.get_station_queues()
    main_dish_q = queues[STATION_MAIN_DISH]
    beverages_q = queues[STATION_BEVERAGES]

    # Verify both exist in their own separate station queues
    assert any(t["token"] == res1["token"] for t in main_dish_q)
    assert any(t["token"] == res2["token"] for t in beverages_q)

    # In beverages queue, Token 2 is at position 1, independent of Main Dish!
    bev_task = next(t for t in beverages_q if t["token"] == res2["token"])
    assert bev_task["station_position"] == 1


# ── Test 7 & 8: Cancellation Removes Task & Allows Next to Proceed ────────
def test_07_and_08_cancellation_removes_queue_task_and_advances_next():
    """
    T1 -> Biryani
    T2 -> Biryani
    T3 -> Biryani
    Cancel T1 before ready.
    T1 disappears from active queue.
    T2 becomes next (#1).
    """
    r1 = place_test_order([{"name": "Chicken Biriyani", "qty": 1}], student_name="User 1")
    r2 = place_test_order([{"name": "Chicken Biriyani", "qty": 1}], student_name="User 2")
    r3 = place_test_order([{"name": "Chicken Biriyani", "qty": 1}], student_name="User 3")

    # Cancel T1
    cancel_res = OrderService.cancel_order(r1["token"])
    assert cancel_res["success"] is True

    # Check station queue
    queues = OrderService.get_station_queues()
    active_main_tokens = [t["token"] for t in queues[STATION_MAIN_DISH]]

    # T1 must NOT be in active queue
    assert r1["token"] not in active_main_tokens

    # T2 must now be at the front (#1)
    assert queues[STATION_MAIN_DISH][0]["token"] == r2["token"]
    assert queues[STATION_MAIN_DISH][0]["station_position"] == 1


# ── Test 9, 10, 11, 12: Editing Active Order, +5min Penalty, Re-ordering ──
def test_09_to_12_editing_order_penalty_and_reorder():
    """
    Edit scenario:
    Token 1: Tea (3 mins) -> ETA = now + 3 mins
    Token 2: Tea (3 mins) -> ETA = now + 6 mins
    Customer edits Token 1: adds +5 min penalty -> ETA = now + 8 mins.
    Queue priority recalculates based on current estimated ready time!
    Token 2 (6 mins) becomes FIRST (#1). Token 1 (8 mins) becomes SECOND (#2).
    Token number remains unchanged!
    """
    t1_res = place_test_order([{"name": "Tea", "qty": 1}], student_name="Order 1")
    t2_res = place_test_order([{"name": "Tea", "qty": 1}], student_name="Order 2")

    queues_before = OrderService.get_station_queues()[STATION_BEVERAGES]
    t1_before = next(t for t in queues_before if t["token"] == t1_res["token"])
    t2_before = next(t for t in queues_before if t["token"] == t2_res["token"])

    eta_t1_before = parse_iso(t1_before["estimated_ready_time"])

    # Now edit T1 (increase quantity to 2)
    conn = get_db_connection()
    tea_id = conn.execute("SELECT id FROM menu_items WHERE name = 'Tea'").fetchone()["id"]
    conn.close()

    edit_res = OrderService.edit_order(
        token=t1_res["token"],
        new_items=[{"id": tea_id, "qty": 2}],
    )
    assert edit_res["success"] is True
    # Test 12: Token number remains identical
    assert edit_res["token"] == t1_res["token"]

    # Test 10: Edit added extra preparation time (+5 min penalty)
    queues_after = OrderService.get_station_queues()[STATION_BEVERAGES]
    t1_after = next(t for t in queues_after if t["token"] == t1_res["token"])
    t2_after = next(t for t in queues_after if t["token"] == t2_res["token"])

    eta_t1_after = parse_iso(t1_after["estimated_ready_time"])
    eta_t2_after = parse_iso(t2_after["estimated_ready_time"])
    diff_minutes = (eta_t1_after - eta_t1_before).total_seconds() / 60.0
    assert diff_minutes >= EDIT_EXTRA_MINUTES, f"Expected at least +{EDIT_EXTRA_MINUTES} min penalty, got {diff_minutes}"

    # Test 11: Queue dynamically reorders: T2 is now ahead of T1 because T1 was penalized!
    assert eta_t2_after < eta_t1_after
    assert t2_after["station_position"] == 1
    assert t1_after["station_position"] == 2


# ── Test 13: Editing Unavailable Item Fails Safely ────────────────────────
def test_13_editing_unavailable_item_fails_safely():
    """If a customer attempts to edit an order by adding an out-of-stock item, edit fails safely."""
    order_res = place_test_order([{"name": "Tea", "qty": 1}], student_name="Charlie")

    conn = get_db_connection()
    burger_id = conn.execute("SELECT id FROM menu_items WHERE name = 'Burger'").fetchone()["id"]
    conn.close()

    # Set Burger stock to 0
    reset_menu_item_stock(burger_id, 0)

    # Attempt to edit Charlie's order to include 1 Burger
    with pytest.raises(OutOfStockError):
        OrderService.edit_order(
            token=order_res["token"],
            new_items=[{"name": "Tea", "qty": 1}, {"id": burger_id, "name": "Burger", "qty": 1}],
        )

    # Verify original order is untouched
    tracking = OrderService.get_order_tracking(order_res["token"])
    assert tracking["status"] == STATUS_WAITING
    assert len(tracking["items"]) == 1
    assert tracking["items"][0]["name"] == "Tea"


# ── Test 14 & 15: Editing/Cancelling After Completion Rejected ────────────
def test_14_and_15_editing_and_cancellation_after_completion_rejected():
    """Editing and cancellation must be rejected by backend once food is Ready or Completed."""
    order_res = place_test_order([{"name": "Tea", "qty": 1}], student_name="Dave")
    token = order_res["token"]

    # Advance task to Ready
    task_id = order_res["tasks"][0]["task_id"]
    OrderService.advance_station_task(task_id)  # Waiting -> Preparing
    OrderService.advance_station_task(task_id)  # Preparing -> Ready

    # Status is now Ready: cancellation must be REJECTED!
    with pytest.raises(InvalidOrderStateError):
        OrderService.cancel_order(token)

    # Status is Ready: editing must be REJECTED!
    with pytest.raises(InvalidOrderStateError):
        OrderService.edit_order(token, [{"name": "Tea", "qty": 2}])

    # Advance to Completed
    OrderService.advance_station_task(task_id)  # Ready -> Completed
    with pytest.raises(InvalidOrderStateError):
        OrderService.cancel_order(token)
    with pytest.raises(InvalidOrderStateError):
        OrderService.edit_order(token, [{"name": "Tea", "qty": 2}])


# ── Test 16: Multi-Station Order Creates Multiple Station Tasks ───────────
def test_16_multi_station_order_creates_multiple_tasks():
    """
    Multi-station order:
    Biryani (MAIN DISH) + Samosa (SNACKS) + Tea (BEVERAGES)
    Creates 3 independent station tasks.
    """
    res = place_test_order(
        [
            {"name": "Chicken Biriyani", "qty": 1},
            {"name": "Samosa", "qty": 1},
            {"name": "Tea", "qty": 1},
        ],
        student_name="Multi Station User",
    )
    assert res["success"] is True
    assert len(res["tasks"]) == 3

    station_codes = {t["station_code"] for t in res["tasks"]}
    assert STATION_MAIN_DISH in station_codes
    assert STATION_SNACKS in station_codes
    assert STATION_BEVERAGES in station_codes


# ── Test 17: Live Update Event Emitted After Order Mutation ───────────────
def test_17_live_update_event_emitted():
    """Verify that EventBroker receives and broadcasts events on order operations."""
    received = []
    q = EventBroker.subscribe()

    def listen():
        try:
            msg = q.get(timeout=2.0)
            received.append(msg)
        except Exception:
            pass

    listener_thread = threading.Thread(target=listen)
    listener_thread.start()

    # Trigger order creation
    place_test_order([{"name": "Tea", "qty": 1}], student_name="Event Test")
    listener_thread.join(timeout=3.0)

    assert len(received) >= 1
    assert "order_created" in received[0]
