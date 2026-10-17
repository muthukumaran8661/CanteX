"""
services/recommendation_service.py
==================================
Comprehensive Item-to-Item Collaborative Filtering Recommendation Engine
for Smart Food Recommender.

Features:
- Primary signal: Explicit user ratings matrix R(u, i) in [1..5].
- Secondary signal: Order interactions & time-decayed co-occurrence.
- Item-to-Item Cosine Similarity based on overlapping user rating vectors with shrinkage:
    cos(i, j) = sum(R_ui * R_uj) / (sqrt(sum(R_ui^2)) * sqrt(sum(R_uj^2))) * (N / (N + 2))
- Predicted user rating:
    R_hat(u, i) = sum(sim(i, k) * R_uk) / sum(|sim(i, k)|)
- Bayesian average smoothing for global item quality:
    R_bayes(i) = (C * m + sum(R)) / (C + N) where C=3, m=3.5
- 7 Distinct Recommendation Sections:
    1. Recommended for You (Personalized Item-to-Item Collaborative Filtering)
    2. Because You Rated This (Anchored to user's highest rated food)
    3. Because You Ordered This (Contextual recommendation anchored to recent order)
    4. Frequently Ordered Together (Real order co-occurrence pairs with 1-click combo)
    5. Top Rated Food (Ranked strictly by Bayesian smoothed rating and rating count)
    6. Popular at Smart Canteen (Order volume bestsellers)
    7. Discover Something New (High-quality unrated items across diverse categories)
    8. Your Favourite Categories (Food suggestions from user's top-rated categories)
    9. Quick Bites for You (Fast snacks & beverages tailored to taste)
- Standalone Food Details related items:
    get_related_foods(food_id) using Item-to-Item CF similarity.
- Strictly excludes unavailable items (stock <= 0) and inactive items from orderable recommendations.
- Category diversity and deduplication control.
- In-memory similarity matrix caching with automatic invalidation.
- Admin recommendation insights & real measured ratings analytics.
- Offline ranking evaluation metric (Precision@K and Recall@K).
"""

import math
import time
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Set

from config import (
    REC_WEIGHT_ITEM_SIMILARITY,
    REC_WEIGHT_USER_RATINGS,
    REC_WEIGHT_ITEM_QUALITY,
    REC_WEIGHT_COOCCURRENCE,
    REC_WEIGHT_POPULARITY,
    REC_RECENCY_DECAY_DAYS,
    REC_CACHE_TTL_SECONDS,
    REC_DEFAULT_TOP_K,
    REC_MAX_PER_CATEGORY,
    REC_BAYESIAN_PRIOR_RATING,
    REC_BAYESIAN_PRIOR_COUNT,
    STATUS_CANCELLED,
    STATUS_WAITING,
    STATUS_PREPARING,
    STATUS_PARTIALLY_READY,
    STATUS_READY,
    STATUS_COMPLETED,
    STATUS_SERVED,
    TOKEN_PREFIX,
    TOKEN_PADDING,
)
from database import get_db_connection, _DB_LOCK

logger = logging.getLogger(__name__)

# Valid order statuses that represent legitimate customer interactions
ELIGIBLE_ORDER_STATUSES = (
    STATUS_WAITING,
    STATUS_PREPARING,
    STATUS_PARTIALLY_READY,
    STATUS_READY,
    STATUS_COMPLETED,
    STATUS_SERVED,
)


