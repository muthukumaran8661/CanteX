"""
tests/test_auth_and_admin.py
============================
Automated test suite verifying:
1. Guest access to menu, offers, and tracking.
2. Guest blocked from checkout with 401 and login_required flag.
3. Customer sign-in & registration.
4. Authenticated customer checkout success.
5. Admin login with valid credentials.
6. Admin login rejected with generic error on invalid credentials or non-admin user.
7. Admin dashboard & API route protection (normal user blocked from admin).
8. Admin can access admin metrics and management endpoints.
9. Logout clears session and restores guest state.
"""

import pytest
from app import app
from config import (
    DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_STAFF_EMAIL,
    DEFAULT_STAFF_PASSWORD,
    DEFAULT_STUDENT_EMAIL,
    DEFAULT_STUDENT_PASSWORD,
)
from database import init_db


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_guest_can_browse_menu(client):
    """Guest can view menu items without logging in."""
    res = client.get("/api/menu")
    assert res.status_code == 200
    items = res.get_json()
    assert len(items) > 0


def test_guest_cannot_checkout(client):
    """Guest user MUST NOT be allowed to place an order."""
    payload = {
        "items": [{"id": 1, "name": "Idli", "qty": 1, "price": 30.0}],
        "method": "UPI",
    }
    res = client.post("/api/checkout", json=payload)
    assert res.status_code == 401
    data = res.get_json()
    assert data["login_required"] is True
    assert "Please sign in to place an order" in data["message"]


def test_customer_login_and_checkout(client):
    """Signed-in student can checkout successfully."""
    # 1. Login
    login_res = client.post("/api/auth/login", json={
        "email": DEFAULT_STUDENT_EMAIL,
        "password": DEFAULT_STUDENT_PASSWORD,
    })
    assert login_res.status_code == 200
    assert login_res.get_json()["success"] is True

    # 2. Checkout
    payload = {
        "items": [{"id": 1, "name": "Idli", "qty": 1, "price": 30.0}],
        "method": "UPI",
    }
    chk_res = client.post("/api/checkout", json=payload)
    assert chk_res.status_code == 200
    data = chk_res.get_json()
    assert data["success"] is True
    assert "token" in data
    assert data["token"].startswith("T-")


def test_invalid_login_credentials(client):
    """Invalid login returns 401."""
    res = client.post("/api/auth/login", json={
        "email": "nonexistent@test.com",
        "password": "WrongPassword",
    })
    assert res.status_code == 401
    assert res.get_json()["success"] is False


def test_admin_login_success(client):
    """Valid admin credentials allow login."""
    res = client.post("/api/auth/admin-login", json={
        "email": DEFAULT_ADMIN_EMAIL,
        "password": DEFAULT_ADMIN_PASSWORD,
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["redirect"] == "/admin"


def test_admin_login_invalid_credentials_generic_message(client):
    """Invalid admin credentials return generic message without leaking details."""
    res = client.post("/api/auth/admin-login", json={
        "email": DEFAULT_ADMIN_EMAIL,
        "password": "WrongPassword123",
    })
    assert res.status_code == 401
    assert res.get_json()["message"] == "Invalid admin credentials."

    # Student trying admin login
    res2 = client.post("/api/auth/admin-login", json={
        "email": DEFAULT_STUDENT_EMAIL,
        "password": DEFAULT_STUDENT_PASSWORD,
    })
    assert res2.status_code == 401
    assert res2.get_json()["message"] == "Invalid admin credentials."


def test_student_cannot_access_admin_endpoints(client):
    """Normal customer must NOT be able to access admin dashboard or admin APIs."""
    # Login as student
    client.post("/api/auth/login", json={
        "email": DEFAULT_STUDENT_EMAIL,
        "password": DEFAULT_STUDENT_PASSWORD,
    })

    # Access admin metrics API
    res = client.get("/api/admin/metrics")
    assert res.status_code == 403
    assert "Admin privileges required" in res.get_json()["message"]

    # Access admin users API
    res_users = client.get("/api/admin/users")
    assert res_users.status_code == 403

    # Access admin page -> redirected to login
    res_page = client.get("/admin")
    assert res_page.status_code == 302
    assert "/admin/login" in res_page.headers["Location"]


def test_admin_can_access_admin_endpoints(client):
    """Authenticated Admin can access admin APIs and metrics."""
    # Login as admin
    client.post("/api/auth/admin-login", json={
        "email": DEFAULT_ADMIN_EMAIL,
        "password": DEFAULT_ADMIN_PASSWORD,
    })

    # Metrics
    res = client.get("/api/admin/metrics")
    assert res.status_code == 200
    m = res.get_json()
    assert "total_orders" in m
    assert "today_revenue" in m

    # Users
    res_u = client.get("/api/admin/users")
    assert res_u.status_code == 200

    # Staff
    res_s = client.get("/api/admin/staff")
    assert res_s.status_code == 200


def test_logout_clears_session(client):
    """Logout clears session and restricts order placement again."""
    client.post("/api/auth/login", json={
        "email": DEFAULT_STUDENT_EMAIL,
        "password": DEFAULT_STUDENT_PASSWORD,
    })

    # Verify signed in
    me_res = client.get("/api/auth/me")
    assert me_res.get_json()["authenticated"] is True

    # Logout
    logout_res = client.post("/api/auth/logout")
    assert logout_res.status_code == 200

    # Verify guest again
    me_res2 = client.get("/api/auth/me")
    assert me_res2.get_json()["authenticated"] is False

    # Checkout now blocked
    chk_res = client.post("/api/checkout", json={"items": [{"id": 1, "qty": 1}], "method": "UPI"})
    assert chk_res.status_code == 401
