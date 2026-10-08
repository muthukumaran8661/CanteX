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

