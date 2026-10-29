# Razorpay Simulator

Standalone FastAPI service that imitates the Razorpay API so the ERP's fee-payment flow can be built and tested without a Razorpay account or real money. It is a dev/test tool: state is in memory (lost on restart) and `/simulator/*` is unauthenticated. Never deploy it where real payments are expected.

## Run

```powershell
pip install -r requirements.txt
uvicorn app.main:app --port 4010            # http://localhost:4010
# or
docker-compose up -d --build
python -m unittest discover tests
```

Configuration is `RZP_SIM_*` env vars (see `.env.example`).

## What it implements

Razorpay-compatible REST under `/v1` (HTTP Basic `key_id:key_secret`, amounts in paise, Razorpay error envelope `{"error": {"code", "description", "field", ...}}`):

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/v1/orders` | `amount` >= 100, `currency`, `receipt` (<= 40 chars), `notes` |
| GET | `/v1/orders`, `/v1/orders/{id}`, `/v1/orders/{id}/payments` | |
| GET | `/v1/payments`, `/v1/payments/{id}` | |
| POST | `/v1/payments/{id}/capture` | only when `RZP_SIM_AUTO_CAPTURE=false`; amount must match |
| POST | `/v1/payments/{id}/refund` | full or partial; over-refund rejected |
| GET | `/v1/payments/{id}/refunds`, `/v1/refunds/{id}` | |

Customer side (replaces Razorpay Checkout):

- `GET /checkout?order_id=...&callback_url=...` — hosted page with Pay (success) / Pay (fail). On success it POSTs `razorpay_payment_id`, `razorpay_order_id`, `razorpay_signature` to `callback_url`, like Checkout's redirect mode.
- `POST /simulator/orders/{order_id}/pay` — same thing for scripts: `{"outcome": "success"|"failure", "method": "card"|"upi"|"netbanking"|"wallet", "webhook_duplicates": 1}`.
- `GET /simulator/webhooks[?event=...]` — delivery log (body, signature, attempts, status) for assertions.
- `POST /simulator/reset` — wipe all state.

Webhooks: `payment.authorized`, `payment.captured`, `payment.failed`, `order.paid`, `refund.processed`, sent to `RZP_SIM_WEBHOOK_URL` with `X-Razorpay-Signature` (HMAC-SHA256 of the raw body using `RZP_SIM_WEBHOOK_SECRET`) and `X-Razorpay-Event-Id`. Non-2xx or network errors are retried (`RZP_SIM_WEBHOOK_RETRIES`, default 3) with the same event id.

Not simulated: disputes, settlements/payouts, subscriptions, payment links, UPI collect timeouts, partial payments on one order, 3-D Secure.

## Contract the ERP integration must follow

Same rules as real Razorpay, so the swap to production is only config:

1. **Create the order server-side** (`POST /v1/orders`), store `order_id` against the fee record, and send only `order_id` and the public key id to the browser. Never expose the key secret.
2. **Verify the checkout response**: `HMAC_SHA256(key_secret, "{order_id}|{payment_id}")` must equal `razorpay_signature` (constant-time compare).
3. **Treat webhooks as the source of truth for marking a fee paid.** Verify `X-Razorpay-Signature` against the **raw** body before parsing; dedupe on `X-Razorpay-Event-Id` (deliveries repeat); return 2xx quickly.
4. **Amounts are integer paise.** Check that the captured amount equals the fee amount due.
5. Pull the base URL, key id/secret and webhook secret from `.env`; the simulator values are `http://localhost:4010`, `rzp_test_simulator`, `simulator_secret`, `simulator_webhook_secret`. Production swaps in `https://api.razorpay.com` and real credentials.

`app/signing.py` has reference implementations of both signature checks to copy into the finance module.

## Pointing the Razorpay SDK at it

The official SDKs call `https://api.razorpay.com` by default; override the client's base URL (for the Python SDK, the `base_url` attribute on `razorpay.Client` — check your SDK version) to `http://localhost:4010`. Alternatively use `httpx` with Basic auth directly.

## Quick manual run

```powershell
$a = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("rzp_test_simulator:simulator_secret"))
$o = Invoke-RestMethod -Method Post http://localhost:4010/v1/orders -Headers @{Authorization="Basic $a"} -ContentType application/json -Body '{"amount":50000,"receipt":"fee-1"}'
Start-Process "http://localhost:4010/checkout?order_id=$($o.id)"
```
