"""
services/payment_adapter.py
===========================
Clean adapter service integrating with the locked organizer-provided payment_stub.py.
Handles simulated gateway states (CAPTURED, TIMEOUT, DECLINED, CAPTURED_UNCONFIRMED)
with mandatory reconciliation and resilient gateway retry.
"""

from typing import Dict, Any
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import payment_stub  # LOCKED module – imported without any modification


class PaymentAdapter:
    """
    Adapter between Canteen business logic and the locked payment simulator.
    """

    @staticmethod
    def charge(amount: float, method: str = "UPI", ref: str = "", max_retries: int = 8) -> Dict[str, Any]:
        """
        Attempt a payment charge with automatic timeout reconciliation and resilient retry.
        Handles TIMEOUT, UNCONFIRMED, and simulated transient DECLINED responses.
        """
        # Normalize method to supported uppercase method
        clean_method = (method or "UPI").upper()
        if "CARD" in clean_method:
            clean_method = "CARD"
        elif "CASH" in clean_method:
            clean_method = "CASH"
        elif "WALLET" in clean_method:
            clean_method = "WALLET"
        else:
            clean_method = "UPI"

        last_result = {}

        for attempt in range(max(1, max_retries)):
            attempt_ref = f"{ref}-att{attempt}" if ref else f"TXN-ATT{attempt}"
            result = payment_stub.charge(amount, method=clean_method, ref=attempt_ref)
            last_result = result

            txn_id = result.get("txn_id")

            # 1. Handle Gateway Timeout Trap -> Always call verify(txn_id)
            if result.get("state") == "TIMEOUT" or not result.get("ok"):
                if txn_id:
                    reconciled = payment_stub.verify(txn_id)
                    if reconciled.get("ok") and reconciled.get("state") == "CAPTURED":
                        return {
                            "success": True,
                            "transaction_id": txn_id,
                            "amount": reconciled.get("amount", amount),
                            "state": "CAPTURED",
                            "method": clean_method,
                            "message": "Payment reconciled and captured after timeout.",
                        }

            # 2. Handle Ghost / Unconfirmed Charge Trap -> Call verify(txn_id)
            if result.get("state") == "CAPTURED_UNCONFIRMED":
                if txn_id:
                    reconciled = payment_stub.verify(txn_id)
                    if reconciled.get("ok") and reconciled.get("state") == "CAPTURED":
                        return {
                            "success": True,
                            "transaction_id": txn_id,
                            "amount": reconciled.get("amount", amount),
                            "state": "CAPTURED",
                            "method": clean_method,
                            "message": "Payment confirmed and captured successfully.",
                        }

            # 3. Direct Captured Success
            if result.get("ok") and result.get("state") == "CAPTURED":
                return {
                    "success": True,
                    "transaction_id": result["txn_id"],
                    "amount": result.get("amount", amount),
                    "state": "CAPTURED",
                    "method": clean_method,
                    "message": "Payment approved and settled.",
                }

            # If DECLINED and attempts remain, try alternate attempt
            if result.get("state") == "DECLINED" and attempt < max_retries - 1:
                continue

        # Terminal decline
        return {
            "success": False,
            "transaction_id": last_result.get("txn_id", ""),
            "state": last_result.get("state", "DECLINED"),
            "reason": last_result.get("reason", "Payment declined by provider."),
            "message": f"Payment declined: {last_result.get('reason', 'Payment could not be processed')}",
        }

    @staticmethod
    def process_payment(amount: float, method: str = "UPI", ref: str = "") -> Dict[str, Any]:
        """Backward-compatible alias for charge."""
        return PaymentAdapter.charge(amount, method, ref)

    @staticmethod
    def refund(txn_id: str, reason: str = "CUSTOMER_CANCELLATION") -> Dict[str, Any]:
        """Issue a refund through the locked payment stub."""
        if not txn_id:
            return {"success": False, "message": "No transaction ID provided for refund."}
        res = payment_stub.refund(txn_id, reason=reason)
        return {
            "success": res.get("ok", False),
            "transaction_id": txn_id,
            "refund_data": res,
            "message": "Refund processed." if res.get("ok") else res.get("reason", "Refund failed."),
        }
