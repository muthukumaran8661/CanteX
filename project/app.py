"""
app.py
======
Production-grade Flask application for Smart Canteen Ordering & Token System.
Fullstack PS F2 with Round 2 concurrency guarantees, parallel station queues,
cancellation, editing with +5min penalty, dynamic queue reordering,
locked payment stub integration, and real-time SSE updates.
"""

import json
import os
import sys
import queue
from datetime import datetime, timezone

from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, Response, session, redirect, url_for

# ── Project imports ───────────────────────────────────────────────────────
from config import (
    FLASK_HOST,
    FLASK_PORT,
    FLASK_DEBUG,
    SECRET_KEY,
    ROLE_CUSTOMER,
    ROLE_STAFF,
    ROLE_ADMIN,
    STATUS_WAITING,
    STATUS_PREPARING,
    STATUS_READY,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
    STATUS_SERVED,
    VALID_ORDER_STATUSES,
    VALID_STATUSES,
)
from database import (
    init_db,
    reset_menu_item_stock,
    get_admin_metrics,
    get_all_users,
    get_user_by_id,
    OutOfStockError,
    InvalidOrderStateError,
)
from services import (
    OrderService,
    PaymentAdapter,
    EventBroker,
    AuthService,
)
from core import TokenGenerator, OrderQueue

# ── Initialize Database ───────────────────────────────────────────────────
init_db()

# ── Flask Application Setup ───────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Expose backward-compatible global instances
token_gen = TokenGenerator()
order_queue = OrderQueue()


# ── Authentication & Role Helpers ─────────────────────────────────────────

def get_current_user():
    """Retrieve current logged in user dict from Flask session."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    return {
        "id": user_id,
        "email": session.get("user_email", ""),
        "name": session.get("user_name", "Student"),
        "role": session.get("user_role", ROLE_CUSTOMER),
    }


def login_required(f):
    """Enforce that the requesting client is an authenticated user."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_id"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({
                    "success": False,
                    "login_required": True,
                    "message": "Please sign in to place an order.",
                }), 401
            return redirect(url_for("index", login_required="true"))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Enforce that the requesting client has the ADMIN role server-side."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_role = session.get("user_role")
        if user_role != ROLE_ADMIN:
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({
                    "success": False,
                    "message": "Access denied. Admin privileges required.",
                }), 403
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return decorated_function


# ── Frontend Views ────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve student ordering web application."""
    return send_from_directory("static", "user.html")


@app.route("/staff")
def staff():
    """Serve canteen staff station queue & live dashboard."""
    return send_from_directory("static", "staff.html")


@app.route("/admin/login")
def admin_login_page():
    """Serve dedicated Admin Login page."""
    # If already logged in as ADMIN, redirect straight to Admin Dashboard
    if session.get("user_role") == ROLE_ADMIN:
        return redirect("/admin")
    return send_from_directory("static", "admin_login.html")


@app.route("/admin")
@admin_required
def admin_dashboard_page():
    """Serve dedicated, colourful Admin Dashboard (protected by backend authorization)."""
    return send_from_directory("static", "admin.html")


# ── Authentication API Endpoints ──────────────────────────────────────────

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """
    POST /api/auth/login
    Customer/Staff sign-in endpoint.
    Body: {"email": "...", "password": "..."}
    """
    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    res = AuthService.login(email=email, password=password)
    if not res.get("success"):
        return jsonify({"success": False, "message": res.get("message", "Invalid email or password.")}), 401

    user = res["user"]
    session["user_id"] = user["id"]
    session["user_email"] = user["email"]
    session["user_name"] = user["name"]
    session["user_role"] = user["role"]

    return jsonify({
        "success": True,
        "user": user,
        "message": f"Welcome back, {user['name']}!",
    }), 200