class RecommendationService:
    """Production-grade Item-to-Item Collaborative Filtering Recommendation Engine."""

    # Thread-safe in-memory cache
    _cache_lock = threading.RLock()
    _last_computed_time: float = 0.0
    _cached_model_data: Optional[Dict[str, Any]] = None

    # Operational metrics for Admin Dashboard
    _metrics_lock = threading.Lock()
    _stats_requests_served: int = 0
    _stats_cold_start_served: int = 0
    _stats_item_rec_counts: Dict[int, int] = {}

    @classmethod
    def invalidate_cache(cls):
        """Invalidate similarity cache when new ratings, orders, or stock changes occur."""
        with cls._cache_lock:
            cls._last_computed_time = 0.0
            cls._cached_model_data = None
            logger.debug("Recommendation cache invalidated.")

    @classmethod
    def _parse_timestamp(cls, ts_str: Optional[str]) -> Optional[datetime]:
        """Safely parse ISO timestamp into timezone-aware datetime."""
        if not ts_str:
            return None
        try:
            cleaned = ts_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    @classmethod
    def _build_model(cls) -> Dict[str, Any]:
        """
        Build the collaborative filtering model from database ratings and orders:
        1. Query menu items.
        2. Query explicit ratings and user preferences.
        3. Query eligible non-cancelled orders & items.
        4. Construct user-item ratings matrix and compute Item-to-Item Cosine Similarity.
        5. Compute pairwise co-occurrence frequencies and Jaccard similarities.
        6. Compute item average ratings, rating counts, Bayesian smoothed scores, and popularity.
        """
        with _DB_LOCK:
            conn = get_db_connection()
            try:
                # 1. Fetch active menu items
                menu_cur = conn.execute("""
                    SELECT id, name, price, category, station_code,
                           prep_time_minutes, stock, initial_stock, image, badge, description, active
                    FROM menu_items
                """)
                menu_items = {row["id"]: dict(row) for row in menu_cur.fetchall()}

                # 2. Fetch explicit ratings
                ratings_cur = conn.execute("""
                    SELECT id, user_id, food_id, rating, review, is_verified_purchase, created_at, updated_at
                    FROM ratings
                    ORDER BY updated_at ASC
                """)
                ratings_rows = [dict(r) for r in ratings_cur.fetchall()]

                # 3. Fetch user preferences
                prefs_cur = conn.execute("""
                    SELECT user_id, preferred_categories, dietary_preference
                    FROM user_preferences
                """)
                prefs_rows = [dict(r) for r in prefs_cur.fetchall()]

                # 4. Fetch eligible orders
                status_placeholders = ",".join(["?"] * len(ELIGIBLE_ORDER_STATUSES))
                orders_query = f"""
                    SELECT id, token, student_name, student_email, user_id, status, total, created_at
                    FROM orders
                    WHERE status != '{STATUS_CANCELLED}' AND status IN ({status_placeholders})
                    ORDER BY id ASC
                """
                orders_cur = conn.execute(orders_query, ELIGIBLE_ORDER_STATUSES)
                orders_rows = [dict(r) for r in orders_cur.fetchall()]
                order_ids = [r["id"] for r in orders_rows]

                order_meta: Dict[int, Dict[str, Any]] = {}
                for ord_row in orders_rows:
                    user_key = None
                    if ord_row.get("user_id"):
                        user_key = f"uid:{ord_row['user_id']}"
                    elif ord_row.get("student_email") and ord_row["student_email"].strip():
                        user_key = f"email:{ord_row['student_email'].strip().lower()}"
                    elif ord_row.get("student_name") and ord_row["student_name"].strip():
                        user_key = f"name:{ord_row['student_name'].strip().lower()}"

                    order_meta[ord_row["id"]] = {
                        "user_key": user_key,
                        "created_at": cls._parse_timestamp(ord_row.get("created_at")),
                        "status": ord_row["status"],
                    }

                items_by_order: Dict[int, List[Dict[str, Any]]] = {}
                if order_ids:
                    id_placeholders = ",".join(["?"] * len(order_ids))
                    items_query = f"""
                        SELECT id, order_id, menu_item_id, item_name, quantity, price, station_code
                        FROM order_items
                        WHERE order_id IN ({id_placeholders})
                    """
                    items_cur = conn.execute(items_query, order_ids)
                    for r in items_cur.fetchall():
                        oid = r["order_id"]
                        if oid not in items_by_order:
                            items_by_order[oid] = []
                        items_by_order[oid].append(dict(r))

            finally:
                conn.close()

        now_dt = datetime.now(timezone.utc)

        # ── Process Explicit Ratings Matrix ──────────────────────────────────
        # user_ratings: user_key -> {food_id: rating (1..5)}
        user_ratings: Dict[str, Dict[int, float]] = {}
        # food_ratings: food_id -> {user_key: rating}
        food_ratings: Dict[int, Dict[str, float]] = {mid: {} for mid in menu_items}
        # food_ratings_list: food_id -> list of float ratings
        food_ratings_list: Dict[int, List[float]] = {mid: [] for mid in menu_items}
        # user_verified_foods: user_key -> set of food_ids verified
        user_verified_foods: Dict[str, Set[int]] = {}

        for r_row in ratings_rows:
            u_key = f"uid:{r_row['user_id']}"
            fid = r_row["food_id"]
            if fid not in menu_items:
                continue
            r_val = float(r_row["rating"])

            if u_key not in user_ratings:
                user_ratings[u_key] = {}
            user_ratings[u_key][fid] = r_val

            food_ratings[fid][u_key] = r_val
            food_ratings_list[fid].append(r_val)

            if r_row.get("is_verified_purchase"):
                if u_key not in user_verified_foods:
                    user_verified_foods[u_key] = set()
                user_verified_foods[u_key].add(fid)

        # Compute real average rating, count, and Bayesian smoothed score
        food_avg_ratings: Dict[int, Optional[float]] = {}
        food_rating_counts: Dict[int, int] = {}
        food_bayes_scores: Dict[int, float] = {}

        C = REC_BAYESIAN_PRIOR_COUNT    # 3.0
        m = REC_BAYESIAN_PRIOR_RATING   # 3.5

        for mid in menu_items:
            r_list = food_ratings_list.get(mid, [])
            cnt = len(r_list)
            food_rating_counts[mid] = cnt
            if cnt > 0:
                food_avg_ratings[mid] = round(sum(r_list) / float(cnt), 1)
                food_bayes_scores[mid] = (C * m + sum(r_list)) / (C + cnt)
            else:
                food_avg_ratings[mid] = None
                food_bayes_scores[mid] = m  # prior mean

        # User preferences map
        user_prefs: Dict[str, Dict[str, Any]] = {}
        for p_row in prefs_rows:
            u_key = f"uid:{p_row['user_id']}"
            try:
                cats = json.loads(p_row.get("preferred_categories") or "[]")
            except Exception:
                cats = []
            user_prefs[u_key] = {
                "preferred_categories": cats,
                "dietary_preference": p_row.get("dietary_preference") or "ALL",
            }

        # ── Process Order History & Co-occurrences ────────────────────────────
        user_order_history: Dict[str, List[Dict[str, Any]]] = {}
        user_interactions: Dict[str, Dict[int, float]] = {}
        item_order_counts: Dict[int, int] = {item_id: 0 for item_id in menu_items}
        pair_cooccurrence: Dict[Tuple[int, int], int] = {}

        for oid, meta in order_meta.items():
            user_key = meta.get("user_key")
            ord_items = items_by_order.get(oid, [])
            if not ord_items:
                continue

            created_at = meta.get("created_at")
            if created_at:
                days_diff = max(0.0, (now_dt - created_at).total_seconds() / 86400.0)
                decay_factor = math.exp(-days_diff / max(1.0, REC_RECENCY_DECAY_DAYS))
            else:
                decay_factor = 1.0

            distinct_order_item_ids = set()
            for it in ord_items:
                mid = it["menu_item_id"]
                if mid not in menu_items:
                    continue
                qty = max(1, it.get("quantity", 1))
                distinct_order_item_ids.add(mid)

                interaction_val = (1.0 + math.log(qty)) * decay_factor
                if user_key:
                    if user_key not in user_interactions:
                        user_interactions[user_key] = {}
                    user_interactions[user_key][mid] = (
                        user_interactions[user_key].get(mid, 0.0) + interaction_val
                    )

            for mid in distinct_order_item_ids:
                item_order_counts[mid] = item_order_counts.get(mid, 0) + 1

            distinct_list = sorted(list(distinct_order_item_ids))
            for i in range(len(distinct_list)):
                for j in range(i + 1, len(distinct_list)):
                    pair = (distinct_list[i], distinct_list[j])
                    pair_cooccurrence[pair] = pair_cooccurrence.get(pair, 0) + 1

            if user_key:
                if user_key not in user_order_history:
                    user_order_history[user_key] = []
                user_order_history[user_key].append({
                    "order_id": oid,
                    "created_at": created_at,
                    "items": distinct_list,
                })

        # ── Compute Item-to-Item Cosine Similarity & Association Rules ───────
        all_item_ids = list(menu_items.keys())
        cosine_sim: Dict[Tuple[int, int], float] = {}
        jaccard_sim: Dict[Tuple[int, int], float] = {}
        blended_sim: Dict[Tuple[int, int], float] = {}

        for i in range(len(all_item_ids)):
            id_a = all_item_ids[i]
            r_vec_a = food_ratings.get(id_a, {})

            for j in range(i + 1, len(all_item_ids)):
                id_b = all_item_ids[j]
                r_vec_b = food_ratings.get(id_b, {})
                pair_key = (min(id_a, id_b), max(id_a, id_b))

                # 1. Rating-based Cosine Similarity
                overlapping_users = [u for u in r_vec_a if u in r_vec_b]
                num_overlap = len(overlapping_users)

                if num_overlap >= 1:
                    dot = sum(r_vec_a[u] * r_vec_b[u] for u in overlapping_users)
                    norm_a = math.sqrt(sum(r_vec_a[u] ** 2 for u in overlapping_users))
                    norm_b = math.sqrt(sum(r_vec_b[u] ** 2 for u in overlapping_users))
                    if norm_a > 0.0 and norm_b > 0.0:
                        raw_cos = dot / (norm_a * norm_b)
                        # Confidence / shrinkage factor based on overlap size
                        shrinkage = float(num_overlap) / (float(num_overlap) + 2.0)
                        cos = raw_cos * shrinkage
                    else:
                        cos = 0.0
                else:
                    cos = 0.0
                cosine_sim[pair_key] = cos

                # 2. Order-based Jaccard Co-occurrence Similarity
                co_count = pair_cooccurrence.get(pair_key, 0)
                denom = item_order_counts.get(id_a, 0) + item_order_counts.get(id_b, 0) - co_count
                jacc = (co_count / denom) if denom > 0 else 0.0
                jaccard_sim[pair_key] = jacc

                # 3. Blended Similarity (Rating CF primary, Order Co-occurrence secondary)
                if cos > 0.0 and jacc > 0.0:
                    blend = 0.70 * cos + 0.30 * jacc
                elif cos > 0.0:
                    blend = cos
                elif jacc > 0.0:
                    blend = 0.50 * jacc
                else:
                    blend = 0.0

                blended_sim[pair_key] = blend

        # Normalized global order popularity scores [0, 1]
        max_orders = max(item_order_counts.values()) if item_order_counts else 1
        max_orders = max(1, max_orders)
        popularity_scores = {
            mid: item_order_counts.get(mid, 0) / float(max_orders)
            for mid in menu_items
        }

        return {
            "menu_items": menu_items,
            "user_ratings": user_ratings,
            "food_ratings": food_ratings,
            "food_avg_ratings": food_avg_ratings,
            "food_rating_counts": food_rating_counts,
            "food_bayes_scores": food_bayes_scores,
            "user_prefs": user_prefs,
            "user_interactions": user_interactions,
            "user_order_history": user_order_history,
            "item_order_counts": item_order_counts,
            "pair_cooccurrence": pair_cooccurrence,
            "cosine_sim": cosine_sim,
            "jaccard_sim": jaccard_sim,
            "blended_sim": blended_sim,
            "popularity_scores": popularity_scores,
            "eligible_orders_count": len(orders_rows),
            "ratings_count": len(ratings_rows),
        }

    @classmethod
    def _get_model(cls) -> Dict[str, Any]:
        """Return cached model or compute fresh one if expired."""
        now = time.time()
        with cls._cache_lock:
            if (
                cls._cached_model_data is None
                or (now - cls._last_computed_time) > REC_CACHE_TTL_SECONDS
            ):
                cls._cached_model_data = cls._build_model()
                cls._last_computed_time = now
            return cls._cached_model_data

    @classmethod
    def _get_item_similarity(
        cls, id_a: int, id_b: int, model: Dict[str, Any]
    ) -> float:
        """Lookup blended similarity between two food items."""
        if id_a == id_b:
            return 1.0
        pair_key = (min(id_a, id_b), max(id_a, id_b))
        return model["blended_sim"].get(pair_key, 0.0)

    @classmethod
    def _get_item_cooccurrence(
        cls, id_a: int, id_b: int, model: Dict[str, Any]
    ) -> int:
        """Lookup direct co-occurrence count between two food items."""
        if id_a == id_b:
            return model["item_order_counts"].get(id_a, 0)
        pair_key = (min(id_a, id_b), max(id_a, id_b))
        return model["pair_cooccurrence"].get(pair_key, 0)

    @classmethod
    def _get_user_key(
        cls, user_id: Optional[int] = None, email: Optional[str] = None
    ) -> Optional[str]:
        """Construct consistent user key."""
        if user_id:
            return f"uid:{user_id}"
        if email and email.strip():
            return f"email:{email.strip().lower()}"
        return None

    @classmethod
    def _predict_user_rating(
        cls, user_key: Optional[str], food_id: int, model: Dict[str, Any]
    ) -> float:
        """
        Predict user's rating for candidate food_id using Item-to-Item Collaborative Filtering:
        R_hat(u, i) = sum_{k in Rated(u)} [ sim(i, k) * R(u, k) ] / sum_{k in Rated(u)} |sim(i, k)|
        Falls back to Bayesian item average if no similar rated items exist.
        """
        bayes_avg = model["food_bayes_scores"].get(food_id, REC_BAYESIAN_PRIOR_RATING)
        if not user_key or "user_ratings" not in model:
            return bayes_avg

        user_rated = model["user_ratings"].get(user_key, {})
        if not user_rated:
            return bayes_avg

        # If user already explicitly rated this item, return their rating
        if food_id in user_rated:
            return user_rated[food_id]

        numer = 0.0
        denom = 0.0

        for rated_id, user_val in user_rated.items():
            sim = cls._get_item_similarity(food_id, rated_id, model)
            if sim > 0.01:
                numer += sim * user_val
                denom += sim

        if denom > 0.0:
            pred = numer / denom
            weight_cf = min(1.0, denom / 1.5)
            return weight_cf * pred + (1.0 - weight_cf) * bayes_avg

        return bayes_avg

    @classmethod
    def _format_card(
        cls,
        item: Dict[str, Any],
        reason: str,
        model: Optional[Dict[str, Any]] = None,
        user_key: Optional[str] = None,
        predicted_rating: Optional[float] = None,
        similarity_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Format an individual menu item into a rich food recommendation card."""
        stock = item.get("stock", 0)
        mid = item["id"]

        avg_rating = None
        rating_count = 0
        user_rating = None

        if model:
            avg_rating = model.get("food_avg_ratings", {}).get(mid)
            rating_count = model.get("food_rating_counts", {}).get(mid, 0)
            if user_key and "user_ratings" in model:
                user_rating = model["user_ratings"].get(user_key, {}).get(mid)

        return {
            "id": mid,
            "name": item["name"],
            "price": float(item["price"]),
            "category": item.get("category", "Main"),
            "station_code": item.get("station_code", "MAIN_DISH"),
            "prep_time_minutes": item.get("prep_time_minutes", 5),
            "stock": stock,
            "is_available": stock > 0,
            "image": item.get("image", "/static/images/idli.jpg"),
            "badge": item.get("badge", ""),
            "description": item.get("description", ""),
            "reason": reason,
            "average_rating": avg_rating,
            "rating_count": rating_count,
            "user_rating": user_rating,
            "predicted_rating": round(predicted_rating, 1) if predicted_rating is not None else None,
            "similarity_score": round(similarity_score, 2) if similarity_score is not None else None,
        }

    @classmethod
    def _apply_category_diversity(
        cls,
        scored_items: List[Dict[str, Any]],
        limit: int,
        model: Optional[Dict[str, Any]] = None,
        user_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Enforce diversity control: avoid overwhelming recommendations with
        a single category (max 2 per category). Deduplicates items.
        """
        chosen: List[Dict[str, Any]] = []
        category_counts: Dict[str, int] = {}
        seen_ids: Set[int] = set()

        remaining = []
        for entry in scored_items:
            item = entry["item"]
            mid = item["id"]
            if mid in seen_ids:
                continue
            cat = item.get("category", "Main")
            curr_cat_count = category_counts.get(cat, 0)
            if curr_cat_count < REC_MAX_PER_CATEGORY:
                chosen.append(cls._format_card(
                    item,
                    entry["reason"],
                    model=model,
                    user_key=user_key,
                    predicted_rating=entry.get("predicted_rating"),
                    similarity_score=entry.get("similarity_score"),
                ))
                category_counts[cat] = curr_cat_count + 1
                seen_ids.add(mid)
                if len(chosen) >= limit:
                    return chosen
            else:
                remaining.append(entry)

        for entry in remaining:
            item = entry["item"]
            mid = item["id"]
            if mid not in seen_ids:
                chosen.append(cls._format_card(
                    item,
                    entry["reason"],
                    model=model,
                    user_key=user_key,
                    predicted_rating=entry.get("predicted_rating"),
                    similarity_score=entry.get("similarity_score"),
                ))
                seen_ids.add(mid)
                if len(chosen) >= limit:
                    break

        return chosen

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION GENERATORS
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def get_recommendations(
        cls,
        user_id: Optional[int] = None,
        email: Optional[str] = None,
        cart_item_ids: Optional[List[int]] = None,
        limit: int = REC_DEFAULT_TOP_K,
    ) -> Dict[str, Any]:
        """
        Main API returning full recommendation suite with real ratings & collaborative filtering.
        """
        model = cls._get_model()
        user_key = cls._get_user_key(user_id=user_id, email=email)
        cart_set = set(cart_item_ids or [])

        user_ratings = model["user_ratings"].get(user_key, {}) if user_key else {}
        user_interactions = model["user_interactions"].get(user_key, {}) if user_key else {}
        has_history = len(user_ratings) > 0 or len(user_interactions) > 0

        with cls._metrics_lock:
            cls._stats_requests_served += 1
            if not has_history:
                cls._stats_cold_start_served += 1

        # 1. Section: Recommended for You (Personalized Item-to-Item CF)
        rec_for_you = cls._build_recommended_for_you(
            model=model,
            user_key=user_key,
            user_ratings=user_ratings,
            user_interactions=user_interactions,
            cart_set=cart_set,
            limit=limit,
        )

        # 2. Section: Because You Rated This
        because_rated = cls._build_because_you_rated(
            model=model,
            user_key=user_key,
            user_ratings=user_ratings,
            cart_set=cart_set,
            limit=limit,
        )

        # 3. Section: Because You Ordered This (Preserved for compatibility)
        because_ordered = cls._build_because_you_ordered(
            model=model,
            user_key=user_key,
            cart_set=cart_set,
            limit=limit,
        )

        # 4. Section: Frequently Ordered Together (Pairs)
        freq_together = cls._build_frequently_ordered_together(
            model=model,
            cart_item_ids=cart_item_ids or [],
            user_key=user_key,
            limit=4,
        )

        # 5. Section: Top Rated Food (Ranked by Bayesian average)
        top_rated = cls._build_top_rated_food(
            model=model,
            exclude_ids=cart_set,
            limit=limit,
        )

        # 6. Section: Popular at Smart Canteen
        popular_items = cls._build_popular_items(
            model=model,
            exclude_ids=cart_set,
            limit=limit,
        )

        # 7. Section: Try Something New (Never rated or ordered by user)
        try_new = cls._build_try_something_new(
            model=model,
            user_key=user_key,
            user_ratings=user_ratings,
            user_interactions=user_interactions,
            cart_set=cart_set,
            limit=limit,
        )

        # 8. Section: Your Favourite Categories
        fav_categories = cls._build_favourite_categories(
            model=model,
            user_key=user_key,
            user_ratings=user_ratings,
            cart_set=cart_set,
            limit=limit,
        )

        # 9. Section: Quick Bites for You
        quick_bites = cls._build_quick_bites(
            model=model,
            user_key=user_key,
            user_interactions=user_interactions,
            cart_set=cart_set,
            limit=limit,
        )

        # Record served items in operational metrics
        all_served_ids = []
        for s in (
            rec_for_you["items"],
            because_rated["items"],
            because_ordered["items"],
            top_rated["items"],
            popular_items["items"],
            try_new["items"],
            fav_categories["items"],
            quick_bites["items"],
        ):
            for item in s:
                all_served_ids.append(item["id"])
        with cls._metrics_lock:
            for item_id in all_served_ids:
                cls._stats_item_rec_counts[item_id] = (
                    cls._stats_item_rec_counts.get(item_id, 0) + 1
                )

        return {
            "success": True,
            "has_history": has_history,
            "user_id": user_id,
            "ratings_count": len(user_ratings),
            "sections": {
                "recommended_for_you": rec_for_you,
                "because_you_rated": because_rated,
                "because_you_ordered": because_ordered,
                "frequently_ordered_together": freq_together,
                "top_rated_food": top_rated,
                "popular_items": popular_items,
                "try_something_new": try_new,
                "favourite_categories": fav_categories,
                "quick_bites": quick_bites,
            },
        }

    @classmethod
    def get_related_foods(cls, food_id: int, limit: int = 4) -> List[Dict[str, Any]]:
        """
        Return related foods for a specific food item using Item-to-Item Collaborative Filtering.
        Used on the Food Details modal to display real similar foods.
        """
        model = cls._get_model()
        menu_items = model["menu_items"]
        if food_id not in menu_items:
            return []

        target_item = menu_items[food_id]
        candidates = []

        for mid, item in menu_items.items():
            if mid == food_id:
                continue
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue

            sim = cls._get_item_similarity(mid, food_id, model)
            co_cnt = cls._get_item_cooccurrence(mid, food_id, model)
            bayes_score = model["food_bayes_scores"].get(mid, 3.5) / 5.0

            score = 0.60 * sim + 0.25 * (min(co_cnt, 5) / 5.0) + 0.15 * bayes_score

            if sim > 0.1:
                reason = f"{int(sim * 100)}% rating similarity to {target_item['name']}"
            elif co_cnt > 0:
                reason = f"Frequently ordered with {target_item['name']}"
            else:
                reason = f"Pairs well with {target_item['name']}"

            candidates.append({
                "item": item,
                "score": score,
                "reason": reason,
                "similarity_score": sim,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return [
            cls._format_card(c["item"], c["reason"], model=model, similarity_score=c["similarity_score"])
            for c in candidates[:limit]
        ]

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION IMPLEMENTATIONS
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def _build_recommended_for_you(
        cls,
        model: Dict[str, Any],
        user_key: Optional[str],
        user_ratings: Dict[int, float],
        user_interactions: Dict[int, float],
        cart_set: Set[int],
        limit: int,
    ) -> Dict[str, Any]:
        """
        Section 1: "Recommended for You"
        Uses Item-to-Item Collaborative Filtering when explicit ratings or orders exist.
        Predicts preference score for each unrated candidate item.
        """
        menu_items = model["menu_items"]
        prefs = model["user_prefs"].get(user_key, {}) if user_key else {}
        preferred_cats = set(prefs.get("preferred_categories", []))
        diet_pref = prefs.get("dietary_preference", "ALL")

        # Check if user has explicit ratings or order interactions
        if user_ratings or user_interactions:
            scored_candidates = []

            # Determine anchor
            top_anchor_name = None
            if user_ratings:
                top_rated_entry = sorted(user_ratings.items(), key=lambda x: x[1], reverse=True)[0]
                top_anchor_name = menu_items.get(top_rated_entry[0], {}).get("name")
            elif user_interactions:
                top_order_entry = sorted(user_interactions.items(), key=lambda x: x[1], reverse=True)[0]
                top_anchor_name = menu_items.get(top_order_entry[0], {}).get("name")

            for mid, item in menu_items.items():
                if not item.get("active") or item.get("stock", 0) <= 0:
                    continue
                if mid in cart_set:
                    continue

                # Filter by dietary preference
                cat = item.get("category", "")
                if diet_pref == "VEG" and cat == "Non-Vegetarian":
                    continue

                # 1. Predicted rating from explicit ratings CF
                pred_rating = cls._predict_user_rating(user_key, mid, model)
                norm_pred = pred_rating / 5.0

                # 2. User order interaction affinity
                interaction_sim = 0.0
                if user_interactions:
                    tot_int_weight = sum(user_interactions.values())
                    if tot_int_weight > 0:
                        sim_sum = 0.0
                        for past_id, weight in user_interactions.items():
                            if mid == past_id:
                                sim_val = 1.0
                            else:
                                sim_val = cls._get_item_similarity(mid, past_id, model)
                                past_cat = menu_items.get(past_id, {}).get("category")
                                if past_cat and past_cat == cat:
                                    sim_val = max(sim_val, 0.35)
                            sim_sum += sim_val * weight
                        interaction_sim = sim_sum / tot_int_weight

                # Combined user preference signal
                if user_ratings and user_interactions:
                    user_pref_score = 0.65 * norm_pred + 0.35 * interaction_sim
                elif user_ratings:
                    user_pref_score = norm_pred
                else:
                    user_pref_score = interaction_sim

                # 3. Bayesian item quality
                bayes_score = model["food_bayes_scores"].get(mid, 3.5)
                norm_bayes = bayes_score / 5.0

                # 4. Preference match boost
                pref_boost = 0.20 if cat in preferred_cats else 0.0

                # 5. Normalized popularity
                norm_pop = model["popularity_scores"].get(mid, 0.0)

                # Composite personalized score: user preference is prioritized over popularity
                final_score = (
                    0.50 * user_pref_score
                    + 0.25 * norm_bayes
                    + 0.15 * pref_boost
                    + 0.10 * norm_pop
                )

                # Reason determination
                if user_ratings and mid not in user_ratings and top_anchor_name:
                    sim_to_top = cls._get_item_similarity(mid, top_rated_entry[0], model) if user_ratings else 0.0
                    if sim_to_top > 0.15:
                        reason = f"Similar taste to {top_anchor_name}"
                    elif pred_rating >= 4.0:
                        reason = f"Predicted {pred_rating:.1f}★ based on your ratings"
                    else:
                        reason = "Personalized match for your taste"
                elif user_interactions:
                    reason = f"Pairs well with {top_anchor_name}" if top_anchor_name else "Recommended from your orders"
                else:
                    reason = "Personalized selection"

                scored_candidates.append({
                    "item": item,
                    "score": final_score,
                    "reason": reason,
                    "predicted_rating": pred_rating,
                })

            scored_candidates.sort(key=lambda x: x["score"], reverse=True)
            diverse_items = cls._apply_category_diversity(scored_candidates, limit, model=model, user_key=user_key)

            return {
                "title": "Recommended for You",
                "badge": "PERSONALIZED FOR YOU",
                "is_cold_start": False,
                "description": "Curated with Item-to-Item Collaborative Filtering based on your authentic ratings.",
                "items": diverse_items,
            }

        # ── Cold Start Fallback ───────────────────────────────────────────────
        cold_candidates = []
        for mid, item in menu_items.items():
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue
            if mid in cart_set:
                continue

            cat = item.get("category", "")
            if diet_pref == "VEG" and cat == "Non-Vegetarian":
                continue

            bayes_score = model["food_bayes_scores"].get(mid, 3.5)
            pop_score = model["popularity_scores"].get(mid, 0.0)
            pref_boost = 0.25 if cat in preferred_cats else 0.0

            score = 0.50 * (bayes_score / 5.0) + 0.30 * pop_score + pref_boost
            rating_cnt = model["food_rating_counts"].get(mid, 0)
            avg_r = model["food_avg_ratings"].get(mid)

            if avg_r and avg_r >= 4.5:
                reason = f"Top rated ({avg_r}★ from {rating_cnt} diners)"
            elif pop_score > 0.3:
                reason = "Popular canteen favorite"
            else:
                reason = "Campus chef recommendation"

            cold_candidates.append({
                "item": item,
                "score": score,
                "reason": reason,
                "predicted_rating": bayes_score,
            })

        cold_candidates.sort(key=lambda x: x["score"], reverse=True)
        diverse_items = cls._apply_category_diversity(cold_candidates, limit, model=model, user_key=user_key)

        return {
            "title": "Popular at Smart Canteen",
            "badge": "CAMPUS BESTSELLERS",
            "is_cold_start": True,
            "description": "Top-rated and most-loved dishes across the campus.",
            "items": diverse_items,
        }

    @classmethod
    def _build_because_you_rated(
        cls,
        model: Dict[str, Any],
        user_key: Optional[str],
        user_ratings: Dict[int, float],
        cart_set: Set[int],
        limit: int,
    ) -> Dict[str, Any]:
        """
        Section 2: "Because You Rated This"
        Recommends companion food items based on the food item the user rated highest.
        """
        menu_items = model["menu_items"]
        anchor_item = None
        anchor_rating = 0.0

        if user_ratings:
            # Pick user's highest rated food (>= 3 stars)
            sorted_ratings = sorted(user_ratings.items(), key=lambda x: (x[1], x[0]), reverse=True)
            for fid, r_val in sorted_ratings:
                if fid in menu_items:
                    anchor_item = menu_items[fid]
                    anchor_rating = r_val
                    break

        # Fallback anchor if user hasn't rated anything yet: highest Bayesian rated item
        is_fallback_anchor = False
        if not anchor_item:
            sorted_by_bayes = sorted(
                model["food_bayes_scores"].items(), key=lambda x: x[1], reverse=True
            )
            for mid, score in sorted_by_bayes:
                if mid in menu_items and menu_items[mid].get("stock", 0) > 0:
                    anchor_item = menu_items[mid]
                    anchor_rating = model["food_avg_ratings"].get(mid, 4.5) or 4.5
                    is_fallback_anchor = True
                    break

        if not anchor_item:
            return {
                "title": "Because You Rated",
                "badge": "RELATED PICKS",
                "anchor_item": None,
                "items": [],
            }

        anchor_id = anchor_item["id"]
        anchor_name = anchor_item["name"]

        candidates = []
        for mid, item in menu_items.items():
            if mid == anchor_id:
                continue
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue
            if mid in cart_set:
                continue

            sim = cls._get_item_similarity(mid, anchor_id, model)
            bayes_score = model["food_bayes_scores"].get(mid, 3.5) / 5.0
            co_cnt = cls._get_item_cooccurrence(mid, anchor_id, model)

            score = 0.65 * sim + 0.20 * (min(co_cnt, 5) / 5.0) + 0.15 * bayes_score

            if sim > 0.1:
                reason = f"{int(sim * 100)}% taste match to {anchor_name}"
            elif co_cnt > 0:
                reason = f"Frequently ordered with {anchor_name}"
            else:
                reason = f"Pairs wonderfully with {anchor_name}"

            candidates.append({
                "item": item,
                "score": score,
                "reason": reason,
                "similarity_score": sim,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        diverse_items = cls._apply_category_diversity(candidates, limit, model=model, user_key=user_key)

        subtitle = (
            f"Because you rated {anchor_name} ({int(anchor_rating)}★)"
            if not is_fallback_anchor
            else f"Because you loved {anchor_name}"
        )

        return {
            "title": "Because You Rated This",
            "badge": "SIMILAR FLAVOURS",
            "subtitle": subtitle,
            "anchor_item": {
                "id": anchor_id,
                "name": anchor_name,
                "rating": anchor_rating,
            },
            "items": diverse_items,
        }

    @classmethod
    def _build_because_you_ordered(
        cls,
        model: Dict[str, Any],
        user_key: Optional[str],
        cart_set: Set[int],
        limit: int,
    ) -> Dict[str, Any]:
        """
        Section 3: "Because You Ordered This"
        Recommendations anchored to the user's most recently ordered food item.
        """
        menu_items = model["menu_items"]
        anchor_item = None

        if user_key:
            user_history = model["user_order_history"].get(user_key, [])
            if user_history:
                for ord_entry in reversed(user_history):
                    for mid in ord_entry.get("items", []):
                        if mid in menu_items:
                            anchor_item = menu_items[mid]
                            break
                    if anchor_item:
                        break

        # Fallback anchor if user has no past order: canteen #1 bestseller
        is_fallback_anchor = False
        if not anchor_item:
            sorted_by_pop = sorted(
                model["item_order_counts"].items(), key=lambda x: x[1], reverse=True
            )
            for mid, cnt in sorted_by_pop:
                if mid in menu_items and menu_items[mid].get("stock", 0) > 0:
                    anchor_item = menu_items[mid]
                    is_fallback_anchor = True
                    break

        if not anchor_item:
            for mid, itm in menu_items.items():
                if itm.get("stock", 0) > 0:
                    anchor_item = itm
                    is_fallback_anchor = True
                    break

        if not anchor_item:
            return {
                "title": "Because You Ordered",
                "badge": "RELATED PICKS",
                "anchor_item": None,
                "items": [],
            }

        anchor_id = anchor_item["id"]
        anchor_name = anchor_item["name"]

        candidates = []
        for mid, item in menu_items.items():
            if mid == anchor_id:
                continue
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue
            if mid in cart_set:
                continue

            sim = cls._get_item_similarity(mid, anchor_id, model)
            co_cnt = cls._get_item_cooccurrence(mid, anchor_id, model)
            pop = model["popularity_scores"].get(mid, 0.0)

            score = 0.60 * sim + 0.25 * (min(co_cnt, 5) / 5.0) + 0.15 * pop

            if co_cnt > 0:
                reason = f"Frequently ordered with {anchor_name}"
            elif sim > 0.1:
                reason = f"Similar ordering patterns to {anchor_name}"
            else:
                reason = f"Pairs well with {anchor_name}"

            candidates.append({
                "item": item,
                "score": score,
                "reason": reason,
                "similarity_score": sim,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        diverse_items = cls._apply_category_diversity(candidates, limit, model=model, user_key=user_key)

        subtitle = (
            f"Because you ordered {anchor_name}"
            if not is_fallback_anchor
            else f"Because diners loved {anchor_name}"
        )

        return {
            "title": "Because You Ordered This",
            "badge": "RELATED PICKS",
            "subtitle": subtitle,
            "anchor_item": {
                "id": anchor_id,
                "name": anchor_name,
            },
            "items": diverse_items,
        }

    @classmethod
    def _build_frequently_ordered_together(
        cls,
        model: Dict[str, Any],
        cart_item_ids: List[int],
        user_key: Optional[str],
        limit: int = 4,
    ) -> Dict[str, Any]:
        """
        Section 4: "Frequently Ordered Together"
        Identifies pairs of items that appear together in the same order.
        Displays both items with individual prices and 1-click 'Add Both to Cart'.
        """
        menu_items = model["menu_items"]
        pairs_data = []

        anchor_ids = set(cart_item_ids)
        if not anchor_ids and user_key:
            user_history = model["user_order_history"].get(user_key, [])
            if user_history:
                anchor_ids = set(user_history[-1]["items"])

        for (id_a, id_b), count in model["pair_cooccurrence"].items():
            if count <= 0:
                continue
            item_a = menu_items.get(id_a)
            item_b = menu_items.get(id_b)

            if not item_a or not item_b:
                continue
            if (
                not item_a.get("active")
                or not item_b.get("active")
                or item_a.get("stock", 0) <= 0
                or item_b.get("stock", 0) <= 0
            ):
                continue

            jacc = model["jaccard_sim"].get((id_a, id_b), 0.0)
            is_anchor_match = bool(anchor_ids and (id_a in anchor_ids or id_b in anchor_ids))
            priority = (2.0 if is_anchor_match else 1.0) * (count * 10.0 + jacc)

            total_combo_price = float(item_a["price"]) + float(item_b["price"])
            pairs_data.append({
                "item_a": cls._format_card(item_a, f"Frequently ordered with {item_b['name']}", model=model),
                "item_b": cls._format_card(item_b, f"Frequently ordered with {item_a['name']}", model=model),
                "cooccurrence_count": count,
                "jaccard_similarity": round(jacc, 3),
                "combo_price": total_combo_price,
                "priority": priority,
                "reason": f"Ordered together {count} time{'s' if count > 1 else ''} by students",
            })

        pairs_data.sort(key=lambda x: x["priority"], reverse=True)
        selected_pairs = pairs_data[:limit]

        return {
            "title": "Frequently Ordered Together",
            "badge": "POPULAR PAIRINGS",
            "description": "Dishes frequently enjoyed together in a single order.",
            "pairs": selected_pairs,
        }

    @classmethod
    def _build_top_rated_food(
        cls,
        model: Dict[str, Any],
        exclude_ids: Set[int],
        limit: int,
    ) -> Dict[str, Any]:
        """
        Section 5: "Top Rated Food"
        Ranks available food strictly using Bayesian smoothed ratings.
        """
        menu_items = model["menu_items"]
        candidates = []

        for mid, item in menu_items.items():
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue
            if mid in exclude_ids:
                continue

            bayes = model["food_bayes_scores"].get(mid, 3.5)
            cnt = model["food_rating_counts"].get(mid, 0)
            avg_r = model["food_avg_ratings"].get(mid)

            if avg_r is not None:
                reason = f"Rated {avg_r}★ by {cnt} diner{'s' if cnt > 1 else ''}"
            else:
                reason = "Popular canteen selection"

            candidates.append({
                "item": item,
                "score": bayes,
                "reason": reason,
                "predicted_rating": bayes,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        diverse_items = cls._apply_category_diversity(candidates, limit, model=model)

        return {
            "title": "Top Rated Food",
            "badge": "HIGHEST RATED",
            "description": "Highest rated South Indian canteen specialties by genuine student reviews.",
            "items": diverse_items,
        }

    @classmethod
    def _build_popular_items(
        cls,
        model: Dict[str, Any],
        exclude_ids: Set[int],
        limit: int,
    ) -> Dict[str, Any]:
        """
        Section 6: "Popular at Smart Canteen"
        Bestsellers ranked by eligible order frequency.
        """
        menu_items = model["menu_items"]
        candidates = []

        for mid, item in menu_items.items():
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue
            if mid in exclude_ids:
                continue

            pop_score = model["popularity_scores"].get(mid, 0.0)
            order_count = model["item_order_counts"].get(mid, 0)
            reason = (
                f"Ordered {order_count} time{'s' if order_count != 1 else ''} this term"
                if order_count > 0
                else "Campus chef pick"
            )

            candidates.append({
                "item": item,
                "score": pop_score,
                "reason": reason,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        diverse_items = cls._apply_category_diversity(candidates, limit, model=model)

        return {
            "title": "Popular at Smart Canteen",
            "badge": "MOST ORDERED",
            "description": "Student favorites with the highest order volume this semester.",
            "items": diverse_items,
        }

    @classmethod
    def _build_try_something_new(
        cls,
        model: Dict[str, Any],
        user_key: Optional[str],
        user_ratings: Dict[int, float],
        user_interactions: Dict[int, float],
        cart_set: Set[int],
        limit: int,
    ) -> Dict[str, Any]:
        """
        Section 7: "Discover Something New"
        Identifies high-quality, relevant items the user has NEVER rated and NEVER ordered.
        """
        menu_items = model["menu_items"]
        known_ids = set(user_ratings.keys()) | set(user_interactions.keys()) | cart_set

        candidates = []
        for mid, item in menu_items.items():
            if mid in known_ids:
                continue
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue

            # Quality + slight popularity discovery score
            bayes = model["food_bayes_scores"].get(mid, 3.5) / 5.0
            pop = model["popularity_scores"].get(mid, 0.0)
            score = 0.70 * bayes + 0.30 * pop

            avg_r = model["food_avg_ratings"].get(mid)
            if avg_r:
                reason = f"Rated {avg_r}★ by fellow students"
            else:
                reason = "Fresh new dish on campus"

            candidates.append({
                "item": item,
                "score": score,
                "reason": reason,
                "predicted_rating": model["food_bayes_scores"].get(mid, 3.5),
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        diverse_items = cls._apply_category_diversity(candidates, limit, model=model, user_key=user_key)

        return {
            "title": "Discover Something New",
            "badge": "EXPLORE FLAVOURS",
            "description": "Delectable dishes you have not rated yet, selected for your palate.",
            "items": diverse_items,
        }

    @classmethod
    def _build_favourite_categories(
        cls,
        model: Dict[str, Any],
        user_key: Optional[str],
        user_ratings: Dict[int, float],
        cart_set: Set[int],
        limit: int,
    ) -> Dict[str, Any]:
        """
        Section 8: "Your Favourite Categories"
        Suggests top dishes from categories where user has high ratings or saved preferences.
        """
        menu_items = model["menu_items"]
        prefs = model["user_prefs"].get(user_key, {}) if user_key else {}
        preferred_cats = set(prefs.get("preferred_categories", []))

        # Compute average user rating per category
        cat_ratings: Dict[str, List[float]] = {}
        for fid, r_val in user_ratings.items():
            cat = menu_items.get(fid, {}).get("category")
            if cat:
                cat_ratings.setdefault(cat, []).append(r_val)

        fav_cat = None
        if cat_ratings:
            fav_cat = max(cat_ratings.keys(), key=lambda c: sum(cat_ratings[c]) / len(cat_ratings[c]))
        elif preferred_cats:
            fav_cat = list(preferred_cats)[0]
        else:
            fav_cat = "Breakfast"

        candidates = []
        for mid, item in menu_items.items():
            if item.get("category") != fav_cat:
                continue
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue
            if mid in cart_set:
                continue

            bayes = model["food_bayes_scores"].get(mid, 3.5)
            candidates.append({
                "item": item,
                "score": bayes,
                "reason": f"Top pick in your favorite category ({fav_cat})",
                "predicted_rating": bayes,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        items = [
            cls._format_card(c["item"], c["reason"], model=model, user_key=user_key)
            for c in candidates[:limit]
        ]

        return {
            "title": f"Best in {fav_cat}",
            "badge": "FAVOURITE CATEGORY",
            "category": fav_cat,
            "description": f"Top selections from {fav_cat}, your most-rated culinary style.",
            "items": items,
        }

    @classmethod
    def _build_quick_bites(
        cls,
        model: Dict[str, Any],
        user_key: Optional[str],
        user_interactions: Dict[int, float],
        cart_set: Set[int],
        limit: int,
    ) -> Dict[str, Any]:
        """
        Section 9: "Quick Bites for You"
        Fast snacks and beverages tailored to taste.
        """
        menu_items = model["menu_items"]
        candidates = []

        for mid, item in menu_items.items():
            cat = item.get("category", "")
            if cat not in ("Snacks", "Beverages"):
                continue
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue
            if mid in cart_set:
                continue

            bayes = model["food_bayes_scores"].get(mid, 3.5) / 5.0
            pop = model["popularity_scores"].get(mid, 0.0)
            prep_bonus = 0.2 if item.get("prep_time_minutes", 10) <= 3 else 0.0

            score = 0.50 * bayes + 0.30 * pop + prep_bonus
            reason = f"Ready in ~{item.get('prep_time_minutes', 3)} mins"

            candidates.append({
                "item": item,
                "score": score,
                "reason": reason,
                "predicted_rating": model["food_bayes_scores"].get(mid, 3.5),
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        items = [
            cls._format_card(c["item"], c["reason"], model=model, user_key=user_key)
            for c in candidates[:limit]
        ]

        return {
            "title": "Quick Bites for You",
            "badge": "SPEEDY PICKS",
            "description": "Delicious snacks & hot beverages prepared in 3 minutes or less.",
            "items": items,
        }

    @classmethod
    def get_related_foods(cls, food_id: int, limit: int = 4) -> List[Dict[str, Any]]:
        """
        Return items most similar to food_id using Item-to-Item Collaborative Filtering
        based on overlapping user rating vectors and co-occurrence.
        """
        model = cls._get_model()
        menu_items = model["menu_items"]
        if food_id not in menu_items:
            return []

        target_item = menu_items[food_id]
        candidates = []
        for other_id, other_item in menu_items.items():
            if other_id == food_id:
                continue
            if not other_item.get("active") or other_item.get("stock", 0) <= 0:
                continue

            pair = (min(food_id, other_id), max(food_id, other_id))
            sim = cls._get_item_similarity(food_id, other_id, model)
            jacc = model["jaccard_sim"].get(pair, 0.0)
            combined_sim = 0.70 * sim + 0.30 * jacc

            # Category affinity bonus
            cat_bonus = 0.15 if other_item.get("category") == target_item.get("category") else 0.0
            total_score = combined_sim + cat_bonus

            # Informative human-readable reason
            if sim > 0.3:
                reason = f"Diners who rated {target_item['name']} also liked this"
            elif jacc > 0.2:
                reason = f"Frequently paired with {target_item['name']}"
            elif cat_bonus > 0:
                reason = f"Similar {other_item.get('category')} favorite"
            else:
                reason = "Popular canteen companion"

            candidates.append({
                "item": other_item,
                "score": total_score,
                "similarity_score": round(sim, 2),
                "reason": reason,
            })

        candidates.sort(key=lambda x: (x["score"], model["food_bayes_scores"].get(x["item"]["id"], 0.0)), reverse=True)
        return [
            cls._format_card(
                c["item"],
                c["reason"],
                model=model,
                similarity_score=c["similarity_score"]
            )
            for c in candidates[:limit]
        ]

    # ──────────────────────────────────────────────────────────────────────────
    # ADMIN INSIGHTS & ANALYTICS
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def get_admin_insights(cls) -> Dict[str, Any]:
        """
        Calculate transparent recommendation and ratings analytics for Admin Dashboard.
        Strictly distinguishes measured values from calculated values.
        """
        model = cls._get_model()
        menu_items = model["menu_items"]

        # 1. Frequently ordered pairs
        pairs_list = []
        total_eligible = max(1, model["eligible_orders_count"])
        for (id_a, id_b), count in model["pair_cooccurrence"].items():
            name_a = menu_items.get(id_a, {}).get("name", f"Item #{id_a}")
            name_b = menu_items.get(id_b, {}).get("name", f"Item #{id_b}")
            pairs_list.append({
                "item_1_id": id_a,
                "item_1_name": name_a,
                "item_2_id": id_b,
                "item_2_name": name_b,
                "cooccurrence_count": count,
                "order_percentage": round((count / total_eligible) * 100.0, 1),
            })
        pairs_list.sort(key=lambda x: x["cooccurrence_count"], reverse=True)
        top_pairs = pairs_list[:8]

        # 2. Most frequently recommended items
        with cls._metrics_lock:
            rec_counts_copy = dict(cls._stats_item_rec_counts)
            req_served = cls._stats_requests_served
            cold_served = cls._stats_cold_start_served

        top_recommended = []
        for mid, count in sorted(rec_counts_copy.items(), key=lambda x: x[1], reverse=True)[:8]:
            it = menu_items.get(mid)
            if it:
                top_recommended.append({
                    "item_id": mid,
                    "item_name": it["name"],
                    "category": it["category"],
                    "times_recommended": count,
                })

        # 3. Items with sufficient similarity data
        items_with_sim = 0
        for mid in menu_items:
            has_sim = False
            for (id_a, id_b), sim in model["blended_sim"].items():
                if (id_a == mid or id_b == mid) and sim > 0.01:
                    has_sim = True
                    break
            if has_sim:
                items_with_sim += 1

        cold_start_pct = (
            round((cold_served / req_served) * 100.0, 1) if req_served > 0 else 0.0
        )

        # 4. Rating metrics from model
        total_ratings = model["ratings_count"]
        ratings_sum = sum(
            sum(model["food_ratings"].get(mid, {}).values()) for mid in menu_items
        )
        avg_rating = round(ratings_sum / float(total_ratings), 2) if total_ratings > 0 else 0.0

        # Most rated items
        most_rated = []
        for mid, cnt in sorted(model["food_rating_counts"].items(), key=lambda x: x[1], reverse=True)[:5]:
            if cnt > 0 and mid in menu_items:
                most_rated.append({
                    "id": mid,
                    "name": menu_items[mid]["name"],
                    "category": menu_items[mid]["category"],
                    "rating_count": cnt,
                    "average_rating": model["food_avg_ratings"].get(mid),
                })

        # Top rated items (min 2 ratings)
        top_rated_items = []
        for mid, bayes in sorted(model["food_bayes_scores"].items(), key=lambda x: x[1], reverse=True):
            if mid in menu_items and model["food_rating_counts"].get(mid, 0) >= 2:
                top_rated_items.append({
                    "id": mid,
                    "name": menu_items[mid]["name"],
                    "category": menu_items[mid]["category"],
                    "rating_count": model["food_rating_counts"].get(mid, 0),
                    "average_rating": model["food_avg_ratings"].get(mid),
                    "bayes_score": round(bayes, 2),
                })
                if len(top_rated_items) >= 5:
                    break

        return {
            "success": True,
            "measured_analytics": {
                "eligible_orders_count": model["eligible_orders_count"],
                "total_menu_items": len(menu_items),
                "total_ratings": total_ratings,
                "global_average_rating": avg_rating,
                "users_with_rating_history": len(model["user_ratings"]),
                "items_with_similarity_data": items_with_sim,
                "recommendation_requests_served": req_served,
                "cold_start_fallback_usage": cold_served,
                "cold_start_fallback_percentage": cold_start_pct,
            },
            "most_frequently_ordered_pairs": top_pairs,
            "most_frequently_recommended_items": top_recommended,
            "most_rated_items": most_rated,
            "top_rated_items": top_rated_items,
            "algorithm_parameters": {
                "model_type": "Item-to-Item Collaborative Filtering (Cosine Shrinkage + Jaccard Association)",
                "weights": {
                    "item_similarity": REC_WEIGHT_ITEM_SIMILARITY,
                    "user_ratings": REC_WEIGHT_USER_RATINGS,
                    "item_quality": REC_WEIGHT_ITEM_QUALITY,
                    "cooccurrence": REC_WEIGHT_COOCCURRENCE,
                    "popularity": REC_WEIGHT_POPULARITY,
                },
                "cache_ttl_seconds": REC_CACHE_TTL_SECONDS,
                "recency_decay_days": REC_RECENCY_DECAY_DAYS,
                "bayesian_prior_rating": REC_BAYESIAN_PRIOR_RATING,
                "bayesian_prior_count": REC_BAYESIAN_PRIOR_COUNT,
            },
        }

    # ──────────────────────────────────────────────────────────────────────────
    # EVALUATION METRICS
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def evaluate_offline_ranking(cls, k: int = 3) -> Dict[str, Any]:
        """
        Evaluate recommendation relevance using Leave-One-Out offline ranking:
        Precision@K and Recall@K across all users with at least 2 interactions.
        Supports both explicit ratings and order history.
        """
        model = cls._get_model()
        user_ratings = model["user_ratings"]
        user_history = model["user_order_history"]
        menu_items = model["menu_items"]

        eligible_users = []
        # First preference: explicit ratings
        for u_key, r_dict in user_ratings.items():
            if len(r_dict) >= 2:
                eligible_users.append((u_key, list(r_dict.keys())))

        # If sparse on ratings, also check order history
        if len(eligible_users) < 2:
            for u_key, orders in user_history.items():
                all_items = []
                for o in orders:
                    all_items.extend(o.get("items", []))
                distinct_items = list(dict.fromkeys(all_items))
                if len(distinct_items) >= 2 and u_key not in [u[0] for u in eligible_users]:
                    eligible_users.append((u_key, distinct_items))

        if not eligible_users:
            return {
                "evaluated_users": 0,
                "k": k,
                "precision_at_k": 0.0,
                "recall_at_k": 0.0,
                "message": "Insufficient multi-item user history for offline ranking evaluation.",
            }

        hits = 0
        total_eval = len(eligible_users)

        for u_key, distinct_items in eligible_users:
            target_item_id = distinct_items[-1]
            train_items = distinct_items[:-1]

            candidates = []
            for mid, item in menu_items.items():
                if mid in train_items:
                    continue
                sim_sum = sum(
                    cls._get_item_similarity(mid, h_id, model) for h_id in train_items
                )
                candidates.append((mid, sim_sum))

            candidates.sort(key=lambda x: x[1], reverse=True)
            top_k_ids = [c[0] for c in candidates[:k]]

            if target_item_id in top_k_ids:
                hits += 1

        precision_at_k = round(hits / (total_eval * k), 4)
        recall_at_k = round(hits / total_eval, 4)

        return {
            "evaluated_users": total_eval,
            "k": k,
            "hits": hits,
            "precision_at_k": precision_at_k,
            "recall_at_k": recall_at_k,
            "message": f"Evaluated {total_eval} users: Precision@{k}={precision_at_k}, Recall@{k}={recall_at_k}.",
        }

    # ──────────────────────────────────────────────────────────────────────────
    # AUTHENTIC DATA SEEDER
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def seed_demo_orders(cls, count: int = 25, force: bool = False) -> int:
        """Seed realistic historical orders for demonstration purposes."""
        with _DB_LOCK:
            conn = get_db_connection()
            try:
                cur = conn.execute("SELECT COUNT(*) as cnt FROM orders")
                existing = cur.fetchone()["cnt"]
                if existing > 0 and not force:
                    return 0

                cur_seq = conn.execute("SELECT next_seq FROM token_sequence WHERE id = 1")
                row_seq = cur_seq.fetchone()
                next_seq = row_seq["next_seq"] if row_seq else 1

                combos = [
                    [(2, "Masala Dosa", 50, "MAIN_DISH", 1), (15, "Filter Coffee", 25, "BEVERAGES", 1)],
                    [(1, "Idli", 30, "MAIN_DISH", 2), (4, "Vada", 20, "SNACKS", 1), (15, "Filter Coffee", 25, "BEVERAGES", 1)],
                    [(3, "Pongal", 45, "MAIN_DISH", 1), (4, "Vada", 20, "SNACKS", 1)],
                    [(11, "Chicken Biriyani", 120, "MAIN_DISH", 1), (13, "Chicken 65", 100, "SNACKS", 1)],
                    [(11, "Chicken Biriyani", 120, "MAIN_DISH", 1), (16, "Fresh Lime Juice", 25, "BEVERAGES", 1)],
                    [(10, "Parotta", 40, "MAIN_DISH", 2), (14, "Tea", 15, "BEVERAGES", 1)],
                    [(12, "Veg Meals", 80, "MAIN_DISH", 1), (16, "Fresh Lime Juice", 25, "BEVERAGES", 1)],
                    [(6, "Sambar Rice", 45, "MAIN_DISH", 1), (4, "Vada", 20, "SNACKS", 1)],
                    [(18, "Samosa", 20, "SNACKS", 2), (14, "Tea", 15, "BEVERAGES", 1)],
                    [(5, "Poori Masala", 50, "MAIN_DISH", 1), (15, "Filter Coffee", 25, "BEVERAGES", 1)],
                ]

                users = [
                    (1, "student@smartcanteen.com", "Karthik Raja"),
                    (2, "priya@smartcanteen.com", "Priya Sundaram"),
                    (3, "anand@smartcanteen.com", "Anand Kumar"),
                    (4, "divya@smartcanteen.com", "Divya Ramesh"),
                    (5, "suresh@smartcanteen.com", "Suresh Babu"),
                ]

                inserted = 0
                now_dt = datetime.now(timezone.utc)

                conn.execute("BEGIN IMMEDIATE")
                for i in range(count):
                    u_id, u_email, u_name = users[i % len(users)]
                    combo = combos[i % len(combos)]

                    token = f"{TOKEN_PREFIX}{str(next_seq).zfill(TOKEN_PADDING)}"
                    next_seq += 1

                    total = sum(item[2] * item[4] for item in combo)
                    ts_iso = now_dt.isoformat()

                    cur_ord = conn.execute("""
                        INSERT INTO orders (
                            token, student_name, student_email, user_id, status, total,
                            payment_method, transaction_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'UPI', ?, ?, ?)
                    """, (
                        token, u_name, u_email, u_id, STATUS_COMPLETED, total,
                        f"DEMO-TXN-{next_seq}", ts_iso, ts_iso
                    ))
                    order_id = cur_ord.lastrowid

                    for mid, mname, mprice, mstation, mqty in combo:
                        conn.execute("""
                            INSERT INTO order_items (
                                order_id, menu_item_id, item_name, quantity, price, station_code
                            ) VALUES (?, ?, ?, ?, ?, ?)
                        """, (order_id, mid, mname, mqty, mprice, mstation))

                    inserted += 1

                conn.execute("UPDATE token_sequence SET next_seq = ? WHERE id = 1", (next_seq,))
                conn.execute("COMMIT")

                cls.invalidate_cache()
                return inserted

            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
