# SMART CANTEEN ORDERING & TOKEN SYSTEM
**Hackathon PS F2 · Round 2 Production Implementation**
*Fresh. Fast. Traditional.*

A high-performance digital ordering and token management system for college canteens. Built with **Round 2 concurrency guarantees**, **parallel station-wise queues**, **dynamic ETA scheduling**, **order editing with +5min penalty**, **instant order cancellation with automated inventory restoration**, and **real-time Server-Sent Events (SSE)**.

---

## 🏛️ System Architecture

```
project/
├── payment_stub.py             # 🔒 LOCKED - Organizer-provided simulator (never modified)
├── config.py                   # Central settings: stations, edit penalties, tokens, DB paths
├── database.py                 # SQLite WAL mode, BEGIN IMMEDIATE transactions & row locks
├── core/
│   ├── __init__.py             # Exposes core classes & scheduler utilities
│   ├── token_gen.py            # Thread-safe deterministic TokenGenerator
│   ├── order_queue.py          # Singly-linked list FIFO queue
│   └── scheduler.py            # Dynamic ETA scheduling & station queue re-sorter
├── services/
│   ├── __init__.py             # Service layer exports
│   ├── payment_adapter.py      # Adapter for locked payment_stub (timeouts & refunds)
│   ├── event_broker.py         # Real-time SSE Pub/Sub broker (zero page reloads)
│   └── order_service.py        # Atomic order creation, editing, cancellation, advance
├── app.py                      # Flask backend API & real-time SSE stream
├── tests/
│   └── test_smart_canteen.py   # Automated test suite (11 test cases covering all 17 scenarios)
├── static/
│   ├── user.html               # Student web app (Menu, Cart, Multi-Station Tracking, Edit, Cancel)
│   ├── staff.html              # Kitchen dashboard (Station Queues, Live Board, Inventory Control)
│   ├── style.css               # Modern fast-food restaurant design
│   ├── app.js                  # Vanilla JS frontend with real-time SSE listener
│   └── images/                 # 100% offline local authentic food photographs
└── README.md
```

---

## ⚡ Round 2 Concurrency Guarantee: Last-Item Race Condition

### The Problem
When only one item remains (`Burger stock = 1`), two students attempt to purchase it simultaneously. A naive system allows both transactions to read `stock == 1`, decrement to 0, and confirm two orders—leaving negative inventory (`-1`) or selling non-existent food.

### The Solution
The system uses a **two-tier atomic reservation mechanism**:

1. **Process-Level Mutex (`threading.RLock`)**:
   Serializes concurrent threads entering checkout within the application layer.

2. **Database Row-Level Atomic Reservation (`SQLite WAL mode + BEGIN IMMEDIATE`)**:
   ```sql
   UPDATE menu_items
   SET stock = stock - :requested_qty
   WHERE id = :item_id AND stock >= :requested_qty;
   ```
   - If `cursor.rowcount == 1`: The item was successfully reserved.
   - If `cursor.rowcount == 0`: Stock was already depleted by another transaction. The transaction raises `OutOfStockError` and executes an immediate `ROLLBACK`.

3. **Guarantees**:
   - Zero negative inventory (`stock >= 0` invariant strictly enforced).
   - Zero duplicate tokens (`token_sequence` table updated inside transaction).
   - Proven by multithreaded unit test `test_03_and_04_last_item_race_and_no_negative_stock`.

---

## 🍳 Parallel Station-Wise Queues

The canteen is partitioned into independent preparation stations:
1. **MAIN DISH** (`MAIN_DISH`): Idli, Masala Dosa, Pongal, Poori Masala, Sambar Rice, Biryani, Veg Meals, Parotta.
2. **SNACKS** (`SNACKS`): Vada, Samosa, Chicken 65, Burger, Sandwich.
3. **BEVERAGES** (`BEVERAGES`): Tea, Filter Coffee, Fresh Lime Juice.

### Independent Execution
- `Token 1 -> Biryani (MAIN DISH)`
- `Token 2 -> Tea (BEVERAGES)`

**Token 2 does NOT wait for Token 1.** Each station maintains its own independent queue and cooks simultaneously.

### Multi-Station Orders
If a customer orders `Biryani + Samosa + Tea`:
- The order creates 3 separate `station_tasks` across `MAIN_DISH`, `SNACKS`, and `BEVERAGES`.
- Each station prepares its component independently.
- The student tracks each station's status and ETA.
- Overall order status moves: `Waiting` ➔ `Preparing` ➔ `Partially_Ready` ➔ `Ready` (when all station tasks are ready) ➔ `Completed`.

---

## ⏱️ Dynamic Scheduling: Token Number ≠ Permanent Queue Position

A token (e.g. `T-001`) is a **customer reference identifier**, NOT a permanent queue position.

