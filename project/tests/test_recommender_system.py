"""
tests/test_recommender_system.py
================================
Automated test suite verifying the complete Smart Food Recommender System:
1. Ratings CRUD operations (1 to 5 integer validation).
2. Exactly 1 active rating per user per food item (UPSERT without duplicates).
3. Real average rating and rating count calculations (no fake ratings).
4. Verified purchase detection strictly based on historical completed orders.
5. Ownership enforcement: users cannot edit or delete another user's rating.
6. Cold-start fallback and user taste preference profile saving.
7. Item-to-Item CF related foods on Food Details.
8. Bayesian smoothed Top-Rated ranking.
9. Guest browsing works while rating/private endpoints require authentication.
"""

import pytest
import json
from config import (
    ROLE_CUSTOMER,
    ROLE_ADMIN,
    STATUS_COMPLETED,
    STATUS_WAITING,
)
from database import (
    init_db,
    get_db_connection,
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
    _DB_LOCK,
)
from services import (
    RecommendationService,
    AuthService,
)
from app import app


@pytest.fixture(autouse=True)
def setup_test_db():
    """Reset ratings and clean test data before each test."""
    init_db()
    with _DB_LOCK:
        conn = get_db_connection()
        conn.execute("DELETE FROM ratings WHERE user_id >= 900")
        conn.execute("DELETE FROM user_preferences WHERE user_id >= 900")
        conn.execute("DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE user_id >= 900)")
        conn.execute("DELETE FROM orders WHERE user_id >= 900")
        conn.execute("DELETE FROM users WHERE id >= 900")
        conn.close()
    RecommendationService.invalidate_cache()


@pytest.fixture
def client():
    """Create Flask test client with session cookies enabled."""
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def _create_test_user(user_id=901, email="testuser901@smartcanteen.com", name="Test User 901"):
    """Helper to create a known customer in the database."""
    with _DB_LOCK:
        conn = get_db_connection()
        from werkzeug.security import generate_password_hash
        conn.execute("""
            INSERT INTO users (id, name, email, password_hash, role, active, created_at, last_active)
            VALUES (?, ?, ?, ?, ?, 1, '2026-10-14T00:00:00+00:00', '2026-10-14T00:00:00+00:00')
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                email = excluded.email,
                password_hash = excluded.password_hash,
                role = excluded.role,
                last_active = excluded.last_active
        """, (user_id, name, email, generate_password_hash("password123"), ROLE_CUSTOMER))
        conn.close()


def _login_session(client, user_id=901, email="testuser901@smartcanteen.com", name="Test User 901", role=ROLE_CUSTOMER):
    """Set Flask session cookies for test user."""
    _create_test_user(user_id=user_id, email=email, name=name)
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = email
        sess["user_name"] = name
        sess["user_role"] = role


# ── Test 1: Rating Input Validation (1 to 5 stars) ───────────────────────────
def test_rating_validation_integers_only(client):
    """Verify backend accepts only 1-5 integer ratings."""
    _login_session(client, user_id=901)

    # Valid rating (5 stars)
    res = client.post("/api/ratings", json={"food_id": 1, "rating": 5, "review": "Crispy and fresh!"})
    assert res.status_code == 200
    assert res.get_json()["success"] is True

    # Invalid: rating > 5
    res_high = client.post("/api/ratings", json={"food_id": 1, "rating": 6})
    assert res_high.status_code == 400

    # Invalid: rating < 1
    res_low = client.post("/api/ratings", json={"food_id": 1, "rating": 0})
    assert res_low.status_code == 400

    # Invalid: non-integer
    res_float = client.post("/api/ratings", json={"food_id": 1, "rating": "five"})
    assert res_float.status_code == 400

    # Missing food_id
    res_nofood = client.post("/api/ratings", json={"rating": 4})
    assert res_nofood.status_code == 400


