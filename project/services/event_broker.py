"""
services/event_broker.py
========================
Real-time event broadcasting broker using Server-Sent Events (SSE).
Allows connected web clients (Student UI, Live Board, Canteen Staff) to receive
instant live queue updates without page reloads.
"""

import json
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Any, List

# Reentrant lock for managing subscriber list
_BROKER_LOCK = threading.Lock()
_SUBSCRIBERS: List[queue.Queue] = []


class EventBroker:
    """Pub/Sub event broadcaster for real-time frontend updates."""

    @staticmethod
    def subscribe() -> queue.Queue:
        """Register a new client listener queue."""
        q = queue.Queue(maxsize=128)
        with _BROKER_LOCK:
            _SUBSCRIBERS.append(q)
        return q

    @staticmethod
    def unsubscribe(q: queue.Queue):
        """Remove a disconnected client queue."""
        with _BROKER_LOCK:
            if q in _SUBSCRIBERS:
                _SUBSCRIBERS.remove(q)

    @staticmethod
    def publish(event_type: str, data: Dict[str, Any]):
        """
        Broadcast an event to all connected clients.
        Example events:
          - order_created
          - order_cancelled
          - order_edited
          - task_advanced
          - queue_reordered
          - stock_updated
        """
        payload = {
            "event": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        msg = f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"

        with _BROKER_LOCK:
            dead_queues = []
            for q in _SUBSCRIBERS:
                try:
                    q.put_nowait(msg)
                except (queue.Full, Exception):
                    dead_queues.append(q)

            for dead_q in dead_queues:
                if dead_q in _SUBSCRIBERS:
                    _SUBSCRIBERS.remove(dead_q)


def format_sse(data: str, event: str = None) -> str:
    """Format string as standard SSE text stream block."""
    msg = ""
    if event:
        msg += f"event: {event}\n"
    msg += f"data: {data}\n\n"
    return msg
