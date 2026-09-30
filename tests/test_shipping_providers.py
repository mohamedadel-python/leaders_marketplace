"""B6: shipping carrier framework (providers, labels, webhook)."""
import hashlib
import hmac
import json
import time

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_e2e_flows import E2ECommon

HOOK = "/api/v1/shipping/webhook/dummy"
ENABLED = "leaders_marketplace.shipping_providers_enabled"


class ShippingCommon(E2ECommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.set_param = cls.env["ir.config_parameter"].sudo().set_param
        cls.set_param(ENABLED, "True")
        cls.dummy = cls.env.ref("leaders_marketplace.shipping_provider_dummy")
        cls.dummy.active = True
        cls.dummy.webhook_secret = "s3cret"
        cls.customer.email = "ship.buyer@example.com"

    def _group(self, product=None, cod=False):
        order = self._order([(product or self.fbm_product, 1)], cod=cod)
        order.action_confirm()
        group = order.vendor_order_ids.fulfillment_group_ids
        group.shipping_provider_id = self.dummy
        return group

    def _shipment(self, **kw):
        group = self._group(**kw)
        group.action_create_shipment()
        return group

    def _sign(self, body, secret="s3cret", ts=None):
        signed = (ts.encode() + b"." + body) if ts else body
        return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()

    def _hook(self, events, sig=True, secret="s3cret", headers=None, url=HOOK):
        body = json.dumps({"events": events}).encode()
        hdrs = {"Content-Type": "application/json"}
        if sig:
            hdrs["X-Signature"] = self._sign(body, secret)
        hdrs.update(headers or {})
        return self.url_open(url, data=body, headers=hdrs)

    def _ev(self, group, status, eid):
        return {"id": eid, "awb": group.awb, "status": status, "raw_status": status.upper()}


@tagged("post_install", "-at_install")
class TestShippingProviderModel(ShippingCommon):

    def test_acl_and_hidden_credentials(self):
        internal = self.env["res.users"].create({
            "name": "Plain Internal", "login": "plain.internal.b6", "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id])]})
        prov = self.dummy.with_user(internal)
        self.assertEqual(prov.name, self.dummy.name)
        with self.assertRaises(AccessError):
            prov.webhook_secret
        with self.assertRaises(AccessError):
            prov.credentials
        with self.assertRaises(AccessError):
            prov.write({"name": "x"})
        with self.assertRaises(AccessError):
            self.dummy.with_user(self.owner_user).read(["name"])
        self.assertNotIn("webhook_secret", self.env["leaders.shipping.provider"].with_user(internal).fields_get())

    def test_code_unique_and_valid(self):
        Provider = self.env["leaders.shipping.provider"]
        with self.assertRaises(Exception), self.cr.savepoint():
            Provider.create({"name": "dup", "code": "dummy"})
        with self.assertRaises(Exception):
            Provider.create({"name": "bad", "code": "Bad Code!"})

    def test_generate_secret(self):
        self.dummy.action_generate_secret()
        self.assertEqual(len(self.dummy.webhook_secret), 64)

    def test_create_shipment_manual(self):
        group = self._group()
        group.shipping_provider_id = self.env.ref("leaders_marketplace.shipping_provider_manual")
        group.tracking_ref = "MAN-1"
        group.action_create_shipment()
        self.assertEqual(group.awb, "MAN-1")
        self.assertFalse(group.label_attachment_id)

    def test_create_shipment_dummy_with_label(self):
        group = self._shipment()
        self.assertTrue(group.awb.startswith("DUM"))
        self.assertEqual(group.tracking_ref, group.awb)
        self.assertEqual(group.shipping_status, "created")
        self.assertTrue(group.tracking_url)
        att = group.label_attachment_id
        self.assertEqual((att.res_model, att.res_id), ("leaders.fulfillment.group", group.id))
        self.assertTrue(att.raw.startswith(b"%PDF"))
        with self.assertRaises(UserError):
            group.action_create_shipment()

    def test_disabled_setting(self):
        self.set_param(ENABLED, "False")
        with self.assertRaises(UserError):
            self._group().action_create_shipment()
        self.assertFalse(self.env["leaders.shipping.provider"]._leaders_portal_providers())


@tagged("post_install", "-at_install")
class TestShippingWebhook(ShippingCommon):

    def test_signature(self):
        group = self._shipment()
        ev = [self._ev(group, "in_transit", "e1")]
        self.assertEqual(self._hook(ev, sig=False).status_code, 401)
        self.assertEqual(self._hook(ev, secret="wrong").status_code, 401)
        self.assertEqual(self._hook(ev).status_code, 200)
        self.assertEqual(group.shipping_status, "in_transit")

    def test_timestamp(self):
        group = self._shipment()
        body = json.dumps({"events": [self._ev(group, "in_transit", "t1")]}).encode()
        for ts, code in ((str(int(time.time())), 200), (str(int(time.time()) - 4000), 401)):
            resp = self.url_open(HOOK, data=body, headers={
                "Content-Type": "application/json", "X-Timestamp": ts,
                "X-Signature": self._sign(body, ts=ts)})
            self.assertEqual(resp.status_code, code)

    def test_unknown_provider_and_switch_off_and_size(self):
        self.assertEqual(self._hook([], url="/api/v1/shipping/webhook/nope").status_code, 404)
        big = self.url_open(HOOK, data=b"x" * (1024 * 1024 + 10), headers={"X-Signature": "a"})
        self.assertEqual(big.status_code, 413)
        self.set_param(ENABLED, "False")
        self.assertEqual(self._hook([]).status_code, 404)

    def test_bad_payload(self):
        body = b"not json"
        resp = self.url_open(HOOK, data=body, headers={"X-Signature": self._sign(body)})
        self.assertEqual(resp.status_code, 400)

    def test_idempotent_replay_and_unknown_awb(self):
        group = self._shipment()
        ev = [self._ev(group, "picked_up", "p1")]
        first = self._hook(ev).json()
        self.assertEqual(first["applied"], 1)
        self.assertEqual(group.state, "shipped")
        second = self._hook(ev).json()
        self.assertEqual((second["applied"], second["duplicates"]), (0, 1))
        self.assertEqual(len(group.shipping_event_ids), 1)
        resp = self._hook([{"id": "u1", "awb": "NOPE", "status": "delivered"}])
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self.env["leaders.shipping.event"].search([("event_id", "=", "u1")]).group_id.id, False)

    def test_picked_up_ships_fbm_only(self):
        fbl = self._group(self.b_product)
        fbl.action_create_shipment()
        self._hook([self._ev(fbl, "picked_up", "f1")])
        self.assertNotEqual(fbl.state, "shipped")
        event = fbl.shipping_event_ids
        self.assertFalse(event.processed)

    def test_delivered_vendor_mode(self):
        group = self._shipment()
        self._hook([self._ev(group, "picked_up", "a"), self._ev(group, "delivered", "b")])
        self.assertEqual(group.state, "delivered")
        self.assertEqual(group.shipping_status, "delivered")
        self.assertTrue(group.delivered_reported_by)

    def test_delivered_customer_confirm_mode(self):
        self.set_param("leaders_marketplace.fbm_delivery_mode", "customer_confirm")
        group = self._shipment()
        self._hook([self._ev(group, "picked_up", "a"), self._ev(group, "delivered", "b")])
        self.assertEqual(group.state, "delivered")
        self.assertEqual(group.delivered_confirmed_by, self.env.ref("base.user_root"))

    def test_delivered_before_shipping_ships_then_delivers(self):
        # F4: was "logged only"; an out-of-order delivered now applies the ship step first.
        group = self._shipment()
        self._hook([self._ev(group, "delivered", "d")])
        self.assertEqual(group.state, "delivered")

    def test_refused_cod_runs_refusal_flow(self):
        group = self._shipment(cod=True)
        self._hook([self._ev(group, "picked_up", "a"), self._ev(group, "refused", "b")])
        self.assertEqual(group.state, "cancelled")
        self.assertTrue(group.refused_at_delivery)
        self._assert_settled(self.vendor_a)
        self.assertEqual(self._receivable_balance(), 0.0)

    def test_returned_non_cod_is_logged_only(self):
        group = self._shipment()
        self._hook([self._ev(group, "picked_up", "a"), self._ev(group, "returned", "b")])
        self.assertEqual(group.state, "shipped")
        self.assertEqual(group.shipping_status, "returned")


@tagged("post_install", "-at_install")
class TestShippingPortal(ShippingCommon):

    def test_portal_create_and_label(self):
        from .test_security_rpc import PORTAL_PASSWORD
        group = self._group()
        page = "/my/vendor/orders/%s" % group.vendor_order_id.id
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self.assertIn("Create shipment / label", self.url_open(page).text)
        self._post("/my/vendor/fulfillment/%s/create-shipment" % group.id, page, provider_id=self.dummy.id)
        self.assertTrue(group.awb)
        self.assertEqual(self.url_open("/my/vendor/fulfillment/%s/label" % group.id).status_code, 200)
        self.authenticate(self.owner_b.login, PORTAL_PASSWORD)
        self.assertEqual(self.url_open("/my/vendor/fulfillment/%s/label" % group.id).status_code, 404)
        self.set_param(ENABLED, "False")
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self.assertEqual(self.url_open("/my/vendor/fulfillment/%s/label" % group.id).status_code, 404)
