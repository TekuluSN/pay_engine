"""Razorpay API simulator.

`/v1/*` mimics the Razorpay REST API (Basic auth with key_id:key_secret).
`/checkout` and `/simulator/*` stand in for the customer-facing Razorpay Checkout and let tests
drive payment outcomes. Dev/test tool only: `/simulator/*` is intentionally unauthenticated.
"""

from __future__ import annotations

import html
import json
import secrets
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from app.config import settings
from app.engine import Engine, SimError
from app.signing import checkout_signature
from app.webhooks import WebhookDispatcher

engine = Engine(settings)
dispatcher = WebhookDispatcher(settings)

app = FastAPI(title="Razorpay Simulator", version="1.0.0")
_basic = HTTPBasic(auto_error=False)


@app.exception_handler(SimError)
async def _sim_error(_: Request, exc: SimError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body())


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0]
    field = str(first["loc"][-1]) if first["loc"] else None
    return JSONResponse(
        status_code=400,
        content=SimError(400, f"{field}: {first['msg']}", field).body(),
    )


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    ok = credentials is not None and (
        secrets.compare_digest(credentials.username.encode(), settings.key_id.encode())
        & secrets.compare_digest(credentials.password.encode(), settings.key_secret.encode())
    )
    if not ok:
        raise SimError(401, "The api key provided is invalid", code="BAD_REQUEST_ERROR")


def _collection(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"entity": "collection", "count": len(items), "items": items}


# -- request models -------------------------------------------------------------------------


class OrderCreate(BaseModel):
    amount: int
    currency: str = "INR"
    receipt: str | None = None
    notes: dict[str, str] | None = None


class CaptureRequest(BaseModel):
    amount: int
    currency: str = "INR"


class RefundRequest(BaseModel):
    amount: int | None = None
    notes: dict[str, str] | None = None


class PayRequest(BaseModel):
    outcome: Literal["success", "failure"] = "success"
    method: Literal["card", "upi", "netbanking", "wallet"] = "card"
    email: str | None = None
    contact: str | None = None
    amount: int | None = None
    # Deliver each webhook this many times (same event id) to exercise consumer idempotency.
    webhook_duplicates: int = 1


# -- Razorpay REST API ------------------------------------------------------------------------

api = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])


@api.post("/orders")
def create_order(body: OrderCreate) -> dict[str, Any]:
    return engine.create_order(body.amount, body.currency, body.receipt, body.notes)


@api.get("/orders")
def list_orders(count: int = Query(10, ge=1, le=100), skip: int = Query(0, ge=0)) -> dict[str, Any]:
    return _collection(engine.list_orders(count, skip))


@api.get("/orders/{order_id}")
def get_order(order_id: str) -> dict[str, Any]:
    return engine.get_order(order_id)


@api.get("/orders/{order_id}/payments")
def order_payments(order_id: str) -> dict[str, Any]:
    return _collection(engine.order_payments(order_id))


@api.get("/payments")
def list_payments(count: int = Query(10, ge=1, le=100), skip: int = Query(0, ge=0)) -> dict[str, Any]:
    return _collection(engine.list_payments(count, skip))


@api.get("/payments/{payment_id}")
def get_payment(payment_id: str) -> dict[str, Any]:
    return engine.get_payment(payment_id)


@api.post("/payments/{payment_id}/capture")
def capture_payment(payment_id: str, body: CaptureRequest, background: BackgroundTasks) -> dict[str, Any]:
    payment, events = engine.capture(payment_id, body.amount, body.currency)
    background.add_task(dispatcher.dispatch, events)
    return payment


@api.post("/payments/{payment_id}/refund")
def refund_payment(payment_id: str, body: RefundRequest, background: BackgroundTasks) -> dict[str, Any]:
    refund, events = engine.refund(payment_id, body.amount, body.notes)
    background.add_task(dispatcher.dispatch, events)
    return refund


@api.get("/payments/{payment_id}/refunds")
def payment_refunds(payment_id: str) -> dict[str, Any]:
    return _collection(engine.payment_refunds(payment_id))


@api.get("/refunds/{refund_id}")
def get_refund(refund_id: str) -> dict[str, Any]:
    return engine.get_refund(refund_id)


app.include_router(api)

# -- simulator control ------------------------------------------------------------------------

sim = APIRouter(prefix="/simulator")


@sim.post("/orders/{order_id}/pay")
def pay_order(order_id: str, body: PayRequest, background: BackgroundTasks) -> dict[str, Any]:
    """Act as the customer at Razorpay Checkout. On success, returns the three fields Checkout hands back."""
    payment, events = engine.attempt_payment(
        order_id, body.outcome, body.method, body.email, body.contact, body.amount
    )
    background.add_task(dispatcher.dispatch, events, body.webhook_duplicates)
    result: dict[str, Any] = {"payment": payment}
    if payment["status"] != "failed":
        result.update(
            razorpay_payment_id=payment["id"],
            razorpay_order_id=order_id,
            razorpay_signature=checkout_signature(order_id, payment["id"], settings.key_secret),
        )
    return result