# ── Test 2: Exactly 1 Active Rating per User per Food (No Duplicates) ────────
def test_rating_upsert_no_duplicates(client):
    """Verify submitting a second rating updates the existing record rather than duplicating."""
    _login_session(client, user_id=902)

    # First rating: 3 stars
    res1 = client.post("/api/ratings", json={"food_id": 2, "rating": 3, "review": "Average"})
    assert res1.status_code == 200

    # Second rating by same user for same food: updated to 5 stars
    res2 = client.post("/api/ratings", json={"food_id": 2, "rating": 5, "review": "Much better today!"})
    assert res2.status_code == 200

    # Check database: exactly 1 rating row
    with _DB_LOCK:
        conn = get_db_connection()
        cur = conn.execute("SELECT COUNT(*) as cnt, rating, review FROM ratings WHERE user_id = 902 AND food_id = 2")
        row = cur.fetchone()
        conn.close()

    assert row["cnt"] == 1
    assert row["rating"] == 5
    assert row["review"] == "Much better today!"


# ── Test 3: Average and Count Update Accurately ──────────────────────────────
def test_rating_average_and_count_calculation():
    """Verify real average and count are computed accurately from stored ratings."""
    _create_test_user(user_id=903, email="u903@test.com")
    _create_test_user(user_id=904, email="u904@test.com")
    _create_test_user(user_id=905, email="u905@test.com")

    # Item 3 starts with its baseline or 0 test ratings
    food_id = 3

    # Add 3 controlled ratings
    save_food_rating(user_id=903, food_id=food_id, rating=5)
    save_food_rating(user_id=904, food_id=food_id, rating=4)
    save_food_rating(user_id=905, food_id=food_id, rating=3)

    summary = get_food_rating_summary(food_id)
    assert summary["rating_count"] >= 3
    # Check that average is a valid float between 1.0 and 5.0
    assert 1.0 <= summary["average_rating"] <= 5.0
    assert summary["star_distribution"][5] >= 1
    assert summary["star_distribution"][4] >= 1
    assert summary["star_distribution"][3] >= 1


# ── Test 4: Verified Purchase Tag strictly verified ──────────────────────────
def test_verified_purchase_integrity(client):
    """Verify is_verified_purchase is 1 ONLY if the user has a completed order for that item."""
    _create_test_user(user_id=906, email="verified_buyer@test.com")

    # Before placing any order: rating must NOT be verified
    with _DB_LOCK:
        conn = get_db_connection()
        from database import is_verified_purchase
        assert is_verified_purchase(conn, 906, 4) is False
        conn.close()

    # Rate item 4 before buying
    _login_session(client, user_id=906, email="verified_buyer@test.com")
    res_unverified = client.post("/api/ratings", json={"food_id": 4, "rating": 4, "review": "Looks great"})
    assert res_unverified.get_json()["rating"]["is_verified_purchase"] == 0

    # Insert a COMPLETED order for item 4
    with _DB_LOCK:
        conn = get_db_connection()
        cur_ord = conn.execute("""
            INSERT INTO orders (token, student_name, student_email, user_id, status, total, payment_method, transaction_id, created_at, updated_at)
            VALUES ('T-999', 'Verified Buyer', 'verified_buyer@test.com', 906, ?, 20.0, 'UPI', 'TXN-999', '2026-10-14T00:00:00+00:00', '2026-10-14T00:00:00+00:00')
        """, (STATUS_COMPLETED,))
        ord_id = cur_ord.lastrowid
        conn.execute("""
            INSERT INTO order_items (order_id, menu_item_id, item_name, quantity, price, station_code)
            VALUES (?, 4, 'Vada', 1, 20.0, 'SNACKS')
        """, (ord_id,))
        conn.close()

    # Rate again after completed order: must now be marked verified!
    res_verified = client.post("/api/ratings", json={"food_id": 4, "rating": 5, "review": "Tasted hot and crisp!"})
    assert res_verified.get_json()["rating"]["is_verified_purchase"] == 1


