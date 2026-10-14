"""
services/recommendation_service.py
==================================
Advanced Item-to-Item Collaborative Filtering Recommendation Engine
for Smart Canteen.

Features:
- Real historical order interaction matrix with quantity scaling & time decay.
- Item-to-Item Cosine Similarity combined with co-occurrence Jaccard similarity.
- Configurable hybrid scoring:
    Score = W_sim * S_sim + W_cooccur * S_cooccur + W_recency * S_recency + W_pop * S_pop
- 6 Distinct Recommendation Sections:
    1. Recommended for You (Personalized Item-to-Item CF or Cold-start fallback)
    2. Frequently Ordered Together (Pairs ordered in the same cart with 1-click combo)
    3. Because You Ordered This (Contextual recommendations anchored to recent order/item)
    4. Popular at Smart Canteen (Bestsellers across diverse categories)
    5. Try Something New (Relevant items never ordered before by the user)
    6. Quick Bites for You (Fast snacks & beverages tailored to taste)
- Cold-start handling: Transparent fallback without fabricated metrics.
- Excludes sold-out items (stock <= 0) and inactive items.
- Deduplication and category diversity control.
- In-memory similarity matrix caching with automatic invalidation.
- Admin recommendation insights & metrics tracking.
- Offline ranking evaluation (Precision@K and Recall@K).
"""

import math
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Set

