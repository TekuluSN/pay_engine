import hashlib
import hmac


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def checkout_signature(order_id: str, payment_id: str, key_secret: str) -> str:
    """razorpay_signature returned to the client after a successful checkout."""
    return _hmac_hex(key_secret, f"{order_id}|{payment_id}".encode())


def verify_checkout_signature(order_id: str, payment_id: str, signature: str, key_secret: str) -> bool:
    return hmac.compare_digest(checkout_signature(order_id, payment_id, key_secret), signature)


def webhook_signature(raw_body: bytes, webhook_secret: str) -> str:
    """X-Razorpay-Signature header: HMAC-SHA256 of the raw request body."""
    return _hmac_hex(webhook_secret, raw_body)


def verify_webhook_signature(raw_body: bytes, signature: str, webhook_secret: str) -> bool:
    return hmac.compare_digest(webhook_signature(raw_body, webhook_secret), signature)