### Queue Sorting Invariants:
1. Filters out `Cancelled` and `Completed/Served` orders.
2. Evaluates active tasks (`Waiting`, `Preparing`).
3. **Primary Sort**: Current estimated ready time (`estimated_ready_time` ASC).
4. **Deterministic Tie-breaker**: Creation timestamp (`created_at` ASC), then `task_id` ASC.

### Editing Penalty (+5 Minutes) & Dynamic Priority Flip:
Whenever a customer edits an active order:
$$\text{New ETA} = \text{Recalculated ETA} + \text{EDIT\_EXTRA\_MINUTES (5 mins)}$$

#### Example:
- **T1** (Tea): Ready at `1:00 PM`
- **T2** (Tea): Ready at `1:03 PM`
- Customer edits **T1**.
- System adds +5 minutes penalty: **T1 New ETA = 1:05 PM**.
- **The station queue automatically re-orders**:
  - **T2** (`1:03 PM`) ➔ **#1 (Cooked & Served First)**
  - **T1** (`1:05 PM`) ➔ **#2 (Cooked & Served Second)**
- Token numbers remain `T1` and `T2` (no duplicate customer records created).

---

## ❌ Order Cancellation & Instant Refund

- **Allowed Window**: `Waiting` or `Preparing`.
- **Blocked Window**: If food is `Ready` or `Completed`, backend strictly rejects cancellation with HTTP 400.
- **Atomic Actions on Cancellation**:
  1. Order & station task statuses updated to `Cancelled`.
  2. Task is immediately removed from the active station queue.
  3. The next eligible order immediately takes priority.
  4. Food inventory is restored to the kitchen (`UPDATE menu_items SET stock = stock + qty`).
  5. Automated refund processed through `payment_stub.refund(transaction_id)`.
  6. Live update event broadcast to all screens.

---

## 🔒 Locked Payment Stub Integration

Integrates with `payment_stub.py` via `services/payment_adapter.py`:
- **Timeout Reconcile**: Automatically calls `payment_stub.verify(txn_id)` to settle `TIMEOUT` or `CAPTURED_UNCONFIRMED` states into `CAPTURED`.
- **Declined Handling**: If payment fails, order and tokens are NOT created, and reserved stock is immediately rolled back.
- **Refund Integration**: Calls `payment_stub.refund(...)` on order cancellation.

---

## 📡 Real-Time Live Updates (No Page Refreshes)

Uses standard **Server-Sent Events (SSE)** at `/api/events`:
- Supported natively in browser vanilla JS (`EventSource`) with zero third-party dependencies.
- Events emitted: `order_created`, `order_edited`, `order_cancelled`, `task_advanced`, `queue_reordered`, `stock_updated`.
- Connected screens (Student UI, Live Board, Staff Kitchen) automatically re-render upon event receipt.

---

## 🚀 Setup & Running Instructions

### 1. Requirements
- Python 3.8+ (standard library + `flask`, `pytest`)

```bash
pip install flask pytest
```

### 2. Run Automated Test Suite
```bash
python -m pytest tests/test_smart_canteen.py -v
```
*(All 11 tests verifying all 17 scenarios will run and pass.)*

### 3. Start Application Server
```bash
python app.py
```
Server runs at **http://localhost:5000**

