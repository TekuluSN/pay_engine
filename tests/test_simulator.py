import unittest

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.signing import verify_checkout_signature, verify_webhook_signature

AUTH = (settings.key_id, settings.key_secret)


class SimulatorTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        settings.auto_capture = True
        settings.webhook_url = ""
        self.client.post("/simulator/reset")

    def _order(self, amount=50000, **extra):
        res = self.client.post("/v1/orders", json={"amount": amount, "currency": "INR", **extra}, auth=AUTH)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def _pay(self, order_id, **body):
        return self.client.post(f"/simulator/orders/{order_id}/pay", json=body)


class AuthAndOrders(SimulatorTestCase):
    def test_rejects_missing_or_wrong_credentials(self):
        self.assertEqual(self.client.post("/v1/orders", json={"amount": 500}).status_code, 401)
        res = self.client.post("/v1/orders", json={"amount": 500}, auth=(settings.key_id, "nope"))
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "BAD_REQUEST_ERROR")

    def test_create_and_fetch_order(self):
        order = self._order(receipt="fee-123", notes={"student": "S1"})
        self.assertTrue(order["id"].startswith("order_"))
        self.assertEqual((order["status"], order["amount_due"], order["amount_paid"]), ("created", 50000, 0))
        fetched = self.client.get(f"/v1/orders/{order['id']}", auth=AUTH).json()
        self.assertEqual(fetched["receipt"], "fee-123")
        self.assertEqual(fetched["notes"], {"student": "S1"})

    def test_minimum_amount_and_unknown_id(self):
        res = self.client.post("/v1/orders", json={"amount": 99}, auth=AUTH)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["field"], "amount")
        self.assertEqual(self.client.get("/v1/orders/order_missing", auth=AUTH).status_code, 400)

    def test_missing_amount_uses_razorpay_error_envelope(self):
        res = self.client.post("/v1/orders", json={}, auth=AUTH)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["field"], "amount")


class PaymentFlow(SimulatorTestCase):
    def test_successful_payment_auto_captures_and_signs(self):
        order = self._order()
        body = self._pay(order["id"], email="p@example.com").json()
        pay = body["payment"]
        self.assertEqual((pay["status"], pay["captured"]), ("captured", True))
        self.assertEqual(pay["fee"], 1000)
        self.assertTrue(
            verify_checkout_signature(order["id"], body["razorpay_payment_id"], body["razorpay_signature"], settings.key_secret)
        )
        refreshed = self.client.get(f"/v1/orders/{order['id']}", auth=AUTH).json()
        self.assertEqual((refreshed["status"], refreshed["amount_paid"], refreshed["amount_due"]), ("paid", 50000, 0))
        items = self.client.get(f"/v1/orders/{order['id']}/payments", auth=AUTH).json()["items"]
        self.assertEqual([i["id"] for i in items], [pay["id"]])

    def test_failed_payment_leaves_order_open_for_retry(self):
        order = self._order()
        failed = self._pay(order["id"], outcome="failure").json()
        self.assertEqual(failed["payment"]["status"], "failed")
        self.assertNotIn("razorpay_signature", failed)
        mid = self.client.get(f"/v1/orders/{order['id']}", auth=AUTH).json()
        self.assertEqual((mid["status"], mid["attempts"], mid["amount_due"]), ("attempted", 1, 50000))
        self.assertEqual(self._pay(order["id"]).json()["payment"]["status"], "captured")

    def test_paid_order_rejects_second_payment(self):
        order = self._order()
        self._pay(order["id"])
        self.assertEqual(self._pay(order["id"]).status_code, 400)

    def test_manual_capture_when_auto_capture_off(self):
        settings.auto_capture = False
        order = self._order()
        pay = self._pay(order["id"]).json()["payment"]
        self.assertEqual(pay["status"], "authorized")
        self.assertEqual(self.client.get(f"/v1/orders/{order['id']}", auth=AUTH).json()["status"], "attempted")

        wrong = self.client.post(f"/v1/payments/{pay['id']}/capture", json={"amount": 100}, auth=AUTH)
        self.assertEqual(wrong.status_code, 400)
        ok = self.client.post(f"/v1/payments/{pay['id']}/capture", json={"amount": 50000}, auth=AUTH)
        self.assertEqual(ok.json()["status"], "captured")
        self.assertEqual(self.client.get(f"/v1/orders/{order['id']}", auth=AUTH).json()["status"], "paid")
        again = self.client.post(f"/v1/payments/{pay['id']}/capture", json={"amount": 50000}, auth=AUTH)
        self.assertEqual(again.status_code, 400)


