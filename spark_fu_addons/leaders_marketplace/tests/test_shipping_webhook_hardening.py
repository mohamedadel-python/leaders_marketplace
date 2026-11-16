"""F4: shipping webhook rate limits and out-of-order events."""
import json
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_shipping_providers import HOOK, ShippingCommon

P_IP = "leaders_marketplace.shipping_webhook_rate_limit_per_minute"
P_PROV = "leaders_marketplace.shipping_webhook_provider_rate_limit_per_minute"
NOW = "odoo.addons.leaders_marketplace.models.vendor_api.LeadersVendorApiKey._leaders_now"


@tagged("post_install", "-at_install")
class TestShippingWebhookRateLimit(ShippingCommon):

    def test_defaults_when_missing(self):
        Provider = self.env["leaders.shipping.provider"]
        self.env["ir.config_parameter"].sudo().search([("key", "in", [P_IP, P_PROV])]).unlink()
        self.assertEqual(Provider._leaders_webhook_limit(P_IP), 120)
        self.assertEqual(Provider._leaders_webhook_limit(P_PROV), 600)
        self.set_param(P_IP, "0")
        self.assertEqual(Provider._leaders_webhook_limit(P_IP), 0)

    def test_http_429_with_retry_after_and_reset(self):
        self.set_param(P_IP, "2")
        with mock.patch(NOW, return_value=6000.0 + 10):
            self.assertEqual(self._hook([]).status_code, 200)
            self.assertEqual(self._hook([]).status_code, 200)
            resp = self._hook([])
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.headers["Retry-After"], "51")
            self.assertEqual(resp.json()["error"], "rate_limited")
            # signature is checked only for calls under the limit
        with mock.patch(NOW, return_value=6060.0 + 1):
            self.assertEqual(self._hook([]).status_code, 200)

    def test_hmac_still_enforced_under_limit(self):
        self.assertEqual(self._hook([], sig=False).status_code, 401)
        self.assertEqual(self._hook([], secret="bad").status_code, 401)

    def test_limit_off(self):
        self.set_param(P_IP, "0")
        self.set_param(P_PROV, "0")
        for _i in range(5):
            self.assertEqual(self._hook([]).status_code, 200)
        self.assertFalse(self.env["leaders.shipping.webhook.rate"].search([]))

    def test_counters_not_shared(self):
        self.set_param(P_IP, "1")
        self.set_param(P_PROV, "3")
        other = self.env["leaders.shipping.provider"].create({
            "name": "Other", "code": "other", "provider_type": "dummy"})
        with mock.patch(NOW, return_value=12000.0):
            self.assertIsNone(self.dummy._webhook_rate_check("1.1.1.1"))
            self.assertIsNotNone(self.dummy._webhook_rate_check("1.1.1.1"))
            self.assertIsNone(self.dummy._webhook_rate_check("2.2.2.2"))
            self.assertIsNone(other._webhook_rate_check("1.1.1.1"))
            # rejected call was not counted against the provider total: 2 counted so far
            self.assertIsNone(self.dummy._webhook_rate_check("3.3.3.3"))
            self.assertIsNotNone(self.dummy._webhook_rate_check("4.4.4.4"))  # provider total 3 hit
            self.assertIsNone(other._webhook_rate_check("9.9.9.9"))

    def test_cleanup_cron(self):
        Rate = self.env["leaders.shipping.webhook.rate"]
        with mock.patch(NOW, return_value=60.0):
            self.dummy._webhook_rate_check("1.1.1.1")
        self.assertTrue(Rate.search([]))
        with mock.patch(NOW, return_value=60.0 * 500):
            self.env["leaders.vendor.api.key"]._cron_cleanup()
        self.assertFalse(Rate.search([]))


@tagged("post_install", "-at_install")
class TestShippingWebhookOutOfOrder(ShippingCommon):

    def _assert_done(self, group):
        self.assertEqual(group.state, "delivered")
        self.assertTrue(all(p.state == "done" for p in group.picking_ids))
        self.assertEqual(group.vendor_order_id.state, "completed")

    def test_delivered_first_vendor_mode(self):
        group = self._shipment()
        resp = self._hook([self._ev(group, "delivered", "d1")]).json()
        self.assertEqual((resp["applied"], resp["failed"]), (1, 0))
        self._assert_done(group)
        self.assertTrue(group.delivered_reported_by)
        self.assertEqual(len(group.shipping_event_ids), 1)

    def test_delivered_first_customer_confirm_mode(self):
        self.set_param("leaders_marketplace.fbm_delivery_mode", "customer_confirm")
        group = self._shipment()
        self._hook([self._ev(group, "delivered", "d1")])
        self._assert_done(group)
        self.assertEqual(group.delivered_confirmed_by, self.env.ref("base.user_root"))

    def test_delivered_then_late_and_repeated_events(self):
        group = self._shipment()
        self._hook([self._ev(group, "delivered", "d1")])
        again = self._hook([self._ev(group, "delivered", "d2"), self._ev(group, "picked_up", "p1"),
                            self._ev(group, "delivered", "d1")]).json()
        self.assertEqual(again["duplicates"], 1)
        self.assertEqual(again["failed"], 0)
        self.assertEqual(group.state, "delivered")
        self._assert_done(group)
        self.assertEqual(len(group.shipping_event_ids), 3)

    def test_in_transit_before_pickup_is_log_only(self):
        group = self._shipment()
        self._hook([self._ev(group, "in_transit", "t1")])
        self.assertEqual(group.state, "ready")
        self.assertEqual(group.shipping_status, "in_transit")

    def test_fbl_delivered_first_logged_not_applied(self):
        fbl = self._group(self.b_product)
        fbl.action_create_shipment()
        resp = self._hook([self._ev(fbl, "delivered", "f1")]).json()
        self.assertEqual(resp["ignored"], 1)
        self.assertNotIn(fbl.state, ("shipped", "delivered"))
        self.assertFalse(fbl.shipping_event_ids.processed)

    def test_ship_failure_recorded_as_failed(self):
        group = self._shipment()
        model = type(group)
        with mock.patch.object(model, "action_ship", side_effect=UserError("Not enough stock")):
            resp = self._hook([self._ev(group, "delivered", "x1")])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["failed"], 1)
        self.assertEqual(group.state, "ready")
        event = group.shipping_event_ids
        self.assertFalse(event.processed)
        self.assertIn("Not enough stock", event.note)
        # the carrier retry with the same id is a duplicate (no retry storm)
        self.assertEqual(self._hook([self._ev(group, "delivered", "x1")]).json()["duplicates"], 1)

    def test_raw_payload_stored_and_capped(self):
        group = self._shipment()
        self._hook([self._ev(group, "in_transit", "r1")])
        self.assertEqual(json.loads(group.shipping_event_ids.payload)["events"][0]["id"], "r1")
        big = dict(self._ev(group, "in_transit", "r2"), note="x" * 30000)
        self._hook([big])
        event = group.shipping_event_ids.filtered(lambda e: e.event_id == "r2")
        self.assertEqual(len(event.payload), 20 * 1024)