- **Student Ordering App**: [http://localhost:5000/](http://localhost:5000/)
- **Canteen Kitchen Command Panel**: [http://localhost:5000/staff](http://localhost:5000/staff)

---

## 🎯 Judge Demonstration Walkthrough

### DEMO 1 — Last-Item Race Condition
1. Open Staff Panel ([http://localhost:5000/staff](http://localhost:5000/staff)).
2. Under **Live Inventory**, click **"⚡ Set Burger Stock = 1"**.
3. Open two separate student browser tabs at [http://localhost:5000/](http://localhost:5000/).
4. In both tabs, add Burger to cart and click Checkout simultaneously.
5. **Expected Result**: Exactly ONE tab succeeds and gets a token. The other tab immediately displays `"Burger is sold out!"`. Stock becomes 0 (never negative).

### DEMO 2 — Parallel Stations Independent Processing
1. Student 1 orders **Chicken Biriyani** (`MAIN DISH`).
2. Student 2 orders **Tea** (`BEVERAGES`).
3. Check Staff Panel ([http://localhost:5000/staff](http://localhost:5000/staff)).
4. Click **Beverages** tab: Student 2's Tea is at **#1** and can be marked **Ready** immediately, completely independent of the Biryani cooking in Main Dish.

### DEMO 3 — Order Cancellation
1. Place an order for **Masala Dosa**.
2. Note the token (e.g. `T-003`).
3. Go to Track page: click **"❌ Cancel Order & Refund"**.
4. Confirm cancellation:
   - Order marked **Cancelled**.
   - Food inventory restored immediately.
   - Staff queue automatically advances the next order to #1.

### DEMO 4 — Edit Re-Scheduling (+5 Min Penalty & Priority Flip)
1. Order **Tea** (Token A) ➔ ETA: ~3 mins.
2. Order **Tea** (Token B) ➔ ETA: ~6 mins (Token A is #1, Token B is #2).
3. On Token A's tracking view, click **"✏️ Edit Items"** and add another dish.
4. Save the edit:
   - +5 minutes preparation penalty is added to Token A (New ETA: ~8 mins).
   - In Beverages queue, **Token B automatically flips to #1**, and **Token A moves to #2**.
   - Token numbers remain unchanged.

### DEMO 5 — Multi-Station Order
1. In Student App, place an order with: **Chicken Biriyani + Samosa + Tea**.
2. On Tracking page, view **Station-Wise Preparation Status**:
   - 🍛 Main Dish: Biryani (Waiting/Preparing)
   - 🍟 Snacks: Samosa (Waiting/Preparing)
   - 🥤 Beverages: Tea (Waiting/Preparing)
3. Open Staff Panel, advance Beverages to Ready.
4. Student tracking immediately displays Beverages as `Ready`, while Main Dish remains `Preparing`. Overall status becomes `Partially_Ready` until all stations complete!

---

## 🔐 Authentication & Role-Based Access Control (RBAC)

The system features robust authentication and role segregation:

### 1. Default System Accounts

| Role | Email | Password | Description | Access Rights |
| :--- | :--- | :--- | :--- | :--- |
| **ADMIN** | `admin@smartcanteen.com` | `Admin@123` | Head Administrator | Full Admin Dashboard, Staff roster, Inventory controls, Orders, Reports |
| **STAFF** | `staff@smartcanteen.com` | `Staff@123` | Chef Murugan | Kitchen Station operations, Live Token Board, Staff Panel |
| **CUSTOMER** | `student@smartcanteen.com` | `Student@123` | Karthik Raja | Browse menu, Add to cart, Checkout, Payment, Token tracking, My Orders |

### 2. Guest User Browsing vs Order Protection
- **Public Guests**: Can freely browse Home, Menu, Tamil Nadu dishes, prices, offers, search items, and view live stock availability.
- **Ordering Protected**: Attempting to add an item to cart, proceed to checkout, make payment, or generate a token prompts:
  > *"Sign in required — Please sign in to place an order."*
  with a direct `[ Sign In ]` button.
- **Backend Enforced**: `POST /api/checkout` returns `401 Unauthorized` with `login_required: true` if an unauthenticated client bypasses frontend controls.

### 3. Dedicated Admin Login & Role Protection
- **Dedicated Admin Login**: [http://localhost:5000/admin/login](http://localhost:5000/admin/login)
- **Generic Security Rejection**: Invalid credentials return generic `"Invalid admin credentials."` without disclosing whether the email or password was wrong.
- **Server-Side `@admin_required`**: Normal students or staff cannot access `/admin` or `/api/admin/...` APIs by URL manipulation (returns `403 Forbidden` / redirects to login).

### 4. Colourful Admin Dashboard ([http://localhost:5000/admin](http://localhost:5000/admin))
- **Overview Cards**: Total Orders, Active Orders, Pending Orders, Ready Orders, Today's Revenue.
- **Live Operations (3 Parallel Stations)**: Real-time Cooking, Next, and Ready queues for Main Dish (🍛), Snacks (🍟), and Beverages (🥤) with interactive advance controls.
- **Order Management**: Comprehensive filterable database of orders with tokens, students, dishes, stations, payment, and ETAs.
- **Menu & Inventory**: Live stock levels with one-click stock override buttons for instant testing.
- **Live Token Board**: TV/Kiosk multi-station live board streaming via Server-Sent Events (no page reload required).
- **Users & Staff**: Student accounts with order histories and staff duty rosters.

---

## 🧪 Comprehensive Automated Test Suite

Run the full automated test suite covering all 17 core requirements plus authentication and admin role protection:

```bash
python -m pytest tests/ -v
```

All **20 test cases pass (100% green)**:
- `test_guest_can_browse_menu`
- `test_guest_cannot_checkout`
- `test_customer_login_and_checkout`
- `test_invalid_login_credentials`
- `test_admin_login_success`
- `test_admin_login_invalid_credentials_generic_message`
- `test_student_cannot_access_admin_endpoints`
- `test_admin_can_access_admin_endpoints`
- `test_logout_clears_session`
- `test_01_normal_order`
- `test_02_token_uniqueness`
- `test_03_and_04_last_item_race_and_no_negative_stock`
- `test_05_station_separation`
- `test_06_parallel_station_processing`
- `test_07_and_08_cancellation_removes_queue_task_and_advances_next`
- `test_09_to_12_editing_order_penalty_and_reorder`
- `test_13_editing_unavailable_item_fails_safely`
- `test_14_and_15_editing_and_cancellation_after_completion_rejected`
- `test_16_multi_station_order_creates_multiple_tasks`
- `test_17_live_update_event_emitted`

