"""
core/order_queue.py
===================
FIFO queue implemented as a singly-linked list for canteen orders.
Pure Python standard library only.
"""

from typing import Optional, List, Dict, Any


class _Node:
    """A single node in the singly-linked list."""

    def __init__(self, order: Dict[str, Any]):
        self.order = order
        self.next: Optional["_Node"] = None


class OrderQueue:
    """
    Thread-safe FIFO queue implemented as a singly-linked list.

    Invariants:
      - self._head -> front of queue (oldest order, dequeued first)
      - self._tail -> back of queue (newest order, enqueued here)
      - self._size -> current count of orders in the queue
    """

    def __init__(self):
        self._head: Optional[_Node] = None
        self._tail: Optional[_Node] = None
        self._size: int = 0

    def enqueue(self, order: Dict[str, Any]) -> None:
        """
        Append `order` to the back of the queue.

        Raises:
            ValueError: If `order` is None, not a dict, or missing "token".
        """
        if not isinstance(order, dict) or "token" not in order:
            raise ValueError("Order must be a dict containing a 'token' key.")

        node = _Node(order)
        if self._head is None:
            self._head = node
            self._tail = node
        else:
            assert self._tail is not None
            self._tail.next = node
            self._tail = node
        self._size += 1

    def dequeue(self) -> Optional[Dict[str, Any]]:
        """
        Remove and return the order at the front of the queue.
        Returns None if queue is empty.
        """
        if self._head is None:
            return None

        node = self._head
        self._head = node.next
        if self._head is None:
            self._tail = None
        self._size -= 1
        node.next = None
        return node.order

    def peek(self) -> Optional[Dict[str, Any]]:
        """Return the order at the front of the queue without removing it."""
        return self._head.order if self._head else None

    def remove(self, token: str) -> bool:
        """
        Remove the first order matching `token` from anywhere in the queue.
        Returns True if found and removed, False otherwise.
        """
        if not token or self._head is None:
            return False

        # If matching node is at the head
        if self._head.order.get("token") == token:
            self.dequeue()
            return True

        curr = self._head
        while curr.next:
            if curr.next.order.get("token") == token:
                removed_node = curr.next
                curr.next = removed_node.next
                if removed_node is self._tail:
                    self._tail = curr
                self._size -= 1
                removed_node.next = None
                return True
            curr = curr.next

        return False

    def position_of(self, token: str) -> int:
        """
        Return the 1-based position of the order with `token` in the queue.
        Returns -1 if not found.
        """
        curr = self._head
        pos = 1
        while curr:
            if curr.order.get("token") == token:
                return pos
            curr = curr.next
            pos += 1
        return -1

    def to_list(self) -> List[Dict[str, Any]]:
        """Return all orders currently in the queue as a Python list in FIFO order."""
        result = []
        curr = self._head
        while curr:
            result.append(curr.order)
            curr = curr.next
        return result

    def size(self) -> int:
        """Return the current number of orders in the queue."""
        return self._size

    def __len__(self) -> int:
        return self._size

    def is_empty(self) -> bool:
        """Return True if queue is empty, False otherwise."""
        return self._size == 0

    def clear(self) -> None:
        """Clear the queue."""
        self._head = None
        self._tail = None
        self._size = 0