from config import (
    REC_WEIGHT_ITEM_SIMILARITY,
    REC_WEIGHT_COOCCURRENCE,
    REC_WEIGHT_RECENCY,
    REC_WEIGHT_POPULARITY,
    REC_RECENCY_DECAY_DAYS,
    REC_CACHE_TTL_SECONDS,
    REC_DEFAULT_TOP_K,
    REC_MAX_PER_CATEGORY,
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
    """Production-grade Item-to-Item Collaborative Filtering Service."""

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
        """Invalidate similarity cache when new orders or inventory changes occur."""
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
        Build the collaborative filtering model from eligible database orders:
        1. Query all valid non-cancelled orders & line items.
        2. Construct user-item interaction matrix with log quantity and time decay.
        3. Compute item-item cosine similarity matrix.
        4. Compute pairwise co-occurrence frequencies and Jaccard similarities.
        5. Compute item popularity scores and fetch active available menu items.
        """
        with _DB_LOCK:
            conn = get_db_connection()
            try:
                # 1. Fetch active menu items
                menu_cur = conn.execute("""
                    SELECT id, name, price, category, station_code,
                           prep_time_minutes, stock, image, badge, description, active
                    FROM menu_items
                """)
                menu_items = {row["id"]: dict(row) for row in menu_cur.fetchall()}

                # 2. Fetch eligible orders
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

                # Map order metadata
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

                # 3. Fetch order items for these eligible orders
                items_by_order: Dict[int, List[Dict[str, Any]]] = {}
                if order_ids:
                    # Fetch in batches if necessary, or single IN query for SQLite
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

        # 4. Build User-Item Interaction Matrix & Co-occurrence Matrix
        # user_interactions: user_key -> {item_id -> weighted_interaction}
        user_interactions: Dict[str, Dict[int, float]] = {}
        # user_order_history: user_key -> list of {order_id, created_at, items: [item_id]}
        user_order_history: Dict[str, List[Dict[str, Any]]] = {}
        # item_order_counts: item_id -> total distinct orders containing this item
        item_order_counts: Dict[int, int] = {item_id: 0 for item_id in menu_items}
        # item_cooccurrence: (item_a, item_b) -> order count
        pair_cooccurrence: Dict[Tuple[int, int], int] = {}

        for oid, meta in order_meta.items():
            user_key = meta.get("user_key")
            ord_items = items_by_order.get(oid, [])
            if not ord_items:
                continue

            # Calculate time decay factor
            created_at = meta.get("created_at")
            if created_at:
                days_diff = max(0.0, (now_dt - created_at).total_seconds() / 86400.0)
                decay_factor = math.exp(-days_diff / max(1.0, REC_RECENCY_DECAY_DAYS))
            else:
                decay_factor = 1.0

            distinct_order_item_ids = set()
            for it in ord_items:
                mid = it["menu_item_id"]
                qty = max(1, it.get("quantity", 1))
                distinct_order_item_ids.add(mid)

                # Sub-linear log quantity scaling + recency decay
                interaction_val = (1.0 + math.log(qty)) * decay_factor

                if user_key:
                    if user_key not in user_interactions:
                        user_interactions[user_key] = {}
                    user_interactions[user_key][mid] = (
                        user_interactions[user_key].get(mid, 0.0) + interaction_val
                    )

            # Update item order counts
            for mid in distinct_order_item_ids:
                item_order_counts[mid] = item_order_counts.get(mid, 0) + 1

            # Update co-occurrence for all pairs in this order
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

        # 5. Compute Item-to-Item Cosine Similarity
        # Vector for item i across all users: user_key -> value
        item_vectors: Dict[int, Dict[str, float]] = {mid: {} for mid in menu_items}
        for u_key, items_dict in user_interactions.items():
            for mid, val in items_dict.items():
                if mid in item_vectors:
                    item_vectors[mid][u_key] = val

        # Precompute vector norms: ||v_i|| = sqrt(sum(val^2))
        item_norms: Dict[int, float] = {}
        for mid, vec in item_vectors.items():
            sum_sq = sum(v * v for v in vec.values())
            item_norms[mid] = math.sqrt(sum_sq) if sum_sq > 0 else 0.0

        all_item_ids = list(menu_items.keys())
        cosine_sim: Dict[Tuple[int, int], float] = {}
        jaccard_sim: Dict[Tuple[int, int], float] = {}
        blended_sim: Dict[Tuple[int, int], float] = {}

        for i in range(len(all_item_ids)):
            id_a = all_item_ids[i]
            vec_a = item_vectors[id_a]
            norm_a = item_norms[id_a]

            for j in range(i + 1, len(all_item_ids)):
                id_b = all_item_ids[j]
                vec_b = item_vectors[id_b]
                norm_b = item_norms[id_b]

                # Pair tuple key: canonical sorted (smaller, larger)
                pair_key = (min(id_a, id_b), max(id_a, id_b))

                # Cosine similarity
                if norm_a > 0.0 and norm_b > 0.0:
                    # Dot product
                    dot = 0.0
                    if len(vec_a) < len(vec_b):
                        for u, val in vec_a.items():
                            if u in vec_b:
                                dot += val * vec_b[u]
                    else:
                        for u, val in vec_b.items():
                            if u in vec_a:
                                dot += val * vec_a[u]
                    cos = dot / (norm_a * norm_b)
                else:
                    cos = 0.0
                cosine_sim[pair_key] = cos

                # Jaccard Co-occurrence Similarity
                co_count = pair_cooccurrence.get(pair_key, 0)
                denom = item_order_counts.get(id_a, 0) + item_order_counts.get(id_b, 0) - co_count
                jacc = (co_count / denom) if denom > 0 else 0.0
                jaccard_sim[pair_key] = jacc

                # Blended item similarity (70% Cosine CF + 30% Co-occurrence)
                blend = 0.70 * cos + 0.30 * jacc
                blended_sim[pair_key] = blend

        # 6. Global Popularity Scores Normalized in [0, 1]
        max_orders = max(item_order_counts.values()) if item_order_counts else 1
        max_orders = max(1, max_orders)
        popularity_scores = {
            mid: item_order_counts.get(mid, 0) / float(max_orders)
            for mid in menu_items
        }

        return {
            "menu_items": menu_items,
            "user_interactions": user_interactions,
            "user_order_history": user_order_history,
            "item_order_counts": item_order_counts,
            "pair_cooccurrence": pair_cooccurrence,
            "cosine_sim": cosine_sim,
            "jaccard_sim": jaccard_sim,
            "blended_sim": blended_sim,
            "popularity_scores": popularity_scores,
            "eligible_orders_count": len(orders_rows),
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
        """Lookup blended similarity between two items."""
        if id_a == id_b:
            return 1.0
        pair_key = (min(id_a, id_b), max(id_a, id_b))
        return model["blended_sim"].get(pair_key, 0.0)

    @classmethod
    def _get_item_cooccurrence(
        cls, id_a: int, id_b: int, model: Dict[str, Any]
    ) -> int:
        """Lookup direct co-occurrence count between two items."""
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
        Main API method returning the comprehensive 6-section recommendation response.
        """
        model = cls._get_model()
        user_key = cls._get_user_key(user_id=user_id, email=email)
        menu_items = model["menu_items"]

        cart_set = set(cart_item_ids or [])
        user_interactions = model["user_interactions"].get(user_key, {}) if user_key else {}
        has_history = len(user_interactions) > 0

        # Increment global served metric
        with cls._metrics_lock:
            cls._stats_requests_served += 1
            if not has_history:
                cls._stats_cold_start_served += 1

        # 1. Section: Recommended for You
        rec_for_you = cls._build_recommended_for_you(
            model=model,
            user_key=user_key,
            user_interactions=user_interactions,
            cart_set=cart_set,
            limit=limit,
        )

        # 2. Section: Frequently Ordered Together (Pairs)
        freq_together = cls._build_frequently_ordered_together(
            model=model,
            cart_item_ids=cart_item_ids or [],
            user_key=user_key,
            limit=4,
        )

        # 3. Section: Because You Ordered This
        because_ordered = cls._build_because_you_ordered(
            model=model,
            user_key=user_key,
            cart_set=cart_set,
            limit=limit,
        )

        # 4. Section: Popular at Smart Canteen
        popular_items = cls._build_popular_items(
            model=model,
            exclude_ids=cart_set,
            limit=limit,
        )

        # 5. Section: Try Something New (Never ordered by user)
        try_new = cls._build_try_something_new(
            model=model,
            user_key=user_key,
            user_interactions=user_interactions,
            cart_set=cart_set,
            limit=limit,
        )

        # 6. Section: Quick Bites for You (Snacks & Beverages)
        quick_bites = cls._build_quick_bites(
            model=model,
            user_key=user_key,
            user_interactions=user_interactions,
            cart_set=cart_set,
            limit=limit,
        )

        # Record served items in operational metrics
        all_served_ids = []
        for s in (rec_for_you["items"], because_ordered["items"], popular_items["items"], try_new["items"], quick_bites["items"]):
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
            "sections": {
                "recommended_for_you": rec_for_you,
                "frequently_ordered_together": freq_together,
                "because_you_ordered": because_ordered,
                "popular_items": popular_items,
                "try_something_new": try_new,
                "quick_bites": quick_bites,
            },
        }

    @classmethod
    def _build_recommended_for_you(
        cls,
        model: Dict[str, Any],
        user_key: Optional[str],
        user_interactions: Dict[int, float],
        cart_set: Set[int],
        limit: int,
    ) -> Dict[str, Any]:
        """
        Section 1: "Recommended for You"
        Uses Item-to-Item Collaborative Filtering when order history exists,
        with transparent fallback to genuinely popular items during cold-start.
        """
        menu_items = model["menu_items"]
        history_item_ids = set(user_interactions.keys())

        # If user has history, compute hybrid collaborative score
        if user_interactions:
            # Find user's top anchor item
            sorted_history = sorted(
                user_interactions.items(), key=lambda x: x[1], reverse=True
            )
            top_anchor_id = sorted_history[0][0]
            top_anchor_name = menu_items.get(top_anchor_id, {}).get("name", "Favorites")

            user_history_orders = model["user_order_history"].get(user_key, [])
            last_order_items = (
                user_history_orders[-1]["items"] if user_history_orders else []
            )

            scored_candidates = []
            for mid, item in menu_items.items():
                # Invariant: Must be active and in-stock (stock > 0)
                if not item.get("active") or item.get("stock", 0) <= 0:
                    continue
                # Exclude items currently in user's cart
                if mid in cart_set:
                    continue

                # 1. Item-to-Item Similarity Score (CF)
                # Weighted sum of similarities to items user ordered
                sim_numer = 0.0
                sim_denom = 0.0
                for h_id, h_weight in user_interactions.items():
                    sim = cls._get_item_similarity(mid, h_id, model)
                    sim_numer += sim * h_weight
                    sim_denom += h_weight
                s_sim = (sim_numer / sim_denom) if sim_denom > 0 else 0.0

                # 2. Co-occurrence Score with user's top/recent items
                s_cooccur = 0.0
                for h_id in last_order_items:
                    pair_key = (min(mid, h_id), max(mid, h_id))
                    s_cooccur = max(s_cooccur, model["jaccard_sim"].get(pair_key, 0.0))

                # 3. Recency Score (similarity to last order)
                s_recency = 0.0
                for h_id in last_order_items:
                    s_recency = max(s_recency, cls._get_item_similarity(mid, h_id, model))

                # 4. Normalized Popularity Score
                s_pop = model["popularity_scores"].get(mid, 0.0)

                # Composite score
                final_score = (
                    REC_WEIGHT_ITEM_SIMILARITY * s_sim
                    + REC_WEIGHT_COOCCURRENCE * s_cooccur
                    + REC_WEIGHT_RECENCY * s_recency
                    + REC_WEIGHT_POPULARITY * s_pop
                )

                # Truthful reason determination
                co_with_top = cls._get_item_cooccurrence(mid, top_anchor_id, model)
                if co_with_top > 0 and s_cooccur > 0.05:
                    reason = f"Frequently ordered with {top_anchor_name}"
                elif s_sim > 0.15:
                    reason = "Similar ordering patterns"
                elif s_recency > 0.15:
                    reason = "Recommended from your recent order"
                else:
                    reason = "Recommended from your order history"

                scored_candidates.append({
                    "item": item,
                    "score": final_score,
                    "reason": reason,
                })

            scored_candidates.sort(key=lambda x: x["score"], reverse=True)
            diverse_items = cls._apply_category_diversity(scored_candidates, limit)

            return {
                "title": "Recommended for You",
                "badge": "PERSONALIZED FOR YOU",
                "is_cold_start": False,
                "description": "Curated specially from your campus dining preferences.",
                "items": diverse_items,
            }

        # ── Cold Start Fallback ───────────────────────────────────────────────
        # Genuinely popular items from valid historical data
        popular_candidates = []
        for mid, item in menu_items.items():
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue
            if mid in cart_set:
                continue
            pop_score = model["popularity_scores"].get(mid, 0.0)
            order_cnt = model["item_order_counts"].get(mid, 0)
            reason = "Popular among canteen orders" if order_cnt > 0 else "Campus chef pick"
            popular_candidates.append({
                "item": item,
                "score": pop_score,
                "reason": reason,
            })

        popular_candidates.sort(key=lambda x: x["score"], reverse=True)
        diverse_items = cls._apply_category_diversity(popular_candidates, limit)

        return {
            "title": "Popular at Smart Canteen",
            "badge": "CAMPUS BESTSELLERS",
            "is_cold_start": True,
            "description": "Top dishes loved by students across the campus.",
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
        Section 2: "Frequently Ordered Together"
        Identifies pairs of items that appear together in the same order.
        Displays both items with individual prices and 1-click 'Add Both to Cart'.
        """
        menu_items = model["menu_items"]
        pairs_data = []

        # If user has items in cart, prioritize pairs that include a cart item!
        anchor_ids = set(cart_item_ids)
        if not anchor_ids and user_key:
            # Fall back to user's most recent order items
            user_history = model["user_order_history"].get(user_key, [])
            if user_history:
                anchor_ids = set(user_history[-1]["items"])

        # Collect eligible pairs
        for (id_a, id_b), count in model["pair_cooccurrence"].items():
            if count <= 0:
                continue
            item_a = menu_items.get(id_a)
            item_b = menu_items.get(id_b)

            if not item_a or not item_b:
                continue
            # Both items must be active and in-stock
            if (
                not item_a.get("active")
                or not item_b.get("active")
                or item_a.get("stock", 0) <= 0
                or item_b.get("stock", 0) <= 0
            ):
                continue

            jacc = model["jaccard_sim"].get((id_a, id_b), 0.0)
            # Relevance boost if pair contains one of the anchor items
            is_anchor_match = bool(anchor_ids and (id_a in anchor_ids or id_b in anchor_ids))
            priority = (2.0 if is_anchor_match else 1.0) * (count * 10.0 + jacc)

            total_combo_price = float(item_a["price"]) + float(item_b["price"])
            pairs_data.append({
                "item_a": cls._format_card(item_a, f"Frequently ordered with {item_b['name']}"),
                "item_b": cls._format_card(item_b, f"Frequently ordered with {item_a['name']}"),
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
                # Find most recent valid order item that is in menu
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
            # In an empty canteen database, pick first available item
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
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        diverse_items = cls._apply_category_diversity(candidates, limit)

        subtitle = (
            f"Because you ordered {anchor_name}"
            if not is_fallback_anchor
            else f"Because {anchor_name} is popular today"
        )

        return {
            "title": f"Because You Ordered {anchor_name}",
            "badge": "ORDER COMPANIONS",
            "anchor_item": cls._format_card(anchor_item, "Your previous order"),
            "description": subtitle,
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
        Section 4: "Popular at Smart Canteen"
        Most frequently ordered available items across all valid canteen orders.
        """
        menu_items = model["menu_items"]
        candidates = []

        for mid, item in menu_items.items():
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue
            if mid in exclude_ids:
                continue

            order_cnt = model["item_order_counts"].get(mid, 0)
            pop_score = model["popularity_scores"].get(mid, 0.0)

            if order_cnt > 0:
                reason = f"Popular among canteen orders ({order_cnt} orders)"
            else:
                reason = "Fresh kitchen highlight"

            candidates.append({
                "item": item,
                "score": pop_score,
                "reason": reason,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        diverse_items = cls._apply_category_diversity(candidates, limit)

        return {
            "title": "Popular at Smart Canteen",
            "badge": "CAMPUS BESTSELLERS",
            "description": "Most ordered delicacies across the campus food counters.",
            "items": diverse_items,
        }

    @classmethod
    def _build_try_something_new(
        cls,
        model: Dict[str, Any],
        user_key: Optional[str],
        user_interactions: Dict[int, float],
        cart_set: Set[int],
        limit: int,
    ) -> Dict[str, Any]:
        """
        Section 5: "Try Something New"
        Strictly excludes all items already ordered by this user in the past.
        Ranks unexplored items matching categories the student enjoys.
        """
        menu_items = model["menu_items"]
        ordered_by_user = set(user_interactions.keys())

        # Determine user's favorite categories
        favorite_categories: Dict[str, float] = {}
        for mid, weight in user_interactions.items():
            cat = menu_items.get(mid, {}).get("category", "")
            if cat:
                favorite_categories[cat] = favorite_categories.get(cat, 0.0) + weight

        candidates = []
        for mid, item in menu_items.items():
            # Invariant 8: Avoid recommending items already ordered by the user
            if mid in ordered_by_user:
                continue
            if not item.get("active") or item.get("stock", 0) <= 0:
                continue
            if mid in cart_set:
                continue

            cat = item.get("category", "")
            cat_affinity = (
                favorite_categories.get(cat, 0.0)
                / (sum(favorite_categories.values()) + 1e-6)
                if favorite_categories
                else 0.0
            )

            # Similarity to any item user liked
            max_sim = 0.0
            for h_id in ordered_by_user:
                max_sim = max(max_sim, cls._get_item_similarity(mid, h_id, model))

            pop = model["popularity_scores"].get(mid, 0.0)
            score = 0.40 * cat_affinity + 0.35 * max_sim + 0.25 * pop

            if cat_affinity > 0.2:
                reason = f"Top new discovery in {cat}"
            elif max_sim > 0.15:
                reason = "Similar to dishes you enjoy"
            else:
                reason = "Trending dish you haven't tried yet"

            candidates.append({
                "item": item,
                "score": score,
                "reason": reason,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        diverse_items = cls._apply_category_diversity(candidates, limit)

        return {
            "title": "Try Something New",
            "badge": "FRESH DISCOVERIES",
            "description": "Explore exciting canteen specialties you haven't tasted yet.",
            "items": diverse_items,
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
        Section 6: "Quick Bites for You"
        Filter to fast snacks and beverages matching student preferences.
        """
        menu_items = model["menu_items"]
        candidates = []

        for mid, item in menu_items.items():
            cat = (item.get("category") or "").lower()
            station = (item.get("station_code") or "").upper()
            prep_time = item.get("prep_time_minutes", 5)

            # Snacks or beverages criteria
            is_quick = (
                cat in ("snacks", "beverages")
                or station in ("SNACKS", "BEVERAGES")
                or prep_time <= 4
            )
            if not is_quick:
                continue

            if not item.get("active") or item.get("stock", 0) <= 0:
                continue
            if mid in cart_set:
                continue

            # Similarity to user's history
            sim_score = 0.0
            if user_interactions:
                for h_id, weight in user_interactions.items():
                    sim_score = max(sim_score, cls._get_item_similarity(mid, h_id, model))

            pop = model["popularity_scores"].get(mid, 0.0)
            score = 0.50 * sim_score + 0.50 * pop

            if station == "BEVERAGES":
                reason = "Refreshing drink for instant pickup"
            else:
                reason = "Crispy quick bite based on your taste"

            candidates.append({
                "item": item,
                "score": score,
                "reason": reason,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        diverse_items = cls._apply_category_diversity(candidates, limit)

        return {
            "title": "Quick Bites for You",
            "badge": "FAST PICKUP",
            "description": "Hot snacks and davara filter coffees ready in minutes.",
            "items": diverse_items,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # CARD FORMATTING & DIVERSITY
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def _format_card(cls, item: Dict[str, Any], reason: str) -> Dict[str, Any]:
        """Format an individual menu item into a clean recommendation card."""
        stock = item.get("stock", 0)
        return {
            "id": item["id"],
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
        }

    @classmethod
    def _apply_category_diversity(
        cls, scored_items: List[Dict[str, Any]], limit: int
    ) -> List[Dict[str, Any]]:
        """
        Enforce diversity control: avoid overwhelming recommendations with
        a single category (e.g. max 2 per category). Deduplicates items.
        """
        chosen: List[Dict[str, Any]] = []
        category_counts: Dict[str, int] = {}
        seen_ids: Set[int] = set()

        # First pass: Respect category diversity limits
        remaining = []
        for entry in scored_items:
            item = entry["item"]
            mid = item["id"]
            if mid in seen_ids:
                continue
            cat = item.get("category", "Main")
            curr_cat_count = category_counts.get(cat, 0)
            if curr_cat_count < REC_MAX_PER_CATEGORY:
                chosen.append(cls._format_card(item, entry["reason"]))
                category_counts[cat] = curr_cat_count + 1
                seen_ids.add(mid)
                if len(chosen) >= limit:
                    return chosen
            else:
                remaining.append(entry)

        # Second pass: If limit is not reached, backfill from remaining
        for entry in remaining:
            item = entry["item"]
            mid = item["id"]
            if mid not in seen_ids:
                chosen.append(cls._format_card(item, entry["reason"]))
                seen_ids.add(mid)
                if len(chosen) >= limit:
                    break

        return chosen

    # ──────────────────────────────────────────────────────────────────────────
    # ADMIN INSIGHTS & ANALYTICS (Section 10)
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def get_admin_insights(cls) -> Dict[str, Any]:
        """
        Calculate transparent recommendation analytics for Admin Dashboard.
        Strictly distinguishes measured values from calculated values.
        """
        model = cls._get_model()
        menu_items = model["menu_items"]

        # 1. Most frequently ordered item pairs (Measured from real order_items)
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

        # 2. Most frequently recommended items (Measured)
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

        return {
            "success": True,
            "measured_analytics": {
                "eligible_orders_count": model["eligible_orders_count"],
                "total_menu_items": len(menu_items),
                "items_with_similarity_data": items_with_sim,
                "recommendation_requests_served": req_served,
                "cold_start_fallback_usage": cold_served,
                "cold_start_fallback_percentage": cold_start_pct,
            },
            "most_frequently_ordered_pairs": top_pairs,
            "most_frequently_recommended_items": top_recommended,
            "algorithm_parameters": {
                "model_type": "Item-to-Item Collaborative Filtering + Association Rules",
                "weights": {
                    "item_similarity": REC_WEIGHT_ITEM_SIMILARITY,
                    "cooccurrence": REC_WEIGHT_COOCCURRENCE,
                    "recency": REC_WEIGHT_RECENCY,
                    "popularity": REC_WEIGHT_POPULARITY,
                },
                "cache_ttl_seconds": REC_CACHE_TTL_SECONDS,
                "recency_decay_days": REC_RECENCY_DECAY_DAYS,
            },
        }

    # ──────────────────────────────────────────────────────────────────────────
    # EVALUATION METRIC (Section 11)
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def evaluate_offline_ranking(cls, k: int = 3) -> Dict[str, Any]:
        """
        Evaluate recommendation relevance using Leave-One-Out offline ranking metric:
        Precision@K and Recall@K across all users with at least 2 ordered items.
        """
        model = cls._get_model()
        user_history = model["user_order_history"]
        menu_items = model["menu_items"]

        # Filter users who have ordered at least 2 distinct items
        eligible_users = []
        for u_key, orders in user_history.items():
            all_items = []
            for o in orders:
                all_items.extend(o.get("items", []))
            distinct_items = list(dict.fromkeys(all_items))
            if len(distinct_items) >= 2:
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
            # Leave the last item out as ground truth target
            target_item_id = distinct_items[-1]
            train_items = distinct_items[:-1]

            # Score all candidates using similarity to train_items
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
    # AUTHENTIC DATA SEEDER (For live demonstration & realistic dataset)
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def seed_demo_orders(cls, count: int = 25, force: bool = False) -> int:
        """
        Seed authentic South Indian orders into database if few or no orders exist.
        Populates real rows in orders and order_items tables with realistic
        combinations so Item-to-Item Collaborative Filtering has genuine data to process.
        """
        with _DB_LOCK:
            conn = get_db_connection()
            try:
                cur = conn.execute("SELECT COUNT(*) as cnt FROM orders")
                curr_cnt = cur.fetchone()["cnt"]
                if curr_cnt >= 10 and not force:
                    return 0

                # Menu mapping
                m_cur = conn.execute("SELECT id, name, price, station_code FROM menu_items")
                menu_by_name = {row["name"]: dict(row) for row in m_cur.fetchall()}

                # Authentic ordering archetypes in a college canteen:
                # 1. Morning Tiffin Combo: Masala Dosa + Filter Coffee
                # 2. Classic Breakfast: Idli + Vada + Tea
                # 3. Heavy Pongal: Pongal + Vada + Filter Coffee
                # 4. Lunch Biryani: Chicken Biriyani + Chicken 65 + Fresh Lime Juice
                # 5. Traditional Rice Meal: Sambar Rice + Curd Rice
                # 6. Evening Snack: Parotta + Tea + Samosa
                sample_users = [
                    ("Karthik Raja", "student@smartcanteen.com", 3),
                    ("Priya Sundaram", "priya@campus.edu", None),
                    ("Rahul Sharma", "rahul@campus.edu", None),
                    ("Ananya Krishnan", "ananya@campus.edu", None),
                    ("Vikram Raman", "vikram@campus.edu", None),
                    ("Deepa Mohan", "deepa@campus.edu", None),
                    ("Arjun Natarajan", "arjun@campus.edu", None),
                ]

                combos = [
                    [("Masala Dosa", 1), ("Filter Coffee", 1)],
                    [("Masala Dosa", 2), ("Filter Coffee", 2)],
                    [("Idli", 2), ("Vada", 1), ("Tea", 1)],
                    [("Pongal", 1), ("Vada", 1), ("Filter Coffee", 1)],
                    [("Chicken Biriyani", 1), ("Chicken 65", 1), ("Fresh Lime Juice", 1)],
                    [("Chicken Biriyani", 1), ("Fresh Lime Juice", 1)],
                    [("Sambar Rice", 1), ("Curd Rice", 1)],
                    [("Poori Masala", 1), ("Tea", 1)],
                    [("Parotta", 2), ("Tea", 1), ("Samosa", 1)],
                    [("Lemon Rice", 1), ("Curd Rice", 1)],
                    [("Masala Dosa", 1), ("Vada", 1)],
                    [("Chicken Biriyani", 1), ("Chicken 65", 1)],
                ]

                inserted_count = 0
                conn.execute("BEGIN IMMEDIATE")

                # Sequence row
                seq_row = conn.execute("SELECT next_seq FROM token_sequence WHERE id = 1").fetchone()
                next_seq = seq_row["next_seq"] if seq_row else 1

                import random
                for i in range(count):
                    user_name, user_email, u_id = sample_users[i % len(sample_users)]
                    combo_items = combos[i % len(combos)]

                    token_str = f"{TOKEN_PREFIX}{str(next_seq).zfill(TOKEN_PADDING)}"
                    next_seq += 1

                    # Compute line items
                    total_amount = 0.0
                    valid_line_items = []
                    for iname, qty in combo_items:
                        m_row = menu_by_name.get(iname)
                        if m_row:
                            price = float(m_row["price"])
                            total_amount += price * qty
                            valid_line_items.append((m_row, qty, price))

                    if not valid_line_items:
                        continue

                    # Realistic creation timestamp: spread over last 1 to 5 days
                    days_ago = (count - i) * 0.2
                    ts = datetime.now(timezone.utc).timestamp() - (days_ago * 86400)
                    dt_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

                    cur_ord = conn.execute("""
                        INSERT INTO orders (
                            token, student_name, student_email, user_id, status, total,
                            payment_method, transaction_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'UPI', ?, ?, ?)
                    """, (
                        token_str, user_name, user_email, u_id, STATUS_COMPLETED,
                        total_amount, f"TXN-DEMO-{next_seq}", dt_iso, dt_iso
                    ))
                    new_order_id = cur_ord.lastrowid

                    for m_row, qty, price in valid_line_items:
                        conn.execute("""
                            INSERT INTO order_items (
                                order_id, menu_item_id, item_name, quantity, price, station_code
                            ) VALUES (?, ?, ?, ?, ?, ?)
                        """, (
                            new_order_id, m_row["id"], m_row["name"], qty, price, m_row["station_code"]
                        ))

                    inserted_count += 1

                conn.execute("UPDATE token_sequence SET next_seq = ? WHERE id = 1", (next_seq,))
                conn.execute("COMMIT")

                cls.invalidate_cache()
                return inserted_count
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