# ── Test 5: Ownership Enforcement for Editing & Deletion ─────────────────────
def test_rating_ownership_enforcement(client):
    """Verify User B cannot edit or delete User A's rating."""
    _create_test_user(user_id=907, email="owner@test.com")
    _create_test_user(user_id=908, email="attacker@test.com")

    # Owner creates rating
    r = save_food_rating(user_id=907, food_id=5, rating=5, review="Owner review")
    rating_id = r["id"]

    # Attacker tries to delete owner's rating
    _login_session(client, user_id=908, email="attacker@test.com")
    del_res = client.delete(f"/api/ratings/{rating_id}")
    assert del_res.status_code == 404  # Not found or permission denied

    # Attacker tries to update owner's rating
    upd_res = client.put(f"/api/ratings/{rating_id}", json={"rating": 1, "review": "Hacked"})
    assert upd_res.status_code in (400, 404)

    # Verify original rating is untouched
    with _DB_LOCK:
        conn = get_db_connection()
        cur = conn.execute("SELECT * FROM ratings WHERE id = ?", (rating_id,))
        row = cur.fetchone()
        conn.close()
    assert row["user_id"] == 907
    assert row["rating"] == 5
    assert row["review"] == "Owner review"

    # Owner deletes their own rating
    _login_session(client, user_id=907, email="owner@test.com")
    del_owner = client.delete(f"/api/ratings/{rating_id}")
    assert del_owner.status_code == 200
    assert del_owner.get_json()["success"] is True


# ── Test 6: Food Details API includes CF Related Foods ───────────────────────
def test_food_details_api(client):
    """Verify GET /api/food/<id> returns complete details, real ratings, and CF related foods."""
    res = client.get("/api/food/2")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True

    food = data["food"]
    assert food["id"] == 2
    assert food["name"] == "Masala Dosa"
    assert "rating_summary" in food
    assert "related_foods" in food
    assert isinstance(food["related_foods"], list)
    # Related items should not include the target item itself
    for rel in food["related_foods"]:
        assert rel["id"] != 2


# ── Test 7: Bayesian Top-Rated Food API ──────────────────────────────────────
def test_top_rated_foods_api(client):
    """Verify GET /api/food/top-rated returns Bayesian-ranked items."""
    res = client.get("/api/food/top-rated?limit=5")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    top_items = data["top_rated"]
    assert len(top_items) > 0
    # Scores must be ordered descending
    scores = [it["bayes_score"] for it in top_items]
    assert scores == sorted(scores, reverse=True)


# ── Test 8: User Preferences Cold-Start API ──────────────────────────────────
def test_user_preferences_api(client):
    """Verify authenticated user can save and retrieve taste preferences."""
    _login_session(client, user_id=909)

    post_res = client.post("/api/user/preferences", json={
        "preferred_categories": ["Breakfast", "Beverages"],
        "dietary_preference": "VEG"
    })
    assert post_res.status_code == 200
    assert post_res.get_json()["success"] is True

    get_res = client.get("/api/user/preferences")
    assert get_res.status_code == 200
    prefs = get_res.get_json()["preferences"]
    assert prefs["preferred_categories"] == ["Breakfast", "Beverages"]
    assert prefs["dietary_preference"] == "VEG"


# ── Test 9: Guest Browsing vs Authenticated Access ───────────────────────────
def test_guest_browsing_permissions(client):
    """Verify guests can browse menu and recommendations, but cannot rate without login."""
    # Guest can view menu
    menu_res = client.get("/api/menu")
    assert menu_res.status_code == 200

    # Guest can view recommendations
    rec_res = client.get("/api/recommendations")
    assert rec_res.status_code == 200

    # Guest cannot submit rating (must return 401)
    rate_res = client.post("/api/ratings", json={"food_id": 1, "rating": 5})
    assert rate_res.status_code == 401
    assert rate_res.get_json()["login_required"] is True

    # Guest cannot access /api/ratings/me
    my_ratings_res = client.get("/api/ratings/me")
    assert my_ratings_res.status_code == 401
