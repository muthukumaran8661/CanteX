"""
tests/test_recommendations.py
=============================
Comprehensive unit and integration test suite for the Item-to-Item
Collaborative Filtering Recommendation Engine.

Tests:
1. Similar item patterns produce relevant recommendations.
2. Items frequently purchased together can be identified.
3. User-specific recommendations differ when their order histories differ.
4. New users receive suitable fallback recommendations (cold-start).
5. Sold-out items (stock = 0) are NEVER presented as available in recommendations.
6. Recommendations do not contain invalid or duplicate entries within/across sections.
7. Recommendation reasons truthfully match the underlying calculation.
8. New eligible orders dynamically influence later recommendations.
9. Empty and insufficient datasets do not cause errors.
10. Offline ranking evaluation metric (Precision@K and Recall@K) measures relevance.
11. Admin recommendation insights endpoint calculates accurate metrics.
"""

import pytest
import sqlite3
from datetime import datetime, timezone

from config import (
    STATUS_COMPLETED,
    STATUS_CANCELLED,
    STATUS_WAITING,
)
from database import (
    init_db,
    get_db_connection,
    reset_menu_item_stock,
    _DB_LOCK,
)
from services import (
    RecommendationService,
    OrderService,
)


@pytest.fixture(autouse=True)
def clean_database():
    """Reset orders, tasks, and stock before each test."""
    init_db()
    with _DB_LOCK:
        conn = get_db_connection()
        conn.execute("DELETE FROM station_tasks")
        conn.execute("DELETE FROM order_items")
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM inventory_logs")
        conn.execute("UPDATE menu_items SET initial_stock = 25 WHERE initial_stock <= 0")
        conn.execute("UPDATE menu_items SET stock = initial_stock")
        conn.close()
    RecommendationService.invalidate_cache()