class Refunds(SimulatorTestCase):
    def _captured(self):
        order = self._order()
        return self._pay(order["id"]).json()["payment"]

    def test_partial_then_full_refund(self):
        pay = self._captured()
        first = self.client.post(f"/v1/payments/{pay['id']}/refund", json={"amount": 20000}, auth=AUTH).json()
        self.assertEqual((first["status"], first["amount"]), ("processed", 20000))
        mid = self.client.get(f"/v1/payments/{pay['id']}", auth=AUTH).json()
        self.assertEqual((mid["refund_status"], mid["amount_refunded"], mid["status"]), ("partial", 20000, "captured"))

        self.client.post(f"/v1/payments/{pay['id']}/refund", json={}, auth=AUTH)
        end = self.client.get(f"/v1/payments/{pay['id']}", auth=AUTH).json()
        self.assertEqual((end["refund_status"], end["amount_refunded"], end["status"]), ("full", 50000, "refunded"))
        listed = self.client.get(f"/v1/payments/{pay['id']}/refunds", auth=AUTH).json()
        self.assertEqual(listed["count"], 2)

    def test_over_refund_and_uncaptured_refund_rejected(self):
        pay = self._captured()
        res = self.client.post(f"/v1/payments/{pay['id']}/refund", json={"amount": 50001}, auth=AUTH)
        self.assertEqual(res.status_code, 400)

        failed = self._pay(self._order()["id"], outcome="failure").json()["payment"]
        res = self.client.post(f"/v1/payments/{failed['id']}/refund", json={}, auth=AUTH)
        self.assertEqual(res.status_code, 400)


class Webhooks(SimulatorTestCase):
    def _events(self, name=None):
        params = {"event": name} if name else {}
        return self.client.get("/simulator/webhooks", params=params).json()["items"]

    def test_success_emits_signed_authorized_captured_and_order_paid(self):
        order = self._order()
        self._pay(order["id"])
        self.assertEqual(
            [e["event"] for e in self._events()], ["payment.authorized", "payment.captured", "order.paid"]
        )
        record = self._events("order.paid")[0]
        self.assertEqual(record["body"]["contains"], ["payment", "order"])
        self.assertEqual(record["body"]["payload"]["order"]["entity"]["id"], order["id"])
        self.assertIsNone(record["url"])
        # Signature is over the exact compact-JSON body that would be POSTed.
        import json

        raw = json.dumps(record["body"], separators=(",", ":")).encode()
        self.assertTrue(verify_webhook_signature(raw, record["signature"], settings.webhook_secret))

    def test_failure_and_refund_events(self):
        self._pay(self._order()["id"], outcome="failure")
        self.assertEqual([e["event"] for e in self._events()], ["payment.failed"])

        pay = self._pay(self._order()["id"]).json()["payment"]
        self.client.post(f"/v1/payments/{pay['id']}/refund", json={"amount": 100}, auth=AUTH)
        self.assertEqual(len(self._events("refund.processed")), 1)

    def test_duplicate_deliveries_share_event_id(self):
        self._pay(self._order()["id"], webhook_duplicates=2)
        captured = self._events("payment.captured")
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["event_id"], captured[1]["event_id"])

    def test_checkout_page(self):
        order = self._order()
        res = self.client.get("/checkout", params={"order_id": order["id"]})
        self.assertEqual(res.status_code, 200)
        self.assertIn(order["id"], res.text)
        self.assertEqual(self.client.get("/checkout", params={"order_id": "nope"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
