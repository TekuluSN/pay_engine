"""In-memory Razorpay state machine: orders, payments, refunds and the events they emit.

Amounts are integers in the smallest currency unit (paise), as in the real API.
"""

from __future__ import annotations

import copy
import secrets
import string
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.config import Settings

_ALNUM = string.ascii_letters + string.digits
GST_ON_FEE = 0.18


def new_id(prefix: str) -> str:
    return prefix + "".join(secrets.choice(_ALNUM) for _ in range(14))


class SimError(Exception):
    """Rendered in Razorpay's error envelope."""

    def __init__(self, status: int, description: str, field: str | None = None, code: str = "BAD_REQUEST_ERROR"):
        super().__init__(description)
        self.status = status
        self.code = code
        self.description = description
        self.field = field

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "description": self.description,
                "source": "business",
                "step": "payment_initiation",
                "reason": "input_validation_failed",
                "metadata": {},
                "field": self.field,
            }
        }


@dataclass
class Event:
    name: str
    contains: list[str]
    entities: dict[str, dict[str, Any]]


def _now() -> int:
    return int(time.time())


class Engine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.orders: dict[str, dict[str, Any]] = {}
            self.payments: dict[str, dict[str, Any]] = {}
            self.refunds: dict[str, dict[str, Any]] = {}

    # -- orders ---------------------------------------------------------------

    def create_order(self, amount: int, currency: str, receipt: str | None, notes: dict[str, str] | None) -> dict[str, Any]:
        if amount < 100:
            raise SimError(400, "Order amount less than minimum amount allowed", "amount")
        if len(currency) != 3 or not currency.isalpha():
            raise SimError(400, "Not a valid currency", "currency")
        if receipt is not None and len(receipt) > 40:
            raise SimError(400, "receipt: the length must be no more than 40.", "receipt")
        if notes and len(notes) > 15:
            raise SimError(400, "notes: max 15 key-value pairs are allowed.", "notes")
        with self._lock:
            order = {
                "id": new_id("order_"),
                "entity": "order",
                "amount": amount,
                "amount_paid": 0,
                "amount_due": amount,
                "currency": currency.upper(),
                "receipt": receipt,
                "offer_id": None,
                "status": "created",
                "attempts": 0,
                "notes": notes or [],
                "created_at": _now(),
            }
            self.orders[order["id"]] = order
            return copy.deepcopy(order)

    def get_order(self, order_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._order(order_id))

    def list_orders(self, count: int, skip: int) -> list[dict[str, Any]]:
        with self._lock:
            ordered = sorted(self.orders.values(), key=lambda o: o["created_at"], reverse=True)
            return copy.deepcopy(ordered[skip : skip + count])

    def order_payments(self, order_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._order(order_id)
            items = [p for p in self.payments.values() if p["order_id"] == order_id]
            return copy.deepcopy(sorted(items, key=lambda p: p["created_at"]))

    # -- payments -------------------------------------------------------------

    def attempt_payment(
        self,
        order_id: str,
        outcome: str,
        method: str,
        email: str | None,
        contact: str | None,
        amount: int | None,
    ) -> tuple[dict[str, Any], list[Event]]:
        """Simulate a customer paying an order at checkout."""
        with self._lock:
            order = self._order(order_id)
            if order["status"] == "paid":
                raise SimError(400, "This order is already paid.", "order_id")
            if amount is not None and amount != order["amount_due"]:
                raise SimError(400, "Payment amount does not match the order's amount due.", "amount")

            order["attempts"] += 1
            order["status"] = "attempted"
            payment = {
                "id": new_id("pay_"),
                "entity": "payment",
                "amount": order["amount_due"],
                "currency": order["currency"],
                "status": "created",
                "order_id": order_id,
                "invoice_id": None,
                "international": False,
                "method": method,
                "amount_refunded": 0,
                "refund_status": None,
                "captured": False,
                "description": None,
                "card_id": new_id("card_") if method == "card" else None,
                "bank": "HDFC" if method == "netbanking" else None,
                "wallet": "simulatedpay" if method == "wallet" else None,
                "vpa": "simulator@upi" if method == "upi" else None,
                "email": email,
                "contact": contact,
                "notes": order["notes"],
                "fee": None,
                "tax": None,
                "error_code": None,
                "error_description": None,
                "error_source": None,
                "error_step": None,
                "error_reason": None,
                "created_at": _now(),
            }
            self.payments[payment["id"]] = payment

            events: list[Event] = []
            if outcome == "failure":
                payment.update(
                    status="failed",
                    error_code="BAD_REQUEST_ERROR",
                    error_description="Payment failed because the bank declined the transaction",
                    error_source="bank",
                    error_step="payment_authorization",
                    error_reason="payment_failed",
                )
                events.append(self._event("payment.failed", payment=payment))
            else:
                payment["status"] = "authorized"
                events.append(self._event("payment.authorized", payment=payment))
                if self.settings.auto_capture:
                    events.extend(self._capture(payment))
            return copy.deepcopy(payment), events

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._payment(payment_id))

    def list_payments(self, count: int, skip: int) -> list[dict[str, Any]]:
        with self._lock:
            ordered = sorted(self.payments.values(), key=lambda p: p["created_at"], reverse=True)
            return copy.deepcopy(ordered[skip : skip + count])

    def capture(self, payment_id: str, amount: int, currency: str) -> tuple[dict[str, Any], list[Event]]:
        with self._lock:
            payment = self._payment(payment_id)
            if payment["status"] == "captured":
                raise SimError(400, "This payment has already been captured", "payment_id")
            if payment["status"] != "authorized":
                raise SimError(400, "This payment is not in an authorized state and cannot be captured", "payment_id")
            if amount != payment["amount"]:
                raise SimError(400, "Capture amount must be equal to the amount authorized", "amount")
            if currency.upper() != payment["currency"]:
                raise SimError(400, "Capture currency must match the payment currency", "currency")
            events = self._capture(payment)
            return copy.deepcopy(payment), events

    # -- refunds --------------------------------------------------------------

    def refund(self, payment_id: str, amount: int | None, notes: dict[str, str] | None) -> tuple[dict[str, Any], list[Event]]:
        with self._lock:
            payment = self._payment(payment_id)
            if payment["status"] not in ("captured", "refunded"):
                raise SimError(400, "The payment has not been captured yet", "payment_id")
            remaining = payment["amount"] - payment["amount_refunded"]
            if remaining <= 0:
                raise SimError(400, "The payment has been fully refunded already", "payment_id")
            refund_amount = remaining if amount is None else amount
            if refund_amount <= 0:
                raise SimError(400, "The refund amount should be greater than zero", "amount")
            if refund_amount > remaining:
                raise SimError(400, "The refund amount provided is greater than amount captured", "amount")

            refund = {
                "id": new_id("rfnd_"),
                "entity": "refund",
                "amount": refund_amount,
                "currency": payment["currency"],
                "payment_id": payment_id,
                "notes": notes or [],
                "receipt": None,
                "acquirer_data": {"arn": None},
                "created_at": _now(),
                "batch_id": None,
                "status": "processed",
                "speed_processed": "normal",
                "speed_requested": "normal",
            }
            self.refunds[refund["id"]] = refund
            payment["amount_refunded"] += refund_amount
            if payment["amount_refunded"] == payment["amount"]:
                payment["refund_status"] = "full"
                payment["status"] = "refunded"
            else:
                payment["refund_status"] = "partial"
            return copy.deepcopy(refund), [self._event("refund.processed", refund=refund, payment=payment)]

    def get_refund(self, refund_id: str) -> dict[str, Any]:
        with self._lock:
            refund = self.refunds.get(refund_id)
            if refund is None:
                raise SimError(400, "The id provided does not exist", "id")
            return copy.deepcopy(refund)

    def payment_refunds(self, payment_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._payment(payment_id)
            return copy.deepcopy([r for r in self.refunds.values() if r["payment_id"] == payment_id])

    # -- internals ------------------------------------------------------------

    def _order(self, order_id: str) -> dict[str, Any]:
        order = self.orders.get(order_id)
        if order is None:
            raise SimError(400, "The id provided does not exist", "id")
        return order

    def _payment(self, payment_id: str) -> dict[str, Any]:
        payment = self.payments.get(payment_id)
        if payment is None:
            raise SimError(400, "The id provided does not exist", "id")
        return payment

    def _capture(self, payment: dict[str, Any]) -> list[Event]:
        fee = round(payment["amount"] * self.settings.fee_percent / 100)
        payment.update(status="captured", captured=True, fee=fee, tax=round(fee * GST_ON_FEE))
        events = [self._event("payment.captured", payment=payment)]

        order = self.orders[payment["order_id"]]
        order["amount_paid"] += payment["amount"]
        order["amount_due"] = order["amount"] - order["amount_paid"]
        if order["amount_due"] == 0:
            order["status"] = "paid"
            events.append(self._event("order.paid", payment=payment, order=order))
        return events

    @staticmethod
    def _event(name: str, **entities: dict[str, Any]) -> Event:
        return Event(name=name, contains=list(entities), entities=copy.deepcopy(entities))