@app.route("/api/auth/admin-login", methods=["POST"])
def auth_admin_login():
    """
    POST /api/auth/admin-login
    Dedicated Admin Login endpoint.
    Body: {"email": "...", "password": "..."}
    Rejects unauthorized or invalid attempts with generic 'Invalid admin credentials.'
    """
    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    res = AuthService.login(email=email, password=password, required_role=ROLE_ADMIN)
    if not res.get("success"):
        # Generic security response: do not expose whether email or password was wrong
        return jsonify({
            "success": False,
            "message": "Invalid admin credentials.",
        }), 401

    user = res["user"]
    session["user_id"] = user["id"]
    session["user_email"] = user["email"]
    session["user_name"] = user["name"]
    session["user_role"] = user["role"]

    return jsonify({
        "success": True,
        "redirect": "/admin",
        "user": user,
        "message": f"Welcome Admin, {user['name']}!",
    }), 200


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    """
    POST /api/auth/register
    New student/customer registration.
    Body: {"name": "...", "email": "...", "password": "...", "phone": "..."}
    """
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    phone = data.get("phone", "").strip()

    res = AuthService.register(name=name, email=email, password=password, phone=phone, role=ROLE_CUSTOMER)
    if not res.get("success"):
        return jsonify({"success": False, "message": res.get("message", "Registration failed.")}), 400

    user = res["user"]
    session["user_id"] = user["id"]
    session["user_email"] = user["email"]
    session["user_name"] = user["name"]
    session["user_role"] = user["role"]

    return jsonify({
        "success": True,
        "user": user,
        "message": "Registration successful! You are now signed in.",
    }), 201


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    """
    GET /api/auth/me
    Return current authenticated session details.
    """
    user = get_current_user()
    if user:
        return jsonify({"authenticated": True, "user": user}), 200
    return jsonify({"authenticated": False, "user": None}), 200


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """
    POST /api/auth/logout
    Clears user session and logs out.
    """
    session.clear()
    return jsonify({"success": True, "message": "Signed out successfully."}), 200



# ── Real-Time Server-Sent Events (SSE) ────────────────────────────────────

@app.route("/api/events", methods=["GET"])
def sse_events():
    """
    GET /api/events
    Stream real-time server events to connected clients without page reloads.
    Emits events: order_created, order_edited, order_cancelled, task_advanced, queue_reordered, stock_updated.
    """
    def event_stream():
        client_queue = EventBroker.subscribe()
        # Initial greeting event
        yield f"event: connected\ndata: {{\"status\": \"connected\", \"timestamp\": \"{datetime.now(timezone.utc).isoformat()}\"}}\n\n"
        try:
            while True:
                try:
                    # Wait up to 20 seconds for an event; send ping heartbeat if idle
                    msg = client_queue.get(timeout=20.0)
                    yield msg
                except queue.Empty:
                    # SSE Keep-Alive comment
                    yield ": ping\n\n"
        except GeneratorExit:
            EventBroker.unsubscribe(client_queue)
        except Exception:
            EventBroker.unsubscribe(client_queue)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── Menu & Catalog ────────────────────────────────────────────────────────

@app.route("/api/menu", methods=["GET"])
def get_menu():
    """
    GET /api/menu
    Return full list of food items with live stock, prep time, station info.
    """
    return jsonify(OrderService.get_menu())


# ── Checkout / Atomic Order Creation ──────────────────────────────────────

