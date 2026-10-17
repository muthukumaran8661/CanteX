# config.py
# ==========
# Central configuration for the Smart Canteen project.
# Import any setting from here rather than hardcoding it elsewhere.

import os

# ── Base paths ────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.path.join(BASE_DIR, "data")
DATABASE_PATH = os.path.join(DATA_DIR, "canteen.db")
ORDERS_FILE   = os.path.join(DATA_DIR, "orders.json")
MENU_FILE     = os.path.join(BASE_DIR, "menu.json")

# ── Token generation ──────────────────────────────────────────────────────
TOKEN_PREFIX  = "T-"     # Prepended to every generated token (e.g. "T-001")
TOKEN_PADDING = 3        # Zero-pad width; "T-001" .. "T-999" then "T-1000"

# ── Dynamic Scheduling & Edit Penalty ─────────────────────────────────────
# Additional preparation penalty added whenever a customer edits an active order
EDIT_EXTRA_MINUTES = 5

# ── Stations ──────────────────────────────────────────────────────────────
# Independent preparation stations in the canteen
STATION_MAIN_DISH = "MAIN_DISH"
STATION_SNACKS    = "SNACKS"
STATION_BEVERAGES = "BEVERAGES"

DEFAULT_STATIONS = [
    {
        "code": STATION_MAIN_DISH,
        "name": "Main Dish Station",
        "icon": "🍛",
        "display_order": 1,
    },
    {
        "code": STATION_SNACKS,
        "name": "Snacks Station",
        "icon": "🍟",
        "display_order": 2,
    },
    {
        "code": STATION_BEVERAGES,
        "name": "Beverages Station",
        "icon": "🥤",
        "display_order": 3,
    },
]

# ── Order and Task Statuses (Single source of truth) ──────────────────────
STATUS_WAITING         = "Waiting"
STATUS_PREPARING       = "Preparing"
STATUS_PARTIALLY_READY = "Partially_Ready"
STATUS_READY           = "Ready"
STATUS_COMPLETED       = "Completed"
STATUS_SERVED          = "Served"       # Treated synonymously with Completed
STATUS_CANCELLED       = "Cancelled"

VALID_ORDER_STATUSES = [
    STATUS_WAITING,
    STATUS_PREPARING,
    STATUS_PARTIALLY_READY,
    STATUS_READY,
    STATUS_COMPLETED,
    STATUS_SERVED,
    STATUS_CANCELLED,
]

VALID_STATUSES = VALID_ORDER_STATUSES  # Backward compatibility alias

VALID_TASK_STATUSES = [
    STATUS_WAITING,
    STATUS_PREPARING,
    STATUS_READY,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
]

# Next progression for station preparation tasks
NEXT_TASK_STATUS = {
    STATUS_WAITING:   STATUS_PREPARING,
    STATUS_PREPARING: STATUS_READY,
    STATUS_READY:     STATUS_COMPLETED,
}

# ── Payment ───────────────────────────────────────────────────────────────
PAYMENT_METHOD = "UPI"

# ── Authentication & Role Constants ────────────────────────────────────────
ROLE_CUSTOMER = "CUSTOMER"
ROLE_STAFF    = "STAFF"
ROLE_ADMIN    = "ADMIN"

DEFAULT_ADMIN_EMAIL    = "admin@smartcanteen.com"
DEFAULT_ADMIN_PASSWORD = "Admin@123"
DEFAULT_ADMIN_NAME     = "Head Administrator"

DEFAULT_STAFF_EMAIL    = "staff@smartcanteen.com"
DEFAULT_STAFF_PASSWORD = "Staff@123"
DEFAULT_STAFF_NAME     = "Chef Murugan"

DEFAULT_STUDENT_EMAIL    = "student@smartcanteen.com"
DEFAULT_STUDENT_PASSWORD = "Student@123"
DEFAULT_STUDENT_NAME     = "Karthik Raja"

SECRET_KEY = os.environ.get("SECRET_KEY", "smart-canteen-auth-secure-secret-key-2026")

# ── Recommendation Engine Configuration ─────────────────────────────────
REC_WEIGHT_ITEM_SIMILARITY   = 0.40   # Weight for Item-to-Item collaborative filtering (explicit ratings)
REC_WEIGHT_USER_RATINGS      = 0.25   # Weight for user explicit ratings & predicted preference
REC_WEIGHT_ITEM_QUALITY      = 0.15   # Weight for Bayesian smoothed average rating & reliability
REC_WEIGHT_COOCCURRENCE       = 0.10   # Weight for co-occurrence frequency in orders
REC_WEIGHT_POPULARITY        = 0.10   # Weight for global order popularity
REC_RECENCY_DECAY_DAYS       = 7.0    # Recency decay timescale in days
REC_CACHE_TTL_SECONDS        = 300    # In-memory similarity cache TTL in seconds
REC_DEFAULT_TOP_K            = 6      # Default recommendations limit
REC_MAX_PER_CATEGORY         = 2      # Max items per category for diversity re-ranking
REC_BAYESIAN_PRIOR_RATING    = 3.5    # Prior mean rating for Bayesian average smoothing
REC_BAYESIAN_PRIOR_COUNT     = 3      # Prior weight/count for Bayesian smoothing (avoids 1-rating 5-star distortion)

# ── Flask server ──────────────────────────────────────────────────────────
FLASK_HOST  = "0.0.0.0"
FLASK_PORT  = 5000
FLASK_DEBUG = True


