"""
database.py
===========
Production-style relational database layer using SQLite with WAL mode.
Provides row-level atomic inventory reservations and concurrency guarantees.
"""

import sqlite3
import threading
import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from werkzeug.security import generate_password_hash, check_password_hash

from config import (
    DATABASE_PATH,
    DATA_DIR,
    MENU_FILE,
    DEFAULT_STATIONS,
    STATION_MAIN_DISH,
    STATION_SNACKS,
    STATION_BEVERAGES,
    STATUS_WAITING,
    STATUS_PREPARING,
    STATUS_READY,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
    STATUS_PARTIALLY_READY,
    STATUS_SERVED,
    TOKEN_PREFIX,
    TOKEN_PADDING,
    ROLE_CUSTOMER,
    ROLE_STAFF,
    ROLE_ADMIN,
    DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_NAME,
    DEFAULT_STAFF_EMAIL,
    DEFAULT_STAFF_PASSWORD,
    DEFAULT_STAFF_NAME,
    DEFAULT_STUDENT_EMAIL,
    DEFAULT_STUDENT_PASSWORD,
    DEFAULT_STUDENT_NAME,
)

# Global re-entrant lock to protect concurrent database operations within the process
_DB_LOCK = threading.RLock()



class OutOfStockError(Exception):
    """Raised when an item does not have enough inventory to fulfill an order or edit."""
    pass


class InvalidOrderStateError(Exception):
    """Raised when an illegal order transition is attempted (e.g. edit/cancel when Ready)."""
    pass


