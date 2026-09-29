"""B4: vendor write API (/api/v1/vendor/*, keys, rate limit, idempotency, OpenAPI)."""
import json
import re
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import PORTAL_PASSWORD, MarketplacePortalUserCommon

ENABLED = "leaders_marketplace.vendor_api_write_enabled"
RATE = "leaders_marketplace.vendor_api_rate_limit_per_minute"
BASE = "/api/v1/vendor"


class VendorApiCommon(HttpCase, MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.param = env["ir.config_parameter"].sudo()
        cls.param.set_param(ENABLED, "True")
        cls.param.set_param(RATE, "0")
        cls.param.set_param("leaders_marketplace.vendor_discounts_enabled", "True")
        cls.Key = env["leaders.vendor.api.key"]
        env["leaders.commission.rule"].create({
            "name": "10% vendor", "scope": "vendor", "vendor_id": cls.vendor.id,
            "rule_type": "percent", "percent": 10.0})
        cls.wkey_rec, cls.wkey = cls.Key._leaders_generate(cls.vendor, cls.owner_user, "write", "write")
        cls.rkey_rec, cls.rkey = cls.Key._leaders_generate(cls.vendor, cls.owner_user, "read", "read")

        # vendor B with an offer and a shipped order
        cls.vendor_b = env["res.partner"].create({
            "name": "API Rival", "is_marketplace_vendor": True, "vendor_state": "approved"})
        cls.vendor_b._marketplace_ensure_vendor_stock()
        contact = env["res.partner"].create({
            "name": "Rival Owner", "email": "rival.owner@example.com",
            "parent_id": cls.vendor_b.id, "type": "contact"})
        cls.owner_b = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Rival Owner", "login": "rival.owner@example.com", "email": "rival.owner@example.com",
            "password": PORTAL_PASSWORD, "partner_id": contact.id,
            "groups_id": [Command.set([env.ref("leaders_marketplace.group_vendor_owner").id])]})
        cls.b_product = env["product.product"].create({
            "name": "Rival Product", "type": "consu", "is_storable": True,
            "list_price": 9.0, "taxes_id": [Command.clear()]})
        cls.b_offer = env["leaders.marketplace.offer"].create({
            "product_tmpl_id": cls.b_product.product_tmpl_id.id, "product_id": cls.b_product.id,
            "vendor_id": cls.vendor_b.id, "seller_sku": "RIVAL-1", "price": 9.0,
            "fulfillment_type": "fbm",
            "stock_location_id": cls.vendor_b._marketplace_stock_location("fbm").id})
        env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.b_product, cls.vendor_b.vendor_location_id, cls.vendor_b, 10)
        cls.b_offer.action_approve()
        cls.bkey_rec, cls.bkey = cls.Key._leaders_generate(cls.vendor_b, cls.owner_b, "b", "write")
        order = env["sale.order"].create({
            "partner_id": cls.customer.id,
            "order_line": [Command.create({"product_id": cls.b_product.id, "product_uom_qty": 1})]})
        order.action_confirm()
        cls.b_order = order.vendor_order_ids
        cls.b_group = cls.b_order.fulfillment_group_ids

    # ------------------------------------------------------------ helpers
    def call(self, method, path, key=None, body=None, headers=None, raw=None):
        hdrs = dict(headers or {})
        if key:
            hdrs["Authorization"] = "Bearer %s" % key
        data = raw
        if body is not None:
            data = json.dumps(body)
            hdrs.setdefault("Content-Type", "application/json")
        return self.opener.request(method, self.base_url() + path, data=data, headers=hdrs, timeout=30)

    def get(self, path, key=None, **kw):
        return self.call("GET", path, key or self.wkey, **kw)

    def a_order(self, qty=4):
        order = self._confirmed_sale_order(self.fbm_product, qty)
        return order.vendor_order_ids

    def assertError(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        data = response.json()
        self.assertEqual(data["error"], code, response.text)
        self.assertIn("message", data)
        self.assertIsInstance(data["details"], list)
        return data


@tagged("post_install", "-at_install")
class TestVendorApiKeys(VendorApiCommon):

    def test_key_is_stored_hashed_with_prefix(self):
        rec, plain = self.Key._leaders_generate(self.vendor, self.owner_user, "hash", "read")
        self.assertTrue(plain.startswith("lmk_"))
        self.assertEqual(rec.key_prefix, plain.split("_")[1])
        self.assertNotEqual(rec.key_hash, plain)
        self.assertNotIn(plain, rec.key_hash)
        self.env.cr.execute("SELECT count(*) FROM leaders_vendor_api_key WHERE key_hash = %s", [plain])
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        self.assertEqual(self.Key._leaders_authenticate(plain), rec)
        rec.action_revoke()
        self.assertFalse(self.Key._leaders_authenticate(plain))

    def test_missing_invalid_and_revoked_keys_are_401(self):
        self.assertError(self.call("GET", BASE + "/offers"), 401, "unauthorized")
        self.assertError(self.call("GET", BASE + "/offers", "lmk_deadbeef_" + "x" * 30), 401, "unauthorized")
        self.assertError(self.call("GET", BASE + "/offers", "garbage"), 401, "unauthorized")
        rec, plain = self.Key._leaders_generate(self.vendor, self.owner_user, "tmp", "read")
        self.assertEqual(self.call("GET", BASE + "/offers", plain).status_code, 200)
        rec.action_revoke()
        response = self.call("GET", BASE + "/offers", plain)
        self.assertError(response, 401, "unauthorized")
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertTrue(rec.last_used)

    def test_read_key_cannot_write(self):
        response = self.call("PATCH", "%s/offers/%s" % (BASE, self.fbm_offer.id), self.rkey, {"price": 5})
        self.assertError(response, 403, "insufficient_scope")
        self.assertEqual(self.fbm_offer.price, 100.0)
        self.assertEqual(self.call("GET", BASE + "/offers", self.rkey).status_code, 200)

    def test_switch_off_is_404(self):
        self.param.set_param(ENABLED, "False")
        for method, path in (("GET", "/offers"), ("GET", "/orders"), ("GET", "/wallet"),
                             ("PATCH", "/offers/%s" % self.fbm_offer.id), ("POST", "/orders/1/ship")):
            self.assertError(self.call(method, BASE + path, self.wkey, {} if method != "GET" else None),
                             404, "not_found")
        self.assertEqual(self.call("GET", "/api/v1/openapi.json").status_code, 404)

    def test_terms_pending_is_403(self):
        self.env["leaders.vendor.terms"].with_context(active_test=False).search([]).write({"active": False})
        terms = self.env["leaders.vendor.terms"].create({"name": "T", "version": 1, "body": "<p>x</p>"})
        response = self.get(BASE + "/offers")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "terms_not_accepted")
        terms.action_accept(self.vendor, self.owner_user, "1.1.1.1")
        self.assertEqual(self.get(BASE + "/offers").status_code, 200)

    def test_native_key_still_works_on_the_legacy_orders_route(self):
        native = self.env["res.users.apikeys"].with_user(self.owner_user).sudo()._generate(
            None, "lm legacy", None)
        response = self.call("GET", BASE + "/orders", native)
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.json())
        self.assertNotIn("total", response.json())  # the original payload, untouched
        self.assertEqual(self.call("GET", BASE + "/orders", "notakey").status_code, 401)
        self.assertEqual(self.call("GET", BASE + "/orders").status_code, 401)
        # a marketplace key gets the new paginated payload on the same URL
        self.assertIn("total", self.get(BASE + "/orders").json())


