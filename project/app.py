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
    save_food_rating,
    get_user_food_rating,
    get_user_ratings,
    delete_food_rating,
    get_food_rating_summary,
    get_all_food_ratings_summary,
    get_top_rated_foods,
    get_food_details,
    save_user_preferences,
    get_user_preferences,
    get_recommendation_admin_metrics,
)
from services import (
    OrderService,
    PaymentAdapter,
    EventBroker,
    AuthService,
    RecommendationService,
)
from core import TokenGenerator, OrderQueue

# ── Initialize Database ───────────────────────────────────────────────────
init_db()

# ── Flask Application Setup ───────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# Expose backward-compatible global instances
token_gen = TokenGenerator()
order_queue = OrderQueue()


# ── Response Cache-Control & Deployment Freshness Hook ────────────────────

@app.after_request
def apply_cache_headers(response):
    """
    Ensure users always receive the latest files upon deployment:
    1. Dynamic APIs, SSE events, and session endpoints must NEVER be cached (no-store).
    2. HTML entrypoints (index, user.html, staff, admin) must never be cached by HTTP cache.
    3. Service Worker must never be cached so browser detects updates on every check.
    4. Static assets (CSS, JS) can revalidate or use versioned queries.
    """
    path = request.path

    # Dynamic APIs & real-time SSE streams: completely dynamic, never cache
    if path.startswith("/api/") or path.startswith("/events"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, post-check=0, pre-check=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    # Service Worker script: check on every visit/poll
    if path == "/sw.js":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    # HTML documents: must never store stale HTML shells
    content_type = response.headers.get("Content-Type", "")
    if "text/html" in content_type or path in ["/", "/index.html", "/staff", "/admin", "/admin/login"]:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    # Static assets: allow conditional caching with revalidation
    if path.startswith("/static/"):
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        return response

    return response


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
@app.route("/index.html")
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


# ── PWA Routes (must be at root scope for SW registration) ───────────────

@app.route("/sw.js")
def service_worker():
    """Serve PWA Service Worker at root scope (/sw.js) — required for scope:'/' to work."""
    response = send_from_directory("static", "sw.js")
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/manifest.json")
def pwa_manifest():
    """Serve PWA manifest at canonical root path."""
    response = send_from_directory("static", "manifest.json")
    response.headers["Content-Type"] = "application/manifest+json"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


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


# ── Menu & Food Catalog ───────────────────────────────────────────────────

@app.route("/api/menu", methods=["GET"])
@app.route("/api/food", methods=["GET"])
def get_menu():
    """
    GET /api/menu or GET /api/food
    Return full list of food items with live stock, prep time, station info,
    and genuine rating summaries (average rating, count, user's rating).
    """
    items = OrderService.get_menu()
    ratings_map = get_all_food_ratings_summary()
    current_user = get_current_user()
    user_ratings = {}
    if current_user:
        for r in get_user_ratings(current_user["id"]):
            user_ratings[r["food_id"]] = r["rating"]

    for it in items:
        fid = it["id"]
        r_info = ratings_map.get(fid, {"average_rating": None, "rating_count": 0})
        it["average_rating"] = r_info["average_rating"]
        it["rating_count"] = r_info["rating_count"]
        it["is_available"] = it.get("stock", 0) > 0
        it["user_rating"] = user_ratings.get(fid)

    return jsonify(items)


@app.route("/api/food/<int:food_id>", methods=["GET"])
def get_food_item_detail(food_id: int):
    """
    GET /api/food/<food_id>
    Return detailed information for a single food item including
    complete rating summary, reviews, user's own rating, and CF related foods.
    """
    current_user = get_current_user()
    user_id = current_user.get("id") if current_user else None
    food = get_food_details(food_id=food_id, user_id=user_id)
    if not food:
        return jsonify({"success": False, "message": "Food item not found."}), 404

    # Attach Item-to-Item CF related foods
    food["related_foods"] = RecommendationService.get_related_foods(food_id=food_id, limit=4)
    return jsonify({"success": True, "food": food}), 200


@app.route("/api/food/<int:food_id>/related", methods=["GET"])
def get_related_food_items(food_id: int):
    """
    GET /api/food/<food_id>/related
    Return related food items based on Item-to-Item Collaborative Filtering.
    """
    limit = int(request.args.get("limit", 4))
    related = RecommendationService.get_related_foods(food_id=food_id, limit=limit)
    return jsonify({"success": True, "food_id": food_id, "related": related}), 200


@app.route("/api/food/top-rated", methods=["GET"])
def get_top_rated_foods_endpoint():
    """
    GET /api/food/top-rated
    Return top rated foods ranked using Bayesian rating score.
    """
    limit = int(request.args.get("limit", 10))
    min_ratings = int(request.args.get("min_ratings", 1))
    top_foods = get_top_rated_foods(limit=limit, min_ratings=min_ratings)
    return jsonify({"success": True, "top_rated": top_foods}), 200


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
    Protected admin endpoint returning real-time aggregated metrics including
    canteen order operations and recommendation/ratings analytics.
    """
    metrics = get_admin_metrics()
    try:
        rec_metrics = get_recommendation_admin_metrics()
        rec_insights = RecommendationService.get_admin_insights()
        metrics["recommendations"] = rec_metrics
        metrics["recommendation_insights"] = rec_insights
    except Exception as e:
        metrics["recommendations_error"] = str(e)
    return jsonify(metrics), 200


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


# ── Recommendation System API Endpoints ──────────────────────────────────

@app.route("/api/recommendations", methods=["GET", "POST"])
def get_recommendations_endpoint():
    """
    GET / POST /api/recommendations
    Returns the comprehensive 6-section recommendation response:
      1. Recommended for You (Item-to-Item CF or Cold-start fallback)
      2. Frequently Ordered Together (Pairs with 1-click combo)
      3. Because You Ordered This (Contextual recommendations anchored to past order)
      4. Popular at Smart Canteen (Bestsellers across categories)
      5. Try Something New (Dishes student hasn't tried)
      6. Quick Bites for You (Fast snacks & beverages)
    Accepts optional cart item IDs in query params (?cart=1,2) or JSON body {"cart_item_ids": [1, 2]}.
    """
    current_user = get_current_user()
    user_id = current_user.get("id") if current_user else None
    email = current_user.get("email") if current_user else None

    cart_ids = []
    limit = 6

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        cart_ids = data.get("cart_item_ids") or []
        limit = int(data.get("limit", 6))
    else:
        cart_param = request.args.get("cart", "").strip()
        if cart_param:
            try:
                cart_ids = [int(x) for x in cart_param.split(",") if x.isdigit()]
            except Exception:
                cart_ids = []
        limit = int(request.args.get("limit", 6))

    try:
        recs = RecommendationService.get_recommendations(
            user_id=user_id,
            email=email,
            cart_item_ids=cart_ids,
            limit=limit,
        )
        return jsonify(recs), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/recommendations/frequently-ordered-together", methods=["GET"])
def frequently_ordered_together_endpoint():
    """
    GET /api/recommendations/frequently-ordered-together
    Returns item pairs frequently purchased together in the same order.
    Optional query params: ?cart=1,2
    """
    current_user = get_current_user()
    user_id = current_user.get("id") if current_user else None
    email = current_user.get("email") if current_user else None

    cart_param = request.args.get("cart", "").strip()
    cart_ids = []
    if cart_param:
        try:
            cart_ids = [int(x) for x in cart_param.split(",") if x.isdigit()]
        except Exception:
            cart_ids = []

    try:
        res = RecommendationService.get_recommendations(
            user_id=user_id,
            email=email,
            cart_item_ids=cart_ids,
            limit=4,
        )
        return jsonify(res["sections"]["frequently_ordered_together"]), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/admin/recommendations/insights", methods=["GET"])
@admin_required
def admin_recommendation_insights():
    """
    GET /api/admin/recommendations/insights
    Protected admin endpoint returning real measured recommendation analytics.
    """
    try:
        insights = RecommendationService.get_admin_insights()
        return jsonify(insights), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/admin/recommendations/seed", methods=["POST"])
@admin_required
def admin_seed_recommendations():
    """
    POST /api/admin/recommendations/seed
    Protected admin endpoint to seed authentic historical orders for demonstration.
    """
    data = request.get_json(silent=True) or {}
    count = int(data.get("count", 25))
    force = bool(data.get("force", False))
    try:
        inserted = RecommendationService.seed_demo_orders(count=count, force=force)
        return jsonify({
            "success": True,
            "orders_seeded": inserted,
            "message": f"Successfully seeded {inserted} authentic historical orders.",
        }), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/admin/recommendations/evaluate", methods=["GET"])
@admin_required
def admin_evaluate_recommendations():
    """
    GET /api/admin/recommendations/evaluate
    Protected admin endpoint calculating offline ranking metric (Precision@K and Recall@K).
    """
    k = int(request.args.get("k", 3))
    try:
        results = RecommendationService.evaluate_offline_ranking(k=k)
        return jsonify(results), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ── Ratings & Reviews API Endpoints ──────────────────────────────────────

@app.route("/api/ratings/me", methods=["GET"])
@login_required
def get_my_ratings():
    """
    GET /api/ratings/me
    Return all food ratings submitted by the authenticated user.
    """
    current_user = get_current_user()
    ratings = get_user_ratings(current_user["id"])
    return jsonify({"success": True, "ratings": ratings}), 200


@app.route("/api/ratings", methods=["POST"])
@login_required
def submit_food_rating():
    """
    POST /api/ratings
    Submit or update a rating (1-5 stars, optional review).
    Enforces exactly 1 active rating per user per food item via database UPSERT.
    """
    current_user = get_current_user()
    data = request.get_json(silent=True) or {}

    food_id = data.get("food_id")
    rating = data.get("rating")
    review = data.get("review")

    if food_id is None:
        return jsonify({"success": False, "message": "food_id is required."}), 400

    try:
        food_id = int(food_id)
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "food_id must be a valid integer."}), 400

    if rating is None:
        return jsonify({"success": False, "message": "rating is required."}), 400

    try:
        rating_val = int(rating)
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "rating must be an integer between 1 and 5."}), 400

    if rating_val < 1 or rating_val > 5:
        return jsonify({"success": False, "message": "rating must be between 1 and 5 stars."}), 400

    try:
        saved = save_food_rating(
            user_id=current_user["id"],
            food_id=food_id,
            rating=rating_val,
            review=str(review).strip() if review else None,
        )
        return jsonify({
            "success": True,
            "rating": saved,
            "message": "Thank you! Your rating has been recorded.",
        }), 200
    except ValueError as ve:
        return jsonify({"success": False, "message": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to save rating: {str(e)}"}), 500


@app.route("/api/ratings/<int:rating_id>", methods=["PUT"])
@login_required
def update_food_rating(rating_id: int):
    """
    PUT /api/ratings/<rating_id>
    Update an existing rating. Enforces that rating belongs to current user.
    """
    current_user = get_current_user()
    data = request.get_json(silent=True) or {}
    food_id = data.get("food_id")
    rating = data.get("rating")
    review = data.get("review")

    # If food_id is not supplied in body, verify rating ownership from existing ratings
    if not food_id:
        user_ratings = get_user_ratings(current_user["id"])
        target = next((r for r in user_ratings if r["id"] == rating_id), None)
        if not target:
            return jsonify({"success": False, "message": "Rating not found or access denied."}), 404
        food_id = target["food_id"]

    try:
        rating_val = int(rating)
        if rating_val < 1 or rating_val > 5:
            return jsonify({"success": False, "message": "Rating must be between 1 and 5."}), 400
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "Valid integer rating (1-5) is required."}), 400

    try:
        saved = save_food_rating(
            user_id=current_user["id"],
            food_id=int(food_id),
            rating=rating_val,
            review=str(review).strip() if review else None,
        )
        return jsonify({"success": True, "rating": saved, "message": "Rating updated successfully."}), 200
    except ValueError as ve:
        return jsonify({"success": False, "message": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/ratings/<int:rating_id>", methods=["DELETE"])
@login_required
def delete_rating_endpoint(rating_id: int):
    """
    DELETE /api/ratings/<rating_id>
    Delete a user's own rating. Enforces ownership strictly.
    """
    current_user = get_current_user()
    deleted = delete_food_rating(user_id=current_user["id"], rating_id=rating_id)
    if not deleted:
        return jsonify({"success": False, "message": "Rating not found or access denied."}), 404

    return jsonify({"success": True, "message": "Rating removed successfully."}), 200


# ── User Preferences & Cold-Start Onboarding ─────────────────────────────

@app.route("/api/user/preferences", methods=["GET"])
@login_required
def get_preferences_endpoint():
    """
    GET /api/user/preferences
    Return taste profile and dietary preferences for current user.
    """
    current_user = get_current_user()
    prefs = get_user_preferences(current_user["id"])
    return jsonify({"success": True, "preferences": prefs}), 200


@app.route("/api/user/preferences", methods=["POST"])
@login_required
def save_preferences_endpoint():
    """
    POST /api/user/preferences
    Save or update taste preferences (categories, dietary) for cold-start recommendations.
    """
    current_user = get_current_user()
    data = request.get_json(silent=True) or {}
    categories = data.get("preferred_categories", [])
    if not isinstance(categories, list):
        categories = []

    dietary = data.get("dietary_preference", "ALL")
    if dietary not in ("ALL", "VEG", "NON_VEG"):
        dietary = "ALL"

    try:
        saved = save_user_preferences(
            user_id=current_user["id"],
            preferred_categories=categories,
            dietary_preference=dietary,
        )
        return jsonify({
            "success": True,
            "preferences": saved,
            "message": "Preferences saved successfully.",
        }), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ── Entry Point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)