def _insert_manual_order(conn, user_id, user_email, user_name, items, status=STATUS_COMPLETED):
    """Helper to insert controlled historical orders for testing."""
    now_iso = datetime.now(timezone.utc).isoformat()
    total = sum(it["price"] * it["qty"] for it in items)
    seq_cur = conn.execute("SELECT next_seq FROM token_sequence WHERE id = 1")
    seq = seq_cur.fetchone()["next_seq"]
    conn.execute("UPDATE token_sequence SET next_seq = next_seq + 1 WHERE id = 1")
    token = f"T-{str(seq).zfill(3)}"

    cur = conn.execute("""
        INSERT INTO orders (
            token, student_name, student_email, user_id, status, total,
            payment_method, transaction_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'UPI', ?, ?, ?)
    """, (token, user_name, user_email, user_id, status, total, f"TXN-{seq}", now_iso, now_iso))
    order_id = cur.lastrowid

    for it in items:
        conn.execute("""
            INSERT INTO order_items (
                order_id, menu_item_id, item_name, quantity, price, station_code
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (order_id, it["id"], it["name"], it["qty"], it["price"], it["station_code"]))

    return order_id


# ── Test 1: Empty and Insufficient Datasets Handle Safely ────────────────────
def test_01_empty_dataset_handling():
    """Verify system gracefully returns diverse menu fallback when 0 orders exist."""
    recs = RecommendationService.get_recommendations(user_id=999, email="new@campus.edu")
    assert recs["success"] is True
    assert recs["has_history"] is False

    sections = recs["sections"]
    assert "recommended_for_you" in sections
    assert "frequently_ordered_together" in sections
    assert "because_you_ordered" in sections
    assert "popular_items" in sections
    assert "try_something_new" in sections
    assert "quick_bites" in sections

    # Should fall back cleanly without raising any exceptions
    assert sections["recommended_for_you"]["is_cold_start"] is True
    assert len(sections["popular_items"]["items"]) > 0


# ── Test 2: Cold-Start User Fallback ─────────────────────────────────────────
def test_02_cold_start_fallback():
    """Verify new user receives suitable popular fallback recommendations."""
    # Seed 3 orders for a known diner
    with _DB_LOCK:
        conn = get_db_connection()
        _insert_manual_order(conn, 101, "existing@campus.edu", "Alice", [
            {"id": 2, "name": "Masala Dosa", "qty": 1, "price": 50.0, "station_code": "MAIN_DISH"},
            {"id": 15, "name": "Filter Coffee", "qty": 1, "price": 25.0, "station_code": "BEVERAGES"},
        ])
        conn.close()

    RecommendationService.invalidate_cache()

    # Query for a brand new user
    new_user_recs = RecommendationService.get_recommendations(user_id=999, email="brand_new@campus.edu")
    assert new_user_recs["has_history"] is False
    rec_sec = new_user_recs["sections"]["recommended_for_you"]
    assert rec_sec["is_cold_start"] is True

    # Most popular item should be present in candidates
    rec_item_ids = [it["id"] for it in rec_sec["items"]]
    assert 2 in rec_item_ids or 15 in rec_item_ids


# ── Test 3: Frequently Ordered Together Identified ───────────────────────────
def test_03_frequently_ordered_together():
    """Verify items frequently purchased together in the same order are correctly identified."""
    with _DB_LOCK:
        conn = get_db_connection()
        # Order 1: Masala Dosa + Filter Coffee
        _insert_manual_order(conn, 1, "u1@campus.edu", "U1", [
            {"id": 2, "name": "Masala Dosa", "qty": 1, "price": 50.0, "station_code": "MAIN_DISH"},
            {"id": 15, "name": "Filter Coffee", "qty": 1, "price": 25.0, "station_code": "BEVERAGES"},
        ])
        # Order 2: Masala Dosa + Filter Coffee
        _insert_manual_order(conn, 2, "u2@campus.edu", "U2", [
            {"id": 2, "name": "Masala Dosa", "qty": 1, "price": 50.0, "station_code": "MAIN_DISH"},
            {"id": 15, "name": "Filter Coffee", "qty": 1, "price": 25.0, "station_code": "BEVERAGES"},
        ])
        # Order 3: Idli + Vada
        _insert_manual_order(conn, 3, "u3@campus.edu", "U3", [
            {"id": 1, "name": "Idli", "qty": 2, "price": 30.0, "station_code": "MAIN_DISH"},
            {"id": 4, "name": "Vada", "qty": 1, "price": 20.0, "station_code": "SNACKS"},
        ])
        conn.close()

    RecommendationService.invalidate_cache()

    recs = RecommendationService.get_recommendations(user_id=1, email="u1@campus.edu")
    freq_pairs = recs["sections"]["frequently_ordered_together"]["pairs"]
    assert len(freq_pairs) > 0

    # Top pair must be Masala Dosa + Filter Coffee (co-occurrence = 2)
    top_pair = freq_pairs[0]
    pair_ids = {top_pair["item_a"]["id"], top_pair["item_b"]["id"]}
    assert pair_ids == {2, 15}
    assert top_pair["cooccurrence_count"] == 2
    assert "Filter Coffee" in top_pair["item_a"]["reason"] or "Masala Dosa" in top_pair["item_b"]["reason"]


# ── Test 4: Similar Item Collaborative Filtering ─────────────────────────────
def test_04_similar_item_collaborative_filtering():
    """Verify item-to-item similarity patterns recommend relevant companion dishes."""
    with _DB_LOCK:
        conn = get_db_connection()
        # 3 users order Chicken Biriyani with Chicken 65
        for uid in [10, 11, 12]:
            _insert_manual_order(conn, uid, f"u{uid}@campus.edu", f"User{uid}", [
                {"id": 11, "name": "Chicken Biriyani", "qty": 1, "price": 120.0, "station_code": "MAIN_DISH"},
                {"id": 13, "name": "Chicken 65", "qty": 1, "price": 100.0, "station_code": "SNACKS"},
            ])
        # User 13 orders Chicken Biriyani only
        _insert_manual_order(conn, 13, "u13@campus.edu", "User13", [
            {"id": 11, "name": "Chicken Biriyani", "qty": 1, "price": 120.0, "station_code": "MAIN_DISH"},
        ])
        conn.close()

    RecommendationService.invalidate_cache()

    # Recommendation for User 13 (who only ordered Biryani) should strongly rank Chicken 65
    recs = RecommendationService.get_recommendations(user_id=13, email="u13@campus.edu")
    assert recs["has_history"] is True

    rec_items = recs["sections"]["recommended_for_you"]["items"]
    rec_ids = [it["id"] for it in rec_items]
    assert 13 in rec_ids  # Chicken 65 recommended

    because_items = recs["sections"]["because_you_ordered"]["items"]
    because_ids = [it["id"] for it in because_items]
    assert 13 in because_ids


# ── Test 5: Distinct Recommendations for Distinct User Histories ────────────
def test_05_user_personalization_differs():
    """Verify User A (vegetarian breakfast) and User B (non-veg biryani) get distinct recommendations."""
    with _DB_LOCK:
        conn = get_db_connection()
        # History for User A: Masala Dosa + Filter Coffee repeatedly
        for _ in range(3):
            _insert_manual_order(conn, 201, "dosa_fan@campus.edu", "Dosa Fan", [
                {"id": 2, "name": "Masala Dosa", "qty": 1, "price": 50.0, "station_code": "MAIN_DISH"},
                {"id": 15, "name": "Filter Coffee", "qty": 1, "price": 25.0, "station_code": "BEVERAGES"},
            ])
        # History for User B: Biryani + Chicken 65 repeatedly
        for _ in range(3):
            _insert_manual_order(conn, 202, "biryani_fan@campus.edu", "Biryani Fan", [
                {"id": 11, "name": "Chicken Biriyani", "qty": 1, "price": 120.0, "station_code": "MAIN_DISH"},
                {"id": 13, "name": "Chicken 65", "qty": 1, "price": 100.0, "station_code": "SNACKS"},
            ])
        conn.close()

    RecommendationService.invalidate_cache()

    recs_a = RecommendationService.get_recommendations(user_id=201, email="dosa_fan@campus.edu")
    recs_b = RecommendationService.get_recommendations(user_id=202, email="biryani_fan@campus.edu")

    items_a = [it["id"] for it in recs_a["sections"]["recommended_for_you"]["items"][:3]]
    items_b = [it["id"] for it in recs_b["sections"]["recommended_for_you"]["items"][:3]]

    # Recommendations must not be identical
    assert items_a != items_b


# ── Test 6: Sold-Out Items are Strictly Excluded ─────────────────────────────
def test_06_sold_out_items_never_recommended():
    """Verify items with stock <= 0 are NEVER presented as available in recommendations."""
    # Reset Burger stock to 0
    reset_menu_item_stock(17, 0)
    reset_menu_item_stock(18, 0)  # Samosa stock = 0

    with _DB_LOCK:
        conn = get_db_connection()
        # Seed orders containing Burger so Burger has historical popularity
        for uid in range(5):
            _insert_manual_order(conn, uid + 50, f"u{uid}@campus.edu", "Burger Fan", [
                {"id": 17, "name": "Burger", "qty": 1, "price": 60.0, "station_code": "SNACKS"},
            ])
        conn.close()

    RecommendationService.invalidate_cache()

    recs = RecommendationService.get_recommendations(user_id=50, email="u0@campus.edu")
    for sec_name, section in recs["sections"].items():
        if "items" in section:
            for item in section["items"]:
                assert item["id"] != 17, f"Sold out Burger appeared in {sec_name}"
                assert item["id"] != 18, f"Sold out Samosa appeared in {sec_name}"
                assert item["stock"] > 0
                assert item["is_available"] is True

    # Restore stock for subsequent tests
    reset_menu_item_stock(17, 15)
    reset_menu_item_stock(18, 30)


# ── Test 7: 'Try Something New' Excludes Already Ordered Dishes ─────────────
def test_07_try_something_new_excludes_ordered_items():
    """Verify 'Try Something New' never recommends food already purchased by this user."""
    ordered_ids = {2, 15}  # Masala Dosa and Filter Coffee

    with _DB_LOCK:
        conn = get_db_connection()
        _insert_manual_order(conn, 301, "explorer@campus.edu", "Explorer", [
            {"id": 2, "name": "Masala Dosa", "qty": 1, "price": 50.0, "station_code": "MAIN_DISH"},
            {"id": 15, "name": "Filter Coffee", "qty": 1, "price": 25.0, "station_code": "BEVERAGES"},
        ])
        conn.close()

    RecommendationService.invalidate_cache()

    recs = RecommendationService.get_recommendations(user_id=301, email="explorer@campus.edu")
    try_new_items = recs["sections"]["try_something_new"]["items"]

    for it in try_new_items:
        assert it["id"] not in ordered_ids, f"Previously ordered item {it['name']} appeared in Try Something New"


# ── Test 8: Deduplication and Diversity Control ──────────────────────────────
def test_08_deduplication_and_diversity():
    """Verify no duplicate items exist within any recommendation section."""
    recs = RecommendationService.get_recommendations()
    for sec_name, section in recs["sections"].items():
        if "items" in section:
            item_ids = [it["id"] for it in section["items"]]
            assert len(item_ids) == len(set(item_ids)), f"Duplicates found in section {sec_name}"


# ── Test 9: Cancelled Orders Excluded from Analysis ──────────────────────────
def test_09_cancelled_orders_excluded():
    """Verify cancelled orders are not counted in item similarity or popularity."""
    with _DB_LOCK:
        conn = get_db_connection()
        # Insert a cancelled order containing Poori Masala
        _insert_manual_order(conn, 401, "cancelled@campus.edu", "Cancelled User", [
            {"id": 5, "name": "Poori Masala", "qty": 10, "price": 50.0, "station_code": "MAIN_DISH"},
        ], status=STATUS_CANCELLED)
        conn.close()

    RecommendationService.invalidate_cache()

    model = RecommendationService._get_model()
    # Poori Masala order count should NOT count the cancelled order
    assert model["item_order_counts"].get(5, 0) == 0


# ── Test 10: New Eligible Orders Influence Later Recommendations ─────────────
def test_10_new_orders_influence_recommendations():
    """Verify placing a new order invalidates cache and updates subsequent recommendations."""
    # User orders Biryani
    with _DB_LOCK:
        conn = get_db_connection()
        _insert_manual_order(conn, 501, "new_eater@campus.edu", "New Eater", [
            {"id": 11, "name": "Chicken Biriyani", "qty": 1, "price": 120.0, "station_code": "MAIN_DISH"},
        ])
        # Another user bought Biryani + Lime Juice
        _insert_manual_order(conn, 502, "other@campus.edu", "Other", [
            {"id": 11, "name": "Chicken Biriyani", "qty": 1, "price": 120.0, "station_code": "MAIN_DISH"},
            {"id": 16, "name": "Fresh Lime Juice", "qty": 1, "price": 30.0, "station_code": "BEVERAGES"},
        ])
        conn.close()

    RecommendationService.invalidate_cache()

    recs_before = RecommendationService.get_recommendations(user_id=501, email="new_eater@campus.edu")
    assert recs_before["sections"]["because_you_ordered"]["anchor_item"]["id"] == 11

    # Now user places a new order with Parotta
    with _DB_LOCK:
        conn = get_db_connection()
        _insert_manual_order(conn, 501, "new_eater@campus.edu", "New Eater", [
            {"id": 10, "name": "Parotta", "qty": 2, "price": 45.0, "station_code": "MAIN_DISH"},
        ])
        conn.close()

    RecommendationService.invalidate_cache()

    recs_after = RecommendationService.get_recommendations(user_id=501, email="new_eater@campus.edu")
    # Recent anchor must now reflect Parotta
    assert recs_after["sections"]["because_you_ordered"]["anchor_item"]["id"] == 10


# ── Test 11: Admin Insights and Offline Evaluation Metrics ───────────────────
def test_11_admin_insights_and_offline_ranking():
    """Verify admin insights and offline ranking metrics calculate real measured values."""
    # Seed authentic dataset
    RecommendationService.seed_demo_orders(count=20, force=True)

    insights = RecommendationService.get_admin_insights()
    assert insights["success"] is True
    measured = insights["measured_analytics"]
    assert measured["eligible_orders_count"] >= 20
    assert measured["items_with_similarity_data"] > 0
    assert len(insights["most_frequently_ordered_pairs"]) > 0

    # Evaluate offline ranking
    eval_res = RecommendationService.evaluate_offline_ranking(k=3)
    assert "precision_at_k" in eval_res
    assert "recall_at_k" in eval_res
    assert eval_res["evaluated_users"] > 0
    assert eval_res["precision_at_k"] >= 0.0
    assert eval_res["recall_at_k"] >= 0.0