@app.route("/api/checkout", methods=["POST"])
def checkout():
    """
    POST /api/checkout
    Body:
        {
            "items": [{"id": 1, "name": "Biryani", "qty": 1, "price": 120.0}],
            "student_name": "Student A",
            "method": "UPI"
        }
    Atomic operations protected against race conditions:
        - Stock verification & decrement
        - Payment authorization via locked payment_stub.py
        - Deterministic token allocation
        - Multi-station task creation & dynamic ETA scheduling
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    method = data.get("method", "UPI")

    if not items:
        return jsonify({"success": False, "message": "Cart is empty."}), 400

    # Requirement 2 & 18: Reject guest ordering. User must be signed in.
    current_user = get_current_user()
    if not current_user:
        return jsonify({
            "success": False,
            "login_required": True,
            "message": "Please sign in to place an order.",
        }), 401

    student_name = current_user.get("name") or data.get("student_name", "Student")
    student_email = current_user.get("email", "")
    user_id = current_user.get("id")

    try:
        res = OrderService.create_order(
            items=items,
            student_name=student_name,
            payment_method=method,
            student_email=student_email,
            user_id=user_id,
        )

        if not res.get("success"):
            return jsonify({
                "success": False,
                "message": res.get("message", "Payment failed."),
                "state": res.get("state", "DECLINED"),
            }), 400

        return jsonify({
            "success": True,
            "token": res["token"],
            "order_id": res["order_id"],
            "total": res["total"],
            "transaction_id": res["transaction_id"],
            "tasks": res["tasks"],
            "message": res["message"],
        }), 200

    except OutOfStockError as ose:
        return jsonify({"success": False, "message": str(ose), "sold_out": True}), 409
    except ValueError as ve:
        return jsonify({"success": False, "message": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "message": f"Server error: {str(e)}"}), 500


# ── Order Status & Tracking ───────────────────────────────────────────────

@app.route("/api/order/<token>/status", methods=["GET"])
def order_status(token: str):
    """
    GET /api/order/<token>/status
    Backward-compatible status endpoint.
    """
    tracking = OrderService.get_order_tracking(token)
    if not tracking:
        return jsonify({"error": "Order not found"}), 404

    # Determine primary queue position
    tasks = tracking.get("station_tasks", [])
    primary_pos = -1
    for t in tasks:
        if t.get("queue_position", -1) > 0:
            if primary_pos == -1 or t["queue_position"] < primary_pos:
                primary_pos = t["queue_position"]

    return jsonify({
        "token": token,
        "status": tracking["status"],
        "position": primary_pos,
        "items": tracking["items"],
        "total": tracking["total"],
        "created_time": tracking["created_at"],
        "station_tasks": tasks,
    })


@app.route("/api/order/<token>/tracking", methods=["GET"])
def order_tracking(token: str):
    """
    GET /api/order/<token>/tracking
    Rich multi-station tracking details with station-by-station ETAs.
    """
    tracking = OrderService.get_order_tracking(token)
    if not tracking:
        return jsonify({"error": "Order not found"}), 404
    return jsonify(tracking)


# ── Order Cancellation ────────────────────────────────────────────────────

@app.route("/api/order/<token>/cancel", methods=["POST"])
def cancel_order(token: str):
    """
    POST /api/order/<token>/cancel
    Cancel an active order before food is READY:
        - Rejects if READY or COMPLETED
        - Removes from active station queues
        - Restores inventory
        - Issues refund through payment_stub
    """
    try:
        res = OrderService.cancel_order(token)
        return jsonify(res), 200
    except InvalidOrderStateError as iose:
        return jsonify({"success": False, "message": str(iose)}), 400
    except ValueError as ve:
        return jsonify({"success": False, "message": str(ve)}), 404
    except Exception as e:
        return jsonify({"success": False, "message": f"Cancellation error: {str(e)}"}), 500


# ── Order Editing with +5min Penalty ──────────────────────────────────────

@app.route("/api/order/<token>/edit", methods=["POST"])
def edit_order(token: str):
    """
    POST /api/order/<token>/edit
    Body:
        {
            "items": [{"id": 1, "qty": 2}, {"id": 14, "qty": 1}]
        }
    Features:
        - Allowed only before food is READY / COMPLETED
        - Concurrency-safe atomic inventory reservation for new items
        - Preserves same customer token & order ID
        - Adds EDIT_EXTRA_MINUTES (+5 mins) prep penalty
        - Re-sorts affected station queues dynamically by updated ETA
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])

    if not items:
        return jsonify({"success": False, "message": "Edited items cannot be empty."}), 400

    try:
        res = OrderService.edit_order(token, items)
        return jsonify(res), 200
    except OutOfStockError as ose:
        return jsonify({"success": False, "message": str(ose), "sold_out": True}), 409
    except InvalidOrderStateError as iose:
        return jsonify({"success": False, "message": str(iose)}), 400
    except ValueError as ve:
        return jsonify({"success": False, "message": str(ve)}), 404
    except Exception as e:
        return jsonify({"success": False, "message": f"Edit error: {str(e)}"}), 500


# ── Parallel Station Queues & Live Board ──────────────────────────────────