@sim.get("/webhooks")
def webhook_log(event: str | None = None) -> dict[str, Any]:
    items = [r for r in dispatcher.log if event is None or r["event"] == event]
    return _collection(items)


@sim.post("/reset")
def reset() -> dict[str, str]:
    engine.reset()
    dispatcher.reset()
    return {"status": "reset"}


app.include_router(sim)


# -- hosted checkout page ---------------------------------------------------------------------

_CHECKOUT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Razorpay Simulator Checkout</title>
<style>
body{font-family:system-ui,sans-serif;max-width:420px;margin:48px auto;padding:0 16px;color:#1a1a1a}
.card{border:1px solid #d0d5dd;border-radius:10px;padding:20px}
button{width:100%;padding:12px;margin-top:10px;border:0;border-radius:8px;font-size:16px;cursor:pointer;color:#fff}
.ok{background:#12805c}.bad{background:#b42318}
select{width:100%;padding:8px;margin-top:6px}
pre{background:#f4f4f5;padding:10px;border-radius:6px;overflow:auto;font-size:12px}
.banner{background:#fef0c7;padding:8px 12px;border-radius:6px;font-size:13px;margin-bottom:14px}
</style></head><body>
<div class="banner">Simulator &mdash; no real money moves.</div>
<div class="card">
<h2>Pay __AMOUNT__ __CURRENCY__</h2>
<p>Order <code>__ORDER_ID__</code></p>
<label>Method<select id="method"><option>card</option><option>upi</option><option>netbanking</option><option>wallet</option></select></label>
<button class="ok" onclick="pay('success')">Pay (success)</button>
<button class="bad" onclick="pay('failure')">Pay (fail)</button>
<pre id="out" hidden></pre>
</div>
<script>
const ORDER_ID = __ORDER_ID_JSON__;
const CALLBACK_URL = __CALLBACK_JSON__;
const OPENER_ORIGIN = __OPENER_JSON__;
function pick(d) {
  return {razorpay_payment_id: d.razorpay_payment_id, razorpay_order_id: d.razorpay_order_id, razorpay_signature: d.razorpay_signature};
}
async function pay(outcome) {
  const res = await fetch('/simulator/orders/' + encodeURIComponent(ORDER_ID) + '/pay', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({outcome, method: document.getElementById('method').value})
  });
  const data = await res.json();
  const out = document.getElementById('out');
  out.hidden = false; out.textContent = JSON.stringify(data, null, 2);
  // Popup mode (like Razorpay Checkout's handler callback): hand the result to the opening page.
  if (res.ok && data.razorpay_signature && window.opener && OPENER_ORIGIN) {
    window.opener.postMessage({type: 'razorpay_simulator_payment', ...pick(data)}, OPENER_ORIGIN);
    window.close();
    return;
  }
  if (res.ok && !data.razorpay_signature && window.opener && OPENER_ORIGIN) {
    window.opener.postMessage({type: 'razorpay_simulator_failed', error: data.payment && data.payment.error_description}, OPENER_ORIGIN);
  }
  if (res.ok && data.razorpay_signature && CALLBACK_URL) {
    const form = document.createElement('form');
    form.method = 'POST'; form.action = CALLBACK_URL;
    for (const k of ['razorpay_payment_id', 'razorpay_order_id', 'razorpay_signature']) {
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = k; input.value = data[k]; form.appendChild(input);
    }
    document.body.appendChild(form); form.submit();
  }
}
</script></body></html>"""


@app.get("/checkout", response_class=HTMLResponse)
def checkout(order_id: str, callback_url: str | None = None, opener_origin: str | None = None) -> HTMLResponse:
    order = engine.orders.get(order_id)
    if order is None:
        return HTMLResponse("<h3>Order not found</h3>", status_code=404)
    page = (
        _CHECKOUT.replace("__AMOUNT__", f"{order['amount_due'] / 100:.2f}")
        .replace("__CURRENCY__", html.escape(order["currency"]))
        .replace("__ORDER_ID__", html.escape(order_id))
        .replace("__ORDER_ID_JSON__", json.dumps(order_id).replace("<", "\\u003c"))
        .replace("__CALLBACK_JSON__", json.dumps(callback_url).replace("<", "\\u003c"))
        .replace("__OPENER_JSON__", json.dumps(opener_origin).replace("<", "\\u003c"))
    )
    return HTMLResponse(page)


# -- health (same shape as the ERP backend) ---------------------------------------------------


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> dict[str, str]:
    return {"status": "ok"}