def get_db_connection() -> sqlite3.Connection:
    """Create a new SQLite connection with WAL mode and row factory."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    """Create all required tables and indexes if they do not exist."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # 1. Stations table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    icon TEXT NOT NULL,
                    display_order INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                )
            """)

            # 2. Menu items table with stock and station mapping
            conn.execute("""
                CREATE TABLE IF NOT EXISTS menu_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    price REAL NOT NULL,
                    category TEXT NOT NULL,
                    station_code TEXT NOT NULL REFERENCES stations(code),
                    prep_time_minutes INTEGER NOT NULL DEFAULT 5,
                    stock INTEGER NOT NULL DEFAULT 50,
                    initial_stock INTEGER NOT NULL DEFAULT 50,
                    image TEXT NOT NULL,
                    badge TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1
                )
            """)

            # 3. Orders table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT UNIQUE NOT NULL,
                    student_name TEXT NOT NULL DEFAULT 'Student',
                    status TEXT NOT NULL DEFAULT 'Waiting',
                    total REAL NOT NULL,
                    payment_method TEXT NOT NULL DEFAULT 'UPI',
                    transaction_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # 4. Order items
            conn.execute("""
                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    menu_item_id INTEGER NOT NULL REFERENCES menu_items(id),
                    item_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    station_code TEXT NOT NULL
                )
            """)

            # 5. Station tasks (Station-specific preparation queue entries)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS station_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    token TEXT NOT NULL,
                    station_code TEXT NOT NULL REFERENCES stations(code),
                    status TEXT NOT NULL DEFAULT 'Waiting',
                    estimated_ready_time TEXT NOT NULL,
                    actual_ready_time TEXT,
                    items_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # 6. Inventory audit log
            conn.execute("""
                CREATE TABLE IF NOT EXISTS inventory_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    menu_item_id INTEGER NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    order_token TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            # 7. Token sequence counter table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS token_sequence (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_seq INTEGER NOT NULL DEFAULT 1
                )
            """)

            # 8. Users table for authentication and RBAC
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'CUSTOMER',
                    phone TEXT DEFAULT '',
                    station_code TEXT DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_active TEXT NOT NULL
                )
            """)

            # 9. Food ratings table (One active rating per user per food item)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ratings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    food_id INTEGER NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
                    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                    review TEXT DEFAULT '',
                    is_verified_purchase INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, food_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ratings_user ON ratings(user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ratings_food ON ratings(food_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ratings_val ON ratings(rating);")

            # 10. User food preferences table (for cold start & dietary personalization)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    preferred_categories TEXT DEFAULT '[]',
                    dietary_preference TEXT DEFAULT 'ALL',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # Ensure student_email and user_id columns exist on orders table
            try:
                conn.execute("ALTER TABLE orders ADD COLUMN student_email TEXT DEFAULT ''")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE orders ADD COLUMN user_id INTEGER DEFAULT NULL")
            except Exception:
                pass

            # Seed token sequence row if missing
            conn.execute("""
                INSERT OR IGNORE INTO token_sequence (id, next_seq) VALUES (1, 1)
            """)

            # Seed default stations if missing
            for s in DEFAULT_STATIONS:
                conn.execute("""
                    INSERT OR IGNORE INTO stations (code, name, icon, display_order, active)
                    VALUES (?, ?, ?, ?, 1)
                """, (s["code"], s["name"], s["icon"], s["display_order"]))

            # Seed default accounts (ADMIN, STAFF, CUSTOMER)
            now_iso = datetime.now(timezone.utc).isoformat()
            default_accounts = [
                (
                    DEFAULT_ADMIN_NAME,
                    DEFAULT_ADMIN_EMAIL.lower(),
                    generate_password_hash(DEFAULT_ADMIN_PASSWORD),
                    ROLE_ADMIN,
                    "+91 98401 11222",
                    "",
                ),
                (
                    DEFAULT_STAFF_NAME,
                    DEFAULT_STAFF_EMAIL.lower(),
                    generate_password_hash(DEFAULT_STAFF_PASSWORD),
                    ROLE_STAFF,
                    "+91 98402 33444",
                    STATION_MAIN_DISH,
                ),
                (
                    DEFAULT_STUDENT_NAME,
                    DEFAULT_STUDENT_EMAIL.lower(),
                    generate_password_hash(DEFAULT_STUDENT_PASSWORD),
                    ROLE_CUSTOMER,
                    "+91 98403 55666",
                    "",
                ),
                (
                    "Priya Sundaram",
                    "priya@smartcanteen.com",
                    generate_password_hash("Student@123"),
                    ROLE_CUSTOMER,
                    "+91 98404 77889",
                    "",
                ),
                (
                    "Anand Kumar",
                    "anand@smartcanteen.com",
                    generate_password_hash("Student@123"),
                    ROLE_CUSTOMER,
                    "+91 98405 11335",
                    "",
                ),
                (
                    "Divya Ramesh",
                    "divya@smartcanteen.com",
                    generate_password_hash("Student@123"),
                    ROLE_CUSTOMER,
                    "+91 98406 22446",
                    "",
                ),
                (
                    "Suresh Babu",
                    "suresh@smartcanteen.com",
                    generate_password_hash("Student@123"),
                    ROLE_CUSTOMER,
                    "+91 98407 33557",
                    "",
                ),
            ]
            for u_name, u_email, u_pwd, u_role, u_phone, u_station in default_accounts:
                conn.execute("""
                    INSERT OR IGNORE INTO users (
                        name, email, password_hash, role, phone, station_code, active, created_at, last_active
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """, (u_name, u_email, u_pwd, u_role, u_phone, u_station, now_iso, now_iso))

            # Seed menu items if empty
            cur = conn.execute("SELECT COUNT(*) as cnt FROM menu_items")
            row = cur.fetchone()
            if row and row["cnt"] == 0:
                _seed_menu_items(conn)

            # Seed initial authentic baseline ratings if ratings table is empty
            cur_r = conn.execute("SELECT COUNT(*) as cnt FROM ratings")
            row_r = cur_r.fetchone()
            if row_r and row_r["cnt"] == 0:
                _seed_initial_ratings(conn)

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _seed_menu_items(conn: sqlite3.Connection):
    """Seed menu items with authentic station assignments and realistic preparation times."""
    # Station category mapping rules
    station_map = {
        "Idli": (STATION_MAIN_DISH, 4, 30),
        "Masala Dosa": (STATION_MAIN_DISH, 6, 25),
        "Pongal": (STATION_MAIN_DISH, 5, 20),
        "Vada": (STATION_SNACKS, 4, 30),
        "Poori Masala": (STATION_MAIN_DISH, 6, 20),
        "Sambar Rice": (STATION_MAIN_DISH, 5, 25),
        "Lemon Rice": (STATION_MAIN_DISH, 4, 25),
        "Curd Rice": (STATION_MAIN_DISH, 3, 25),
        "Tomato Rice": (STATION_MAIN_DISH, 5, 20),
        "Parotta": (STATION_MAIN_DISH, 7, 30),
        "Chicken Biriyani": (STATION_MAIN_DISH, 10, 30),
        "Veg Meals": (STATION_MAIN_DISH, 8, 25),
        "Chicken 65": (STATION_SNACKS, 6, 25),
        "Tea": (STATION_BEVERAGES, 3, 50),
        "Filter Coffee": (STATION_BEVERAGES, 3, 50),
        "Fresh Lime Juice": (STATION_BEVERAGES, 2, 40),
        "Samosa": (STATION_SNACKS, 4, 30),
        "Burger": (STATION_SNACKS, 7, 15),
        "Sandwich": (STATION_SNACKS, 5, 20),
    }

    raw_items = []
    if os.path.exists(MENU_FILE):
        try:
            with open(MENU_FILE, "r", encoding="utf-8") as f:
                raw_items = json.load(f)
        except Exception:
            pass

    # Ensure Burger and Samosa exist for test & demo scenarios
    existing_names = {it.get("name") for it in raw_items}
    if "Burger" not in existing_names:
        raw_items.append({
            "id": 17,
            "name": "Burger",
            "price": 60,
            "category": "Snacks",
            "description": "Crispy spiced veggie patty burger with fresh lettuce.",
            "image": "/static/images/parotta.jpg",
            "badge": "SNACK",
            "rating": 4.8,
            "reviews": 120,
        })
    if "Samosa" not in existing_names:
        raw_items.append({
            "id": 18,
            "name": "Samosa",
            "price": 20,
            "category": "Snacks",
            "description": "Crispy golden triangular pastry stuffed with spicy potatoes.",
            "image": "/static/images/samosa.svg",
            "badge": "CRUNCHY",
            "rating": 4.7,
            "reviews": 190,
        })

    for item in raw_items:
        name = item["name"]
        st_code, prep_time, stock_val = station_map.get(
            name, (STATION_MAIN_DISH, 5, 25)
        )
        conn.execute("""
            INSERT OR REPLACE INTO menu_items (
                id, name, price, category, station_code,
                prep_time_minutes, stock, initial_stock,
                image, badge, description, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            item["id"],
            name,
            float(item["price"]),
            item.get("category", "Main"),
            st_code,
            prep_time,
            stock_val,
            stock_val,
            item.get("image", "/static/images/idli.jpg"),
            item.get("badge", ""),
            item.get("description", ""),
        ))


def is_verified_purchase(conn: sqlite3.Connection, user_id: int, food_id: int) -> bool:
    """Check if the user has an eligible order containing this food item."""
    cur = conn.execute("""
        SELECT 1
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        WHERE o.user_id = ?
          AND oi.menu_item_id = ?
          AND o.status IN ('Waiting', 'Preparing', 'Partially_Ready', 'Ready', 'Completed', 'Served')
        LIMIT 1
    """, (user_id, food_id))
    return cur.fetchone() is not None


def _seed_initial_ratings(conn: sqlite3.Connection):
    """Seed authentic baseline ratings across student personas for collaborative filtering."""
    # Map users by email
    users_cur = conn.execute("SELECT id, email FROM users")
    user_map = {r["email"].lower(): r["id"] for r in users_cur.fetchall()}

    # Map menu items by name
    items_cur = conn.execute("SELECT id, name FROM menu_items")
    item_map = {r["name"]: r["id"] for r in items_cur.fetchall()}

    karthik_id = user_map.get(DEFAULT_STUDENT_EMAIL.lower())
    priya_id = user_map.get("priya@smartcanteen.com")
    anand_id = user_map.get("anand@smartcanteen.com")
    divya_id = user_map.get("divya@smartcanteen.com")
    suresh_id = user_map.get("suresh@smartcanteen.com")

    now_iso = datetime.now(timezone.utc).isoformat()

    seed_data = []
    # Karthik Raja (Breakfast & Coffee lover)
    if karthik_id:
        seed_data.extend([
            (karthik_id, item_map.get("Masala Dosa"), 5, "Crispy golden roast dosa with aromatic potato masala and fresh coconut chutney!"),
            (karthik_id, item_map.get("Filter Coffee"), 5, "Authentic Kumbakonam degree filter coffee, frothy and bold."),
            (karthik_id, item_map.get("Idli"), 4, "Soft, steaming hot idlis served with piping hot drumstick sambar."),
            (karthik_id, item_map.get("Pongal"), 4, "Rich with pure ghee, whole black pepper, and roasted cashews."),
            (karthik_id, item_map.get("Vada"), 4, "Crispy outside, soft inside medu vada."),
        ])

    # Priya Sundaram (South Indian breakfast & light snacks)
    if priya_id:
        seed_data.extend([
            (priya_id, item_map.get("Masala Dosa"), 5, "My daily breakfast favorite on campus! Unmatched crispiness."),
            (priya_id, item_map.get("Filter Coffee"), 5, "Strong aromatic decoction, perfectly sweetened."),
            (priya_id, item_map.get("Vada"), 5, "Super crispy medu vada, pairs delightfully with filter coffee."),
            (priya_id, item_map.get("Poori Masala"), 4, "Puffy golden pooris with flavorful potato peas bhaji."),
            (priya_id, item_map.get("Tea"), 3, "Decent masala tea, but I prefer the filter coffee here."),
        ])

    # Anand Kumar (Biriyani & Non-Veg / Special lover)
    if anand_id:
        seed_data.extend([
            (anand_id, item_map.get("Chicken Biriyani"), 5, "Fragrant Seeraga Samba rice with tender spiced chicken. Absolute 10/10!"),
            (anand_id, item_map.get("Chicken 65"), 5, "Crunchy outside, juicy inside with curry leaves and green chilies."),
            (anand_id, item_map.get("Parotta"), 4, "Flaky multi-layered Malabar parottas served with rich salna."),
            (anand_id, item_map.get("Fresh Lime Juice"), 4, "Refreshing cold lime juice, perfect thirst quencher after a spicy meal."),
        ])

    # Divya Ramesh (Traditional Meals & Rice varieties)
    if divya_id:
        seed_data.extend([
            (divya_id, item_map.get("Veg Meals"), 5, "Wholesome South Indian thali meal with kootu, poriyal, rasam and appalam."),
            (divya_id, item_map.get("Sambar Rice"), 5, "Comfort food at its best! Perfectly seasoned with ghee and papad."),
            (divya_id, item_map.get("Curd Rice"), 4, "Creamy tempered curd rice with pomegranate seeds and mustard tadka."),
            (divya_id, item_map.get("Lemon Rice"), 4, "Zesty lemon flavor with roasted peanuts and curry leaves."),
        ])

    # Suresh Babu (Evening snacks & quick bites)
    if suresh_id:
        seed_data.extend([
            (suresh_id, item_map.get("Samosa"), 5, "Hot and flaky crust stuffed with spiced potato masala. Great evening snack!"),
            (suresh_id, item_map.get("Tea"), 5, "Strong ginger cardamom tea, brewed to perfection."),
            (suresh_id, item_map.get("Tomato Rice"), 4, "Tangy and flavorful tomato bath with onion raita."),
            (suresh_id, item_map.get("Vada"), 3, "Good when fresh from the kadai in the evening."),
        ])

    for uid, fid, rating, review in seed_data:
        if uid and fid:
            verified = 1 if is_verified_purchase(conn, uid, fid) else 0
            conn.execute("""
                INSERT OR IGNORE INTO ratings (
                    user_id, food_id, rating, review, is_verified_purchase, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (uid, fid, rating, review, verified, now_iso, now_iso))


def reset_menu_item_stock(item_id: int, new_stock: int):
    """Update stock directly (e.g. for testing and canteen staff inventory management)."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                UPDATE menu_items SET stock = ?, initial_stock = ? WHERE id = ?
            """, (new_stock, new_stock, item_id))
            conn.execute("""
                INSERT INTO inventory_logs (menu_item_id, delta, reason, created_at)
                VALUES (?, ?, 'STAFF_MANUAL_RESET', datetime('now'))
            """, (item_id, new_stock))
            conn.execute("COMMIT")
            try:
                from services.recommendation_service import RecommendationService
                RecommendationService.invalidate_cache()
            except Exception:
                pass
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


# ── User Account & Authentication Database Helpers ─────────────────────────

def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Retrieve active user by email (case-insensitive)."""
    if not email:
        return None
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            cur = conn.execute("""
                SELECT id, name, email, password_hash, role, phone, station_code, active, created_at, last_active
                FROM users
                WHERE LOWER(email) = LOWER(?) AND active = 1
            """, (email.strip(),))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve user by ID."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            cur = conn.execute("""
                SELECT id, name, email, password_hash, role, phone, station_code, active, created_at, last_active
                FROM users
                WHERE id = ? AND active = 1
            """, (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def create_user(
    name: str,
    email: str,
    password_hash: str,
    role: str = "CUSTOMER",
    phone: str = "",
    station_code: str = "",
) -> int:
    """Create a new user account."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("""
                INSERT INTO users (
                    name, email, password_hash, role, phone, station_code, active, created_at, last_active
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (
                name.strip(),
                email.strip().lower(),
                password_hash,
                role.strip().upper(),
                phone.strip(),
                station_code.strip(),
                now_iso,
                now_iso,
            ))
            user_id = cur.lastrowid
            conn.execute("COMMIT")
            return user_id
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def update_user_last_active(user_id: int):
    """Touch user's last_active timestamp."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                UPDATE users SET last_active = ? WHERE id = ?
            """, (now_iso, user_id))
        finally:
            conn.close()


def get_all_users(role: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all users, optionally filtered by role."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            if role:
                cur = conn.execute("""
                    SELECT u.id, u.name, u.email, u.role, u.phone, u.station_code,
                           u.active, u.created_at, u.last_active,
                           COUNT(o.id) as order_count
                    FROM users u
                    LEFT JOIN orders o ON (o.user_id = u.id OR LOWER(o.student_email) = LOWER(u.email))
                    WHERE u.role = ? AND u.active = 1
                    GROUP BY u.id
                    ORDER BY u.id ASC
                """, (role.upper(),))
            else:
                cur = conn.execute("""
                    SELECT u.id, u.name, u.email, u.role, u.phone, u.station_code,
                           u.active, u.created_at, u.last_active,
                           COUNT(o.id) as order_count
                    FROM users u
                    LEFT JOIN orders o ON (o.user_id = u.id OR LOWER(o.student_email) = LOWER(u.email))
                    WHERE u.active = 1
                    GROUP BY u.id
                    ORDER BY u.id ASC
                """)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_admin_metrics() -> Dict[str, Any]:
    """Calculate aggregated high-level business and operational metrics for Admin Dashboard."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            # Order status breakdown
            cur = conn.execute("""
                SELECT
                    COUNT(*) as total_orders,
                    SUM(CASE WHEN status IN ('Waiting', 'Preparing', 'Partially_Ready', 'Ready') THEN 1 ELSE 0 END) as active_orders,
                    SUM(CASE WHEN status IN ('Waiting', 'Preparing') THEN 1 ELSE 0 END) as pending_orders,
                    SUM(CASE WHEN status IN ('Ready', 'Partially_Ready') THEN 1 ELSE 0 END) as ready_orders,
                    SUM(CASE WHEN status IN ('Completed', 'Served') THEN 1 ELSE 0 END) as completed_orders,
                    SUM(CASE WHEN status = 'Cancelled' THEN 1 ELSE 0 END) as cancelled_orders,
                    SUM(CASE WHEN status != 'Cancelled' THEN total ELSE 0 END) as today_revenue
                FROM orders
            """)
            row = cur.fetchone()
            stats = {
                "total_orders": row["total_orders"] or 0,
                "active_orders": row["active_orders"] or 0,
                "pending_orders": row["pending_orders"] or 0,
                "ready_orders": row["ready_orders"] or 0,
                "completed_orders": row["completed_orders"] or 0,
                "cancelled_orders": row["cancelled_orders"] or 0,
                "today_revenue": round(float(row["today_revenue"] or 0.0), 2),
            }

            # Inventory counts
            cur_inv = conn.execute("""
                SELECT
                    COUNT(*) as total_items,
                    SUM(CASE WHEN stock = 0 THEN 1 ELSE 0 END) as out_of_stock,
                    SUM(CASE WHEN stock > 0 AND stock <= 5 THEN 1 ELSE 0 END) as low_stock
                FROM menu_items
                WHERE active = 1
            """)
            inv_row = cur_inv.fetchone()
            stats["total_items"] = inv_row["total_items"] or 0
            stats["out_of_stock"] = inv_row["out_of_stock"] or 0
            stats["low_stock"] = inv_row["low_stock"] or 0

            # Users counts
            cur_u = conn.execute("""
                SELECT
                    SUM(CASE WHEN role = 'CUSTOMER' THEN 1 ELSE 0 END) as total_customers,
                    SUM(CASE WHEN role = 'STAFF' THEN 1 ELSE 0 END) as total_staff
                FROM users
                WHERE active = 1
            """)
            u_row = cur_u.fetchone()
            stats["total_customers"] = u_row["total_customers"] or 0
            stats["total_staff"] = u_row["total_staff"] or 0

            return stats
        finally:
            conn.close()


# ── Food Rating & Recommender Data Layer ──────────────────────────────────

def save_food_rating(user_id: int, food_id: int, rating: int, review: Optional[str] = None) -> Dict[str, Any]:
    """
    Save or update a food rating submitted by an authenticated user.
    Enforces 1..5 star integer range and uniqueness per (user_id, food_id).
    Automatically checks and tags verified purchases.
    """
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        raise ValueError("Rating must be an integer between 1 and 5.")

    now_iso = datetime.now(timezone.utc).isoformat()
    clean_review = (review or "").strip()

    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Verify food exists
            food_cur = conn.execute("SELECT id, name FROM menu_items WHERE id = ?", (food_id,))
            food_row = food_cur.fetchone()
            if not food_row:
                raise ValueError(f"Food item #{food_id} not found.")

            # Check verified purchase
            verified = 1 if is_verified_purchase(conn, user_id, food_id) else 0

            # Upsert rating
            conn.execute("""
                INSERT INTO ratings (
                    user_id, food_id, rating, review, is_verified_purchase, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, food_id) DO UPDATE SET
                    rating = excluded.rating,
                    review = excluded.review,
                    is_verified_purchase = excluded.is_verified_purchase,
                    updated_at = excluded.updated_at
            """, (user_id, food_id, rating, clean_review, verified, now_iso, now_iso))

            conn.execute("COMMIT")

            # Fetch the saved row
            cur = conn.execute("""
                SELECT r.*, m.name as food_name, m.image as food_image, m.price as food_price, m.category as food_category
                FROM ratings r
                JOIN menu_items m ON m.id = r.food_id
                WHERE r.user_id = ? AND r.food_id = ?
            """, (user_id, food_id))
            saved_row = dict(cur.fetchone())

            # Updated rating summary for this item
            saved_row["summary"] = _get_food_rating_summary_conn(conn, food_id)

            # Invalidate recommendation cache so new rating immediately influences recommendations
            try:
                from services.recommendation_service import RecommendationService
                RecommendationService.invalidate_cache()
            except Exception:
                pass

            return saved_row
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def get_user_food_rating(user_id: int, food_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve active rating for a specific user and food item."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            cur = conn.execute("""
                SELECT * FROM ratings WHERE user_id = ? AND food_id = ?
            """, (user_id, food_id))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def get_user_ratings(user_id: int) -> List[Dict[str, Any]]:
    """Retrieve all ratings submitted by a user with food metadata."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            cur = conn.execute("""
                SELECT
                    r.id, r.user_id, r.food_id, r.rating, r.review, r.is_verified_purchase,
                    r.created_at, r.updated_at,
                    m.name as food_name, m.price as food_price, m.category as food_category,
                    m.image as food_image, m.stock as food_stock, m.station_code
                FROM ratings r
                JOIN menu_items m ON m.id = r.food_id
                WHERE r.user_id = ?
                ORDER BY r.updated_at DESC
            """, (user_id,))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def delete_food_rating(user_id: int, rating_id: int) -> bool:
    """Delete a user's rating, enforcing ownership."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("DELETE FROM ratings WHERE id = ? AND user_id = ?", (rating_id, user_id))
            deleted = cur.rowcount > 0
            conn.execute("COMMIT")

            if deleted:
                try:
                    from services.recommendation_service import RecommendationService
                    RecommendationService.invalidate_cache()
                except Exception:
                    pass

            return deleted
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _get_food_rating_summary_conn(conn: sqlite3.Connection, food_id: int) -> Dict[str, Any]:
    """Helper to compute food rating summary using an active connection."""
    agg_cur = conn.execute("""
        SELECT
            AVG(rating) as avg_rating,
            COUNT(*) as total_count
        FROM ratings
        WHERE food_id = ?
    """, (food_id,))
    agg = agg_cur.fetchone()

    total_count = agg["total_count"] or 0
    avg_rating = round(float(agg["avg_rating"]), 1) if agg["avg_rating"] is not None else None

    # Star distribution
    dist_cur = conn.execute("""
        SELECT rating, COUNT(*) as cnt
        FROM ratings
        WHERE food_id = ?
        GROUP BY rating
    """, (food_id,))
    distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for row in dist_cur.fetchall():
        distribution[row["rating"]] = row["cnt"]

    # Recent reviews (with reviewer name)
    rev_cur = conn.execute("""
        SELECT r.id, r.rating, r.review, r.is_verified_purchase, r.updated_at,
               u.name as user_name
        FROM ratings r
        JOIN users u ON u.id = r.user_id
        WHERE r.food_id = ?
        ORDER BY r.updated_at DESC
        LIMIT 10
    """, (food_id,))
    reviews = [dict(r) for r in rev_cur.fetchall()]

    return {
        "food_id": food_id,
        "average_rating": avg_rating,
        "rating_count": total_count,
        "star_distribution": distribution,
        "reviews": reviews,
    }


def get_food_rating_summary(food_id: int) -> Dict[str, Any]:
    """Calculate real average, count, distribution and reviews for a food item."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            return _get_food_rating_summary_conn(conn, food_id)
        finally:
            conn.close()


def get_all_food_ratings_summary() -> Dict[int, Dict[str, Any]]:
    """Return dictionary of food_id -> {average_rating, rating_count} in single SQL query."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            cur = conn.execute("""
                SELECT
                    food_id,
                    AVG(rating) as avg_rating,
                    COUNT(*) as total_count
                FROM ratings
                GROUP BY food_id
            """)
            summaries = {}
            for row in cur.fetchall():
                summaries[row["food_id"]] = {
                    "average_rating": round(float(row["avg_rating"]), 1) if row["avg_rating"] is not None else None,
                    "rating_count": row["total_count"] or 0,
                }
            return summaries
        finally:
            conn.close()


def get_top_rated_foods(limit: int = 10, min_ratings: int = 1) -> List[Dict[str, Any]]:
    """
    Return top-rated foods ranked by Bayesian smoothed rating:
    BayesScore = (C * m + sum(ratings)) / (C + count)
    where C = 3, m = 3.5.
    This prevents 1-review 5-star items from artificially outranking items with many 4.5-star reviews.
    """
    C = 3.0
    m = 3.5
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            cur = conn.execute("""
                SELECT
                    m.id, m.name, m.price, m.category, m.station_code,
                    m.stock, m.image, m.description, m.badge,
                    COUNT(r.id) as rating_count,
                    COALESCE(AVG(r.rating), 0.0) as average_rating,
                    COALESCE(SUM(r.rating), 0.0) as sum_ratings
                FROM menu_items m
                LEFT JOIN ratings r ON r.food_id = m.id
                WHERE m.active = 1
                GROUP BY m.id
                HAVING COUNT(r.id) >= ?
            """, (min_ratings,))
            rows = [dict(r) for r in cur.fetchall()]

            for item in rows:
                n = item["rating_count"]
                s = item["sum_ratings"]
                bayes_score = (C * m + s) / (C + n)
                item["bayes_score"] = round(bayes_score, 3)
                item["average_rating"] = round(float(item["average_rating"]), 1) if n > 0 else None
                item["is_available"] = item["stock"] > 0

            rows.sort(key=lambda x: (x["bayes_score"], x["rating_count"]), reverse=True)
            return rows[:limit]
        finally:
            conn.close()


def get_food_details(food_id: int, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Retrieve complete food item details including rating summary and user's own rating."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            cur = conn.execute("""
                SELECT id, name, price, category, station_code, prep_time_minutes,
                       stock, initial_stock, image, badge, description, active
                FROM menu_items
                WHERE id = ?
            """, (food_id,))
            row = cur.fetchone()
            if not row:
                return None

            food = dict(row)
            food["is_available"] = food["stock"] > 0
            food["rating_summary"] = _get_food_rating_summary_conn(conn, food_id)
            food["average_rating"] = food["rating_summary"]["average_rating"]
            food["rating_count"] = food["rating_summary"]["rating_count"]

            user_rating = None
            is_verified = False
            if user_id:
                ur_cur = conn.execute("""
                    SELECT id, rating, review, is_verified_purchase, updated_at
                    FROM ratings
                    WHERE user_id = ? AND food_id = ?
                """, (user_id, food_id))
                ur_row = ur_cur.fetchone()
                if ur_row:
                    user_rating = dict(ur_row)
                is_verified = is_verified_purchase(conn, user_id, food_id)

            food["user_rating"] = user_rating
            food["is_user_verified_purchase"] = is_verified
            return food
        finally:
            conn.close()


def save_user_preferences(user_id: int, preferred_categories: List[str], dietary_preference: str = 'ALL') -> Dict[str, Any]:
    """Save user onboarding food preferences for cold-start personalization."""
    now_iso = datetime.now(timezone.utc).isoformat()
    json_cats = json.dumps(preferred_categories or [])
    clean_diet = dietary_preference.upper() if dietary_preference in ['VEG', 'NON_VEG', 'ALL'] else 'ALL'

    with _DB_LOCK:
        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT INTO user_preferences (user_id, preferred_categories, dietary_preference, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    preferred_categories = excluded.preferred_categories,
                    dietary_preference = excluded.dietary_preference,
                    updated_at = excluded.updated_at
            """, (user_id, json_cats, clean_diet, now_iso, now_iso))
            conn.execute("COMMIT")

            try:
                from services.recommendation_service import RecommendationService
                RecommendationService.invalidate_cache()
            except Exception:
                pass

            return {
                "user_id": user_id,
                "preferred_categories": preferred_categories,
                "dietary_preference": clean_diet,
                "updated_at": now_iso,
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def get_user_preferences(user_id: int) -> Dict[str, Any]:
    """Fetch user's saved food preferences."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            cur = conn.execute("SELECT * FROM user_preferences WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            if row:
                return {
                    "user_id": user_id,
                    "preferred_categories": json.loads(row["preferred_categories"] or "[]"),
                    "dietary_preference": row["dietary_preference"] or "ALL",
                    "updated_at": row["updated_at"],
                }
            return {
                "user_id": user_id,
                "preferred_categories": [],
                "dietary_preference": "ALL",
                "updated_at": None,
            }
        finally:
            conn.close()


def get_recommendation_admin_metrics() -> Dict[str, Any]:
    """Calculate aggregated ratings and recommender metrics for the Admin Dashboard."""
    with _DB_LOCK:
        conn = get_db_connection()
        try:
            # Total ratings and global average
            cur_r = conn.execute("""
                SELECT
                    COUNT(*) as total_ratings,
                    AVG(rating) as global_avg_rating,
                    COUNT(DISTINCT user_id) as users_with_ratings,
                    COUNT(DISTINCT food_id) as foods_with_ratings
                FROM ratings
            """)
            r_row = cur_r.fetchone()
            tot_ratings = r_row["total_ratings"] or 0
            global_avg = round(float(r_row["global_avg_rating"]), 2) if r_row["global_avg_rating"] is not None else 0.0

            # Rating distribution (1 to 5 stars)
            cur_dist = conn.execute("""
                SELECT rating, COUNT(*) as cnt
                FROM ratings
                GROUP BY rating
            """)
            distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
            for row in cur_dist.fetchall():
                distribution[row["rating"]] = row["cnt"]

            # Most rated foods
            cur_most_rated = conn.execute("""
                SELECT m.id, m.name, m.category, COUNT(r.id) as rating_count,
                       ROUND(AVG(r.rating), 1) as avg_rating
                FROM menu_items m
                JOIN ratings r ON r.food_id = m.id
                GROUP BY m.id
                ORDER BY rating_count DESC, avg_rating DESC
                LIMIT 5
            """)
            most_rated = [dict(r) for r in cur_most_rated.fetchall()]

            # Highest rated foods (min 2 ratings)
            cur_high_rated = conn.execute("""
                SELECT m.id, m.name, m.category, COUNT(r.id) as rating_count,
                       ROUND(AVG(r.rating), 1) as avg_rating
                FROM menu_items m
                JOIN ratings r ON r.food_id = m.id
                GROUP BY m.id
                HAVING COUNT(r.id) >= 2
                ORDER BY avg_rating DESC, rating_count DESC
                LIMIT 5
            """)
            highest_rated = [dict(r) for r in cur_high_rated.fetchall()]

            # Items with insufficient ratings (< 2 ratings)
            cur_sparse = conn.execute("""
                SELECT m.id, m.name, m.category, COUNT(r.id) as rating_count
                FROM menu_items m
                LEFT JOIN ratings r ON r.food_id = m.id
                WHERE m.active = 1
                GROUP BY m.id
                HAVING COUNT(r.id) < 2
                ORDER BY rating_count ASC, m.name ASC
            """)
            sparse_items = [dict(r) for r in cur_sparse.fetchall()]

            return {
                "total_ratings": tot_ratings,
                "global_average_rating": global_avg,
                "users_with_ratings": r_row["users_with_ratings"] or 0,
                "foods_with_ratings": r_row["foods_with_ratings"] or 0,
                "star_distribution": distribution,
                "most_rated_items": most_rated,
                "highest_rated_items": highest_rated,
                "sparse_items_count": len(sparse_items),
                "sparse_items": sparse_items[:10],
            }
        finally:
            conn.close()