@app.route("/api/stations/queue", methods=["GET"])
def station_queues():
    """
    GET /api/stations/queue
    Return active queues segregated by station (MAIN_DISH, SNACKS, BEVERAGES),
    sorted deterministically by current estimated ready time.
    """
    return jsonify(OrderService.get_station_queues())


@app.route("/api/queue", methods=["GET"])
def get_queue():
    """
    GET /api/queue
    Backward-compatible queue endpoint. Returns flat active queue and station breakdown.
    """
    st_queues = OrderService.get_station_queues()
    flat_active = []
    for st_code, tasks in st_queues.items():
        flat_active.extend(tasks)

    # Sort flat list by ETA
    from core.scheduler import parse_iso
    flat_active.sort(key=lambda t: parse_iso(t.get("estimated_ready_time")))

    return jsonify({
        "queue": flat_active,
        "size": len(flat_active),
        "stations": st_queues,
    })


@app.route("/api/live-board", methods=["GET"])
def live_board():
    """
    GET /api/live-board
    Return station-wise Now Preparing, Next, and Ready tokens.
    """
    return jsonify(OrderService.get_live_board())


# ── Staff Queue Progression ───────────────────────────────────────────────

@app.route("/api/station-task/<int:task_id>/advance", methods=["POST"])
def advance_task(task_id: int):
    """
    POST /api/station-task/<task_id>/advance
    Move station task: Waiting -> Preparing -> Ready -> Completed.
    """
    try:
        res = OrderService.advance_station_task(task_id)
        return jsonify(res), 200
    except InvalidOrderStateError as iose:
        return jsonify({"success": False, "message": str(iose)}), 400
    except ValueError as ve:
        return jsonify({"success": False, "message": str(ve)}), 404
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/order/<token>/advance", methods=["POST"])
def advance_order(token: str):
    """
    POST /api/order/<token>/advance
    Backward-compatible advance endpoint.
    """
    try:
        res = OrderService.advance_order_by_token(token)
        return jsonify(res), 200
    except InvalidOrderStateError as iose:
        return jsonify({"success": False, "message": str(iose)}), 400
    except ValueError as ve:
        return jsonify({"success": False, "message": str(ve)}), 404
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ── Staff Inventory & History Management ──────────────────────────────────

@app.route("/api/orders", methods=["GET"])
def all_orders():
    """
    GET /api/orders
    Return all orders history.
    """
    return jsonify(OrderService.get_all_orders())


@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    """
    GET /api/inventory
    Return inventory table with remaining stock for each dish.
    """
    return jsonify(OrderService.get_menu())


@app.route("/api/inventory/<int:item_id>/reset", methods=["POST"])
def reset_inventory(item_id: int):
    """
    POST /api/inventory/<item_id>/reset
    Body: {"stock": 1}
    Allows resetting stock for demo scenarios (e.g. Burger stock = 1).
    """
    data = request.get_json(silent=True) or {}
    new_stock = int(data.get("stock", 25))
    try:
        reset_menu_item_stock(item_id, new_stock)
        EventBroker.publish("stock_updated", {"item_id": item_id, "new_stock": new_stock})
        return jsonify({"success": True, "item_id": item_id, "new_stock": new_stock}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ── Protected Admin API Endpoints ─────────────────────────────────────────

@app.route("/api/admin/metrics", methods=["GET"])
@admin_required
def admin_metrics():
    """
    GET /api/admin/metrics
    Protected admin endpoint returning real-time aggregated metrics.
    """
    return jsonify(get_admin_metrics()), 200


@app.route("/api/admin/users", methods=["GET"])
@admin_required
def admin_users():
    """
    GET /api/admin/users
    Protected admin endpoint returning registered students/customers.
    """
    return jsonify(get_all_users(role=ROLE_CUSTOMER)), 200


@app.route("/api/admin/staff", methods=["GET"])
@admin_required
def admin_staff():
    """
    GET /api/admin/staff
    Protected admin endpoint returning staff roster with station assignment.
    """
    return jsonify(get_all_users(role=ROLE_STAFF)), 200


# ── Entry Point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)