@tagged("post_install", "-at_install")
class TestVendorApiOffers(VendorApiCommon):

    def test_list_get_and_scoping(self):
        data = self.get(BASE + "/offers").json()
        ids = {o["id"] for o in data["items"]}
        self.assertEqual(ids, {self.fbm_offer.id, self.fbl_offer.id})
        self.assertNotIn(self.b_offer.id, ids)
        self.assertEqual(data["total"], 2)
        one = self.get("%s/offers/%s" % (BASE, self.fbm_offer.id)).json()
        self.assertEqual(one["seller_sku"], "FBM-1")
        self.assertEqual(one["stock_available"], 5.0)
        self.assertEqual(one["discount"]["state"], "none")
        self.assertError(self.get("%s/offers/%s" % (BASE, self.b_offer.id)), 404, "not_found")
        filtered = self.get(BASE + "/offers?fulfillment_type=fbl").json()
        self.assertEqual([o["id"] for o in filtered["items"]], [self.fbl_offer.id])

    def test_vendor_b_key_cannot_touch_vendor_a(self):
        url = "%s/offers/%s" % (BASE, self.fbm_offer.id)
        self.assertError(self.call("GET", url, self.bkey), 404, "not_found")
        self.assertError(self.call("PATCH", url, self.bkey, {"price": 1}), 404, "not_found")
        self.assertEqual(self.fbm_offer.price, 100.0)
        self.assertEqual(self.call("GET", BASE + "/offers", self.bkey).json()["total"], 1)

    def test_patch_price_follows_the_portal_rule(self):
        self.assertEqual(self.fbm_offer.state, "active")
        response = self.call("PATCH", "%s/offers/%s" % (BASE, self.fbm_offer.id), self.wkey, {"price": 120.5})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["price"], 120.5)
        self.assertEqual(self.fbm_offer.price, 120.5)
        self.assertEqual(self.fbm_offer.state, "draft")  # back to review, like the portal edit
        # same price: nothing changes
        self.fbm_offer.state = "active"
        self.call("PATCH", "%s/offers/%s" % (BASE, self.fbm_offer.id), self.wkey, {"price": 120.5})
        self.assertEqual(self.fbm_offer.state, "active")

    def test_patch_discount_set_and_clear(self):
        url = "%s/offers/%s" % (BASE, self.fbm_offer.id)
        response = self.call("PATCH", url, self.wkey, {"discount": {"type": "percent", "percent": 20}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["discount"]["state"], "active")
        self.assertEqual(self.fbm_offer.discount_percent, 20.0)
        self.assertAlmostEqual(response.json()["effective_price"], 80.0)
        response = self.call("PATCH", url, self.wkey, {"discount": None})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.fbm_offer.discount_state, "none")
        # dates
        start = (fields.Datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        response = self.call("PATCH", url, self.wkey, {"discount": {
            "type": "percent", "percent": 10, "start": start}})
        self.assertEqual(response.json()["discount"]["state"], "scheduled")

    def test_discount_switch_off_is_422(self):
        self.param.set_param("leaders_marketplace.vendor_discounts_enabled", "False")
        response = self.call("PATCH", "%s/offers/%s" % (BASE, self.fbm_offer.id), self.wkey,
                             {"discount": {"type": "percent", "percent": 10}})
        self.assertError(response, 422, "unprocessable")

    def test_patch_stock_fbm_and_fbl_refused(self):
        response = self.call("PATCH", "%s/offers/%s" % (BASE, self.fbm_offer.id), self.wkey, {"stock": 9})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["stock_available"], 9.0)
        response = self.call("PATCH", "%s/offers/%s" % (BASE, self.fbl_offer.id), self.wkey, {"stock": 9})
        self.assertError(response, 422, "unprocessable")
        self.assertIn("FBL", response.json()["message"])

    def test_patch_is_atomic(self):
        url = "%s/offers/%s" % (BASE, self.fbl_offer.id)
        response = self.call("PATCH", url, self.wkey, {"price": 77, "stock": 5})
        self.assertError(response, 422, "unprocessable")
        self.assertEqual(self.fbl_offer.price, 40.0)
        self.assertEqual(self.fbl_offer.state, "active")

    def test_patch_validation(self):
        url = "%s/offers/%s" % (BASE, self.fbm_offer.id)
        for body in ({}, {"price": "abc"}, {"price": -1}, {"price": True}, {"stock": -3},
                     {"nope": 1}, {"discount": 5}, {"discount": {"type": "x"}},
                     {"discount": {"type": "percent"}}, {"discount": {"type": "percent", "percent": 500}},
                     {"discount": {"type": "percent", "percent": 5, "start": "yesterday"}},
                     {"submit": "yes"}):
            response = self.call("PATCH", url, self.wkey, body)
            data = self.assertError(response, 422, "validation_error")
            self.assertTrue(data["details"], body)
        self.assertError(self.call("PATCH", url, self.wkey, raw="{not json"), 400, "invalid_json")
        self.assertError(self.call("PATCH", url, self.wkey, raw="[1]"), 400, "invalid_json")
        self.assertEqual(self.fbm_offer.price, 100.0)

    def test_patch_submit(self):
        self.fbm_offer.write({"state": "draft"})
        response = self.call("PATCH", "%s/offers/%s" % (BASE, self.fbm_offer.id), self.wkey, {"submit": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.fbm_offer.state, "pending")

    def test_frozen_vendor_cannot_patch(self):
        self.vendor.vendor_state = "frozen"
        url = "%s/offers/%s" % (BASE, self.fbm_offer.id)
        self.assertEqual(self.get(url).status_code, 200)
        self.assertError(self.call("PATCH", url, self.wkey, {"price": 5}), 403, "vendor_frozen")

    def test_pagination(self):
        data = self.get(BASE + "/offers?limit=1").json()
        self.assertEqual((data["page"], data["limit"], data["total"]), (1, 1, 2))
        self.assertEqual(len(data["items"]), 1)
        self.assertTrue(data["next"])
        second = self.get(data["next"]).json()
        self.assertEqual(second["page"], 2)
        self.assertIsNone(second["next"])
        self.assertNotEqual(second["items"][0]["id"], data["items"][0]["id"])
        for query in ("limit=101", "limit=0", "page=0", "page=x", "state=bogus"):
            self.assertError(self.get(BASE + "/offers?" + query), 422, "validation_error")
        self.assertEqual(self.get(BASE + "/offers?limit=100").status_code, 200)


@tagged("post_install", "-at_install")
class TestVendorApiOrders(VendorApiCommon):

    def test_list_filters_and_detail(self):
        order = self.a_order(2)
        data = self.get(BASE + "/orders").json()
        self.assertIn(order.id, [o["id"] for o in data["items"]])
        self.assertNotIn(self.b_order.id, [o["id"] for o in data["items"]])
        self.assertTrue(all("vendor_net" in o for o in data["items"]))
        self.assertEqual(self.get(BASE + "/orders?state=cancelled").json()["items"], [])
        self.assertError(self.get(BASE + "/orders?state=bogus"), 422, "validation_error")
        self.assertError(self.get(BASE + "/orders?updated_since=notadate"), 422, "validation_error")
        future = (fields.Datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.get(BASE + "/orders?updated_since=" + future).json()["total"], 0)
        detail = self.get("%s/orders/%s" % (BASE, order.id)).json()
        self.assertEqual(detail["lines"][0]["quantity"], 2.0)
        self.assertEqual(detail["groups"][0]["fulfillment_type"], "fbm")
        self.assertError(self.get("%s/orders/%s" % (BASE, self.b_order.id)), 404, "not_found")

    def test_other_vendor_cannot_ship_or_deliver(self):
        for action in ("ship", "delivered"):
            response = self.call("POST", "%s/orders/%s/%s" % (BASE, self.b_order.id, action), self.wkey,
                                 {"tracking_ref": "X"})
            self.assertError(response, 404, "not_found")
        self.assertEqual(self.b_group.state, "ready")

    def test_ship_all_and_partial(self):
        order = self.a_order(4)
        group = order.fulfillment_group_ids
        line = order.line_ids.filtered(lambda l: not l.is_delivery)[:1]
        url = "%s/orders/%s/ship" % (BASE, order.id)
        self.assertError(self.call("POST", url, self.wkey, {}), 422, "validation_error")
        self.assertError(self.call("POST", url, self.wkey, {"tracking_ref": "T", "lines": [
            {"line_id": line.id, "quantity": 99}]}), 422, "unprocessable")
        self.assertError(self.call("POST", url, self.wkey, {"tracking_ref": "T", "lines": [
            {"line_id": "a", "quantity": 1}]}), 422, "validation_error")
        self.assertEqual(group.state, "ready")
        response = self.call("POST", url, self.wkey, {"tracking_ref": "P-1", "lines": [
            {"line_id": line.id, "quantity": 1}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(group.state, "partially_shipped")
        self.assertEqual(response.json()["groups"][0]["state"], "partially_shipped")
        # the rest needs its own reference (model rule -> 422)
        self.assertError(self.call("POST", url, self.wkey, {"tracking_ref": "P-2", "lines": []}),
                         422, "validation_error")
        response = self.call("POST", url, self.wkey, {"tracking_ref": "P-2"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(group.state, "shipped")
        self.assertEqual(order.state, "fulfilled")
        # shipping again is refused
        self.assertError(self.call("POST", url, self.wkey, {"tracking_ref": "P-3"}), 422, "unprocessable")

    def test_ship_via_groups_list(self):
        order = self.a_order(1)
        group = order.fulfillment_group_ids
        response = self.call("POST", "%s/orders/%s/ship" % (BASE, order.id), self.wkey, {
            "groups": [{"group_id": group.id, "tracking_ref": "G-1"}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(group.state, "shipped")
        self.assertEqual(group.tracking_ref, "G-1")
        self.assertError(self.call("POST", "%s/orders/%s/ship" % (BASE, order.id), self.wkey, {
            "groups": [{"group_id": self.b_group.id, "tracking_ref": "G-1"}]}), 404, "not_found")

    def test_delivered_vendor_mode_and_customer_confirm_mode(self):
        order = self.a_order(1)
        group = order.fulfillment_group_ids
        url = "%s/orders/%s/delivered" % (BASE, order.id)
        self.assertError(self.call("POST", url, self.wkey, {}), 422, "unprocessable")  # not shipped yet
        group.tracking_ref = "D-1"
        group.action_ship()
        response = self.call("POST", url, self.wkey, {})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(group.state, "delivered")
        self.assertEqual(group.delivered_reported_by, self.owner_user)
        # customer confirmation mode: the vendor only reports it
        self.param.set_param("leaders_marketplace.fbm_delivery_mode", "customer_confirm")
        order2 = self.a_order(1)
        group2 = order2.fulfillment_group_ids
        group2.tracking_ref = "D-2"
        group2.action_ship()
        response = self.call("POST", "%s/orders/%s/delivered" % (BASE, order2.id), self.wkey, {})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(group2.state, "delivery_reported")
        self.assertEqual(response.json()["groups"][0]["state"], "delivery_reported")

    def test_orders_pagination(self):
        for _i in range(3):
            self.a_order(1)
        data = self.get(BASE + "/orders?limit=2").json()
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["total"], 3)
        self.assertIn("limit=2", data["next"])
        self.assertEqual(len(self.get(data["next"]).json()["items"]), 1)


@tagged("post_install", "-at_install")
class TestVendorApiReturnsAndFinance(VendorApiCommon):

    def _return(self, vendor_order=None):
        vendor_order = vendor_order or self.a_order(2)
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "R-1"
        group.action_ship()
        return self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Damaged",
            "line_ids": [Command.create({
                "vendor_line_id": vendor_order.line_ids[:1].id, "quantity": 1})]})

    def test_list_get_and_scoping(self):
        record = self._return()
        data = self.get(BASE + "/returns").json()
        self.assertEqual([r["id"] for r in data["items"]], [record.id])
        self.assertEqual(data["items"][0]["reason"], "Damaged")
        self.assertEqual(self.get("%s/returns/%s" % (BASE, record.id)).json()["state"], "requested")
        self.assertError(self.call("GET", "%s/returns/%s" % (BASE, record.id), self.bkey), 404, "not_found")
        self.assertError(self.call("POST", "%s/returns/%s/decision" % (BASE, record.id), self.bkey,
                                   {"decision": "approve"}), 404, "not_found")
        self.assertEqual(self.call("GET", BASE + "/returns", self.bkey).json()["total"], 0)
        self.assertError(self.get(BASE + "/returns?state=bogus"), 422, "validation_error")

    def test_decisions(self):
        record = self._return()
        url = "%s/returns/%s/decision" % (BASE, record.id)
        for body in ({}, {"decision": "maybe"}, {"decision": "approve", "note": 5}, {"decision": "approve", "x": 1}):
            self.assertError(self.call("POST", url, self.wkey, body), 422, "validation_error")
        self.assertError(self.call("POST", url, self.wkey, {"decision": "reject"}), 422, "unprocessable")
        self.assertEqual(record.state, "requested")
        response = self.call("POST", url, self.wkey, {"decision": "reject", "note": "Used item"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.vendor_note, "Used item")
        self.assertError(self.call("POST", url, self.wkey, {"decision": "approve"}), 422, "unprocessable")
        record2 = self._return()
        response = self.call("POST", "%s/returns/%s/decision" % (BASE, record2.id), self.wkey,
                             {"decision": "approve", "note": "ok"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(record2.state, "approved")
        self.assertEqual(record2.approved_by, "vendor")

    def test_wallet_and_settlements(self):
        Tx = self.env["leaders.wallet.transaction"]
        currency = self.company.currency_id
        for vendor, amount in ((self.vendor, 25.0), (self.vendor_b, 999.0)):
            Tx.create({"vendor_id": vendor.id, "type": "adjustment", "amount": amount,
                       "currency_id": currency.id, "state": "available",
                       "available_date": fields.Date.today(), "reference": "API-%s" % vendor.id})
        data = self.get(BASE + "/wallet").json()
        self.assertEqual(data["balances"]["available"], self.vendor.vendor_wallet_balance)
        self.assertIn("hold", data["balances"])
        self.assertEqual(data["balances"]["currency"], currency.name)
        refs = [t["reference"] for t in data["items"]]
        self.assertIn("API-%s" % self.vendor.id, refs)
        self.assertNotIn("API-%s" % self.vendor_b.id, refs)
        self.assertEqual(self.get(BASE + "/wallet?state=hold").json()["total"], 0)
        self.assertError(self.get(BASE + "/wallet?type=bogus"), 422, "validation_error")
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": "2026-01-01", "period_to": "2026-01-31"})
        other = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor_b.id, "period_from": "2026-01-01", "period_to": "2026-01-31"})
        items = self.get(BASE + "/settlements").json()["items"]
        self.assertEqual([s["id"] for s in items], [settlement.id])
        self.assertNotIn(other.id, [s["id"] for s in items])
        self.assertEqual(items[0]["period_from"], "2026-01-01")


@tagged("post_install", "-at_install")
class TestVendorApiRateAndIdempotency(VendorApiCommon):

    def test_rate_limit_429_with_retry_after(self):
        self.param.set_param(RATE, "2")
        fixed = patch.object(type(self.Key), "_leaders_now", lambda self: 1_800_000_030.0)
        with fixed:
            self.assertEqual(self.get(BASE + "/offers").status_code, 200)
            second = self.get(BASE + "/offers")
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.headers["X-RateLimit-Remaining"], "0")
            third = self.get(BASE + "/offers")
            self.assertError(third, 429, "rate_limited")
            self.assertEqual(third.headers["Retry-After"], "31")
            # another key has its own counter
            self.assertEqual(self.call("GET", BASE + "/offers", self.bkey).status_code, 200)
        with patch.object(type(self.Key), "_leaders_now", lambda self: 1_800_000_100.0):
            self.assertEqual(self.get(BASE + "/offers").status_code, 200)  # next minute
        self.param.set_param(RATE, "0")
        for _i in range(5):
            self.assertEqual(self.get(BASE + "/offers").status_code, 200)  # 0 = off

    def test_idempotency_replay_and_conflict(self):
        url = "%s/offers/%s" % (BASE, self.fbm_offer.id)
        headers = {"Idempotency-Key": "abc-123"}
        first = self.call("PATCH", url, self.wkey, {"price": 130}, headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertNotIn("Idempotent-Replay", first.headers)
        self.fbm_offer.state = "active"  # would go back to draft if the request ran again
        replay = self.call("PATCH", url, self.wkey, {"price": 130}, headers)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.headers.get("Idempotent-Replay"), "true")
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(self.fbm_offer.state, "active")
        conflict = self.call("PATCH", url, self.wkey, {"price": 131}, headers)
        self.assertError(conflict, 422, "idempotency_key_conflict")
        # same key on another path is a different request
        other = self.call("PATCH", "%s/offers/%s" % (BASE, self.fbl_offer.id), self.wkey, {"price": 130}, headers)
        self.assertError(other, 422, "idempotency_key_conflict")
        # keys are per API key: vendor B may use the same value
        self.assertEqual(self.call("PATCH", "%s/offers/%s" % (BASE, self.b_offer.id), self.bkey,
                                   {"price": 5}, headers).status_code, 200)
        self.assertError(self.call("PATCH", url, self.wkey, {"price": 1}, {"Idempotency-Key": ""}),
                         400, "invalid_idempotency_key")

    def test_failed_requests_are_not_recorded(self):
        url = "%s/offers/%s" % (BASE, self.fbl_offer.id)
        headers = {"Idempotency-Key": "retry-me"}
        self.assertError(self.call("PATCH", url, self.wkey, {"stock": 3}, headers), 422, "unprocessable")
        self.assertEqual(self.call("PATCH", url, self.wkey, {"price": 41}, headers).status_code, 200)
        self.assertEqual(self.fbl_offer.price, 41.0)

    def test_idempotent_ship_does_not_ship_twice(self):
        order = self.a_order(2)
        group = order.fulfillment_group_ids
        url = "%s/orders/%s/ship" % (BASE, order.id)
        headers = {"Idempotency-Key": "ship-1"}
        body = {"tracking_ref": "ID-1"}
        self.assertEqual(self.call("POST", url, self.wkey, body, headers).status_code, 200)
        replay = self.call("POST", url, self.wkey, body, headers)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.headers.get("Idempotent-Replay"), "true")
        self.assertEqual(len(group.picking_ids), 1)

    def test_cleanup_cron(self):
        rec = self.env["leaders.vendor.api.idempotency"].create({
            "key_id": self.wkey_rec.id, "idempotency_key": "old", "request_hash": "h", "status": 200})
        self.env.cr.execute(
            "UPDATE leaders_vendor_api_idempotency SET create_date = now() - interval '25 hours' WHERE id = %s",
            [rec.id])
        fresh = self.env["leaders.vendor.api.idempotency"].create({
            "key_id": self.wkey_rec.id, "idempotency_key": "new", "request_hash": "h", "status": 200})
        self.Key._cron_cleanup()
        self.assertFalse(rec.exists())
        self.assertTrue(fresh.exists())


@tagged("post_install", "-at_install")
class TestVendorApiOpenApi(VendorApiCommon):

    def test_document_lists_every_route(self):
        response = self.call("GET", "/api/v1/openapi.json")
        self.assertEqual(response.status_code, 200)
        doc = response.json()  # valid JSON
        self.assertTrue(doc["openapi"].startswith("3."))
        self.assertIn("bearerAuth", doc["components"]["securitySchemes"])
        self.assertIn("Error", doc["components"]["schemas"])
        # every registered vendor route is documented with its methods
        documented = {(path, m.upper()) for path, item in doc["paths"].items() for m in item}
        registered = set()
        for rule in self.env["ir.http"].routing_map().iter_rules():
            if not rule.rule.startswith(BASE + "/") or rule.rule.endswith("/dashboard"):
                continue
            path = re.sub(r"<(?:int:)?(\w+)>", r"{\1}", rule.rule)
            for method in rule.methods - {"HEAD", "OPTIONS"}:
                registered.add((path, method))
        registered.add(("/api/v1/openapi.json", "GET"))
        self.assertEqual(documented, registered)
        self.assertGreaterEqual(len(documented), 13)
        # every $ref resolves
        refs = set(re.findall(r'"\$ref": "#/components/schemas/(\w+)"', response.text))
        self.assertTrue(refs)
        self.assertLessEqual(refs, set(doc["components"]["schemas"]))
        # write operations declare Idempotency-Key, all vendor ones a bearer requirement
        patch_op = doc["paths"]["/api/v1/vendor/offers/{offer_id}"]["patch"]
        self.assertIn("Idempotency-Key", [p["name"] for p in patch_op["parameters"]])
        self.assertEqual(patch_op["security"], [{"bearerAuth": []}])
        self.assertIn("429", patch_op["responses"])


@tagged("post_install", "-at_install")
class TestVendorApiKeyPage(VendorApiCommon):

    def setUp(self):
        super().setUp()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)

    def test_page_and_nav_for_the_owner(self):
        page = self.url_open("/my/vendor/api")
        self.assertEqual(page.status_code, 200)
        self.assertIn("curl", page.text)
        self.assertIn("/api/v1/openapi.json", page.text)
        self.assertIn("lmk_%s" % self.wkey_rec.key_prefix, page.text)
        self.assertNotIn(self.wkey, page.text)
        self.assertIn('href="/my/vendor/api"', self.url_open("/my/vendor/products").text)

    def test_create_shows_plaintext_once_and_stores_a_hash(self):
        response = self._post("/my/vendor/api/keys", "/my/vendor/api", name="Browser key", scope="write")
        plain = re.search(r'id="lm_api_new_key_value"[^>]*value="(lmk_[^"]+)"', response.text)
        self.assertTrue(plain, response.text[:2000])
        plain = plain.group(1)
        rec = self.Key.search([("name", "=", "Browser key")])
        self.assertEqual(rec.vendor_id, self.vendor)
        self.assertEqual(rec.user_id, self.owner_user)
        self.assertEqual(rec.scope, "write")
        self.assertNotEqual(rec.key_hash, plain)
        self.assertNotIn(plain, response.text.replace(plain, "", 1))  # only shown once in the page
        again = self.url_open("/my/vendor/api")
        self.assertNotIn(plain, again.text)
        self.assertIn("Browser key", again.text)
        # the key works
        self.assertEqual(self.call("GET", BASE + "/offers", plain).status_code, 200)
        # revoke
        self._post("/my/vendor/api/keys/%s/revoke" % rec.id, "/my/vendor/api")
        self.assertTrue(rec.revoked_date)
        self.assertError(self.call("GET", BASE + "/offers", plain), 401, "unauthorized")
        self.assertIn("Revoked", self.url_open("/my/vendor/api").text)

    def test_create_validation_and_csrf(self):
        page = self._post("/my/vendor/api/keys", "/my/vendor/api", name="   ", scope="write")
        self.assertIn("Please give the key a name", page.text)
        self.assertFalse(self.Key.search([("name", "=", "   ")]))
        before = self.Key.search_count([])
        response = self.url_open("/my/vendor/api/keys", data={"name": "no csrf", "scope": "read"})
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(self.Key.search_count([]), before)
        page = self._post("/my/vendor/api/keys", "/my/vendor/api", name="Odd scope", scope="root")
        self.assertIn("Unknown key scope", page.text)

    def test_cannot_revoke_another_vendors_key(self):
        token = self._csrf("/my/vendor/api")
        response = self.url_open("/my/vendor/api/keys/%s/revoke" % self.bkey_rec.id, data={"csrf_token": token})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.bkey_rec.revoked_date)

    def test_switch_off_is_404_and_link_hidden(self):
        self.param.set_param(ENABLED, "False")
        self.assertEqual(self.url_open("/my/vendor/api").status_code, 404)
        token = self._csrf("/my/vendor/products")
        self.assertEqual(self.url_open("/my/vendor/api/keys", data={
            "csrf_token": token, "name": "x", "scope": "read"}).status_code, 404)
        self.assertNotIn('href="/my/vendor/api"', self.url_open("/my/vendor/products").text)

    def test_staff_and_anonymous_are_refused(self):
        self.authenticate(self.staff_user.login, PORTAL_PASSWORD)
        self.assertEqual(self.url_open("/my/vendor/api").status_code, 403)
        self.assertNotIn('href="/my/vendor/api"', self.url_open("/my/vendor/products").text)
        self.authenticate(None, None)
        response = self.url_open("/my/vendor/api", allow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
