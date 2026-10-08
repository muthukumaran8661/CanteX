"""
core/token_gen.py
=================
Thread-safe, deterministic token generator for canteen orders.
Pure Python standard library only.
"""

import threading
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import TOKEN_PREFIX, TOKEN_PADDING


class TokenGenerator:
    """
    Generates sequential, zero-padded, unique tokens.
    Format: <TOKEN_PREFIX><zero-padded counter>
    Example: "T-001", "T-002", ..., "T-999", "T-1000"

    Thread-safe: uses threading.Lock to serialize counter increments across
    concurrent checkout requests.
    """

    def __init__(self, prefix: str = None, padding: int = None, start: int = 1):
        self._prefix = prefix if prefix is not None else TOKEN_PREFIX
        self._padding = padding if padding is not None else TOKEN_PADDING
        self._counter = start
        self._lock = threading.Lock()

    def next_token(self) -> str:
        """
        Generate and return the next unique token atomically.
        Thread-safe: two simultaneous requests will NEVER receive the same token.
        """
        with self._lock:
            current = self._counter
            self._counter += 1
            zfilled = str(current).zfill(self._padding)
            return f"{self._prefix}{zfilled}"

    def current_count(self) -> int:
        """Return how many tokens have been issued so far."""
        with self._lock:
            return self._counter - 1

    def peek_next(self) -> str:
        """Inspect the next token that will be generated without incrementing."""
        with self._lock:
            return f"{self._prefix}{str(self._counter).zfill(self._padding)}"

    def daily_reset(self) -> None:
        """Reset the counter back to 1 (thread-safe)."""
        with self._lock:
            self._counter = 1

    def set_counter(self, val: int) -> None:
        """Set the internal counter to val (thread-safe)."""
        with self._lock:
            self._counter = max(1, int(val))
