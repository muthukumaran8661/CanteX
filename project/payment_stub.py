#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
payment_stub.py  —  LOCKED MODULE
ELEVIX - 1.0  ·  Fullstack PS F2  "Smart Canteen Ordering & Token System"

WHAT THIS IS
------------
A stand-in for a real payment gateway.  Your app never touches a real bank, a
real UPI app or a real card network — it calls this module instead.  It is
LOCKED: read it and call it, but do not edit it or reimplement it.

It deliberately behaves like a bad real gateway:  some payments time out, some
are declined, and a few are charged but never confirmed.  Your order-queue and
token generator must survive all of that.

WHAT IT GIVES YOU
-----------------
    charge(amount, method, ref)  -> dict   attempt a payment
    verify(txn_id)               -> dict   is this payment really settled?
    refund(txn_id, reason)       -> dict   give the money back
    balance()                    -> dict   total taken / refunded / net
    ledger()                     -> list   every transaction, for your audit view
    METHODS                      -> tuple  the methods you may offer

A CHARGE RESULT LOOKS LIKE
--------------------------
    {"ok": True,  "state": "CAPTURED",  "txn_id": "...", "amount": 60.0, ...}
    {"ok": False, "state": "DECLINED",  "reason": "INSUFFICIENT_FUNDS", ...}
    {"ok": False, "state": "TIMEOUT",   "reason": "GATEWAY_TIMEOUT", ...}

The three states your code MUST handle:
    CAPTURED   money moved. Issue the token.
    DECLINED   money did not move. Do NOT issue a token.
    TIMEOUT    unknown! Call verify(txn_id) before you do anything else.
               This is the trap: assuming TIMEOUT means "paid" or assuming it
               means "failed" will both lose money or anger a student.

USAGE
-----
    import payment_stub
    r = payment_stub.charge(60.0, "UPI", ref="ORDER-1042")
    if r["ok"]:
        issue_token(r["txn_id"])
    elif r["state"] == "TIMEOUT":
        r = payment_stub.verify(r["txn_id"])     # always reconcile

    python3 payment_stub.py --selftest

Pure Python standard library only.  Python 3.8+.
"""

import hashlib
import os
import random
import sys
import time

VERSION = "1.0.0"
METHODS = ("UPI", "CARD", "CASH", "WALLET")

# how often each outcome happens - tuned so a team MEETS every state
P_DECLINE = 0.15
P_TIMEOUT = 0.20
P_GHOST = 0.05          # charged at the gateway but never confirmed to you

_LEDGER = []
_BALANCE = {"captured": 0.0, "refunded": 0.0}
_SEQ = [0]


def _txn_id(ref, amount):
    _SEQ[0] += 1
    raw = "%s|%s|%d|%d" % (ref, amount, _SEQ[0], time.time_ns())
    return "TXN-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def charge(amount, method="UPI", ref=""):
    """Attempt a payment. Returns a dict; never raises for a business failure."""
    amount = round(float(amount), 2)
    if amount <= 0:
        return {"ok": False, "state": "DECLINED", "reason": "INVALID_AMOUNT",
                "amount": amount, "method": method, "ref": ref, "at": _now()}
    if method not in METHODS:
        return {"ok": False, "state": "DECLINED", "reason": "UNKNOWN_METHOD",
                "amount": amount, "method": method, "ref": ref, "at": _now()}

    txn = _txn_id(ref, amount)
    roll = random.random()

    if roll < P_TIMEOUT:
        # the gateway hangs.  money may or may not have moved.
        row = {"ok": False, "state": "TIMEOUT", "reason": "GATEWAY_TIMEOUT",
               "txn_id": txn, "amount": amount, "method": method, "ref": ref,
               "at": _now(), "settled": None}
        _LEDGER.append(row)
        return dict(row)

    if roll < P_TIMEOUT + P_DECLINE:
        row = {"ok": False, "state": "DECLINED",
               "reason": random.choice(["INSUFFICIENT_FUNDS", "CARD_EXPIRED",
                                        "BANK_REFUSED"]),
               "txn_id": txn, "amount": amount, "method": method, "ref": ref,
               "at": _now(), "settled": False}
        _LEDGER.append(row)
        return dict(row)

    ghost = roll < P_TIMEOUT + P_DECLINE + P_GHOST
    row = {"ok": True, "state": "CAPTURED" if not ghost else "CAPTURED_UNCONFIRMED",
           "txn_id": txn, "amount": amount, "method": method, "ref": ref,
           "at": _now(), "settled": True, "confirmed": not ghost}
    _BALANCE["captured"] += amount
    _LEDGER.append(row)
    return dict(row)


def verify(txn_id):
    """Reconcile a TIMEOUT or an unconfirmed charge.  Call this, always."""
    for row in _LEDGER:
        if row.get("txn_id") == txn_id:
            if row.get("state") == "TIMEOUT":
                # the gateway finally answers
                row["state"] = "CAPTURED"
                row["settled"] = True
                row["confirmed"] = True
                row["reason"] = "RECONCILED_AFTER_TIMEOUT"
                row["verified_at"] = _now()
                _BALANCE["captured"] += row["amount"]
                return dict(row)
            if row.get("state") == "CAPTURED_UNCONFIRMED":
                row["state"] = "CAPTURED"
                row["confirmed"] = True
                row["verified_at"] = _now()
                return dict(row)
            return dict(row)
    return {"ok": False, "state": "UNKNOWN_TXN", "txn_id": txn_id,
            "reason": "NO_SUCH_TRANSACTION"}


def refund(txn_id, reason=""):
    row = verify(txn_id)
    if not row.get("settled"):
        return {"ok": False, "state": "NOTHING_TO_REFUND", "txn_id": txn_id,
                "reason": "payment was never captured"}
    amt = row["amount"]
    _BALANCE["refunded"] += amt
    out = {"ok": True, "state": "REFUNDED", "txn_id": txn_id, "amount": amt,
           "reason": reason or "CUSTOMER_REQUEST", "at": _now()}
    _LEDGER.append(out)
    return out


def balance():
    net = round(_BALANCE["captured"] - _BALANCE["refunded"], 2)
    return {"captured": round(_BALANCE["captured"], 2),
            "refunded": round(_BALANCE["refunded"], 2), "net": net}


def ledger():
    return [dict(r) for r in _LEDGER]


def verify_integrity():
    try:
        with open(__file__, "rb") as f:
            body = f.read()
    except Exception:
        return False, "cannot read own source"
    lines = [ln for ln in body.split(b"\n") if not ln.startswith(b"EXPECTED_HASH")]
    digest = hashlib.sha256(b"\n".join(lines)).hexdigest()
    return (digest == EXPECTED_HASH), digest


EXPECTED_HASH = "c485cf39ff4f339def3d83b680990b2222a304083f775c7e78ea6358446138b0"


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        seen = {}
        for i in range(60):
            r = charge(60.0, "UPI", ref="SELFTEST-%d" % i)
            seen[r["state"]] = seen.get(r["state"], 0) + 1
            if r["state"] == "TIMEOUT":
                v = verify(r["txn_id"])
                assert v["state"] == "CAPTURED", "reconcile failed"
        print("outcomes over 60 charges :", seen)
        for s in ("CAPTURED", "DECLINED", "TIMEOUT"):
            assert s in seen, "selftest never produced %s - retune the odds" % s
        print("balance                  :", balance())
        print("SELFTEST PASS — every payment state is reachable and reconcilable.")
        print("integrity                :", verify_integrity()[0])
    else:
        print(__doc__)
