"""
core/scheduler.py
=================
Deterministic station queue scheduler and dynamic ETA engine.
Implements the multi-station queue rules:
- Token number != permanent queue position
- Dynamic sorting by current estimated ready time
- Configurable edit preparation penalty (EDIT_EXTRA_MINUTES = 5)
- Deterministic tie-breaking by creation timestamp and stable task ID
"""

from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import (
    EDIT_EXTRA_MINUTES,
    STATUS_WAITING,
    STATUS_PREPARING,
    STATUS_READY,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
    STATUS_SERVED,
)


def parse_iso(iso_str: str) -> datetime:
    """Parse an ISO-8601 timestamp string into a timezone-aware datetime."""
    if not iso_str:
        return datetime.now(timezone.utc)
    try:
        # Handle trailing Z
        if iso_str.endswith("Z"):
            iso_str = iso_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)


def format_iso(dt: datetime) -> str:
    """Format datetime into ISO-8601 string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def sort_station_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort station tasks deterministically:
    1. Filter out CANCELLED and COMPLETED/SERVED tasks.
    2. Primary sort: estimated_ready_time ASC
    3. Secondary tie-breaker: created_at ASC
    4. Tertiary tie-breaker: id ASC

    Returns active tasks sorted by priority.
    """
    inactive_statuses = {STATUS_CANCELLED, STATUS_COMPLETED, STATUS_SERVED}
    active_tasks = [t for t in tasks if t.get("status") not in inactive_statuses]

    def sort_key(t: Dict[str, Any]):
        eta_str = t.get("estimated_ready_time") or ""
        created_str = t.get("created_at") or ""
        eta_dt = parse_iso(eta_str)
        created_dt = parse_iso(created_str)
        task_id = int(t.get("id") or 0)
        return (eta_dt, created_dt, task_id)

    return sorted(active_tasks, key=sort_key)


def compute_initial_task_eta(
    base_time: Optional[datetime],
    prep_time_minutes: int,
    existing_station_tasks: List[Dict[str, Any]],
) -> datetime:
    """
    Compute estimated ready time for a new task in a station.
    If the station currently has pending tasks, the new task is scheduled
    after the current latest pending ETA, or starting at base_time.
    """
    now = base_time or datetime.now(timezone.utc)
    duration = timedelta(minutes=max(1, prep_time_minutes))

    # Look at active tasks in the station
    active_tasks = [
        t for t in existing_station_tasks
        if t.get("status") in {STATUS_WAITING, STATUS_PREPARING}
    ]

    if not active_tasks:
        return now + duration

    # Find the maximum ETA among active tasks
    max_eta = now
    for t in active_tasks:
        t_eta = parse_iso(t.get("estimated_ready_time"))
        if t_eta > max_eta:
            max_eta = t_eta

    # Chain after max_eta
    return max_eta + duration


def apply_edit_penalty_to_eta(current_eta_str: str, extra_minutes: int = EDIT_EXTRA_MINUTES) -> str:
    """
    Add edit preparation penalty to an existing estimated ready time.
    new ETA = current ETA + EDIT_EXTRA_MINUTES
    """
    dt = parse_iso(current_eta_str)
    new_dt = dt + timedelta(minutes=extra_minutes)
    return format_iso(new_dt)
