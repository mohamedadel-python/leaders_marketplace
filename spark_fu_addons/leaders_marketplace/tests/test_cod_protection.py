"""Cash-on-delivery protection (TASK 29 / A3)."""
import re
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_e2e_flows import E2ECommon
from .test_security_rpc import PORTAL_PASSWORD

OTP_SEND = "odoo.addons.leaders_marketplace.models.cod_protection.CodOtp._leaders_send_otp"


class CodProtectionCommon(E2ECommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.set_param = cls.env["ir.config_parameter"].sudo().set_param
        cls.customer.email = "cod.buyer@example.com"

    def _cod_shipped(self, product, qty=2, tag="ref"):
        order = self._order([(product, qty)], cod=True)
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "REF-%s-%s" % (tag, group.id)
        group.action_ship()
        self.assertEqual(group.state, "shipped")
        return vendor_order

    def _stock(self, product, location, vendor):
        quants = self.env["stock.quant"].sudo().search([
            ("product_id", "=", product.id), ("location_id", "=", location.id),
            ("owner_id", "=", vendor.id)])
        return sum(quants.mapped("quantity"))

    def _earnings(self, vendor_order):
        return self.Wallet.search([("vendor_order_id", "=", vendor_order.id)])


@tagged("post_install", "-at_install")
class TestCodMaxAmount(CodProtectionCommon):

    def test_no_limit_by_default(self):
        order = self._order([(self.fbm_product, 5)])
        self.assertTrue(order._leaders_cod_availability()[0])

    def test_above_limit_is_refused_with_a_message(self):
        self.set_param("leaders_marketplace.cod_max_amount", "300")
        big = self._order([(self.fbm_product, 5)])
        allowed, message = big._leaders_cod_availability()
        self.assertFalse(allowed)
        self.assertIn("above", message)
        with self.assertRaises(UserError):
            big._leaders_cod_check_allowed()
        small = self._order([(self.fbm_product, 1)])
        self.assertTrue(small._leaders_cod_check_allowed())

    def test_exactly_the_limit_is_allowed(self):
        order = self._order([(self.fbm_product, 2)])
        self.set_param("leaders_marketplace.cod_max_amount", str(order.amount_total))
        self.assertTrue(order._leaders_cod_availability()[0])


@tagged("post_install", "-at_install")
class TestCodOtp(CodProtectionCommon):

    def setUp(self):
        super().setUp()
        self.set_param("leaders_marketplace.cod_require_otp", "True")
        self.order = self._order([(self.fbm_product, 1)])
        self.Otp = self.env["leaders.cod.otp"]
        self.sent = []

    def _send(self, phone="+201001234567"):
        def fake(this, phone_, code, partner=None):
            self.sent.append(code)
            return True
        with patch(OTP_SEND, fake):
            return self.Otp.leaders_send_code(self.order, phone)

    def test_off_by_default(self):
        self.set_param("leaders_marketplace.cod_require_otp", False)
        self.assertTrue(self.order._leaders_cod_check_allowed())

    def test_required_until_verified(self):
        with self.assertRaises(UserError):
            self.order._leaders_cod_check_allowed()
        self._send()
        self.assertTrue(self.Otp.leaders_verify_code(self.order, "+201001234567", self.sent[-1]))
        self.assertTrue(self.order.leaders_cod_phone_verified)
        self.assertTrue(self.order._leaders_cod_check_allowed())

    def test_code_is_six_digits_and_stored_hashed(self):
        record = self._send()
        code = self.sent[-1]
        self.assertRegex(code, r"^\d{6}$")
        for name in ("code_hash", "salt", "phone"):
            self.assertNotIn(code, record[name] or "")
        self.env.cr.execute("SELECT code_hash, salt FROM leaders_cod_otp WHERE id = %s", [record.id])
        self.assertNotIn(code, " ".join(self.env.cr.fetchone()))
        self.assertEqual(len(record.code_hash), 64)
        self.assertEqual(
            (record.expires_at - record.create_date).total_seconds() // 60, 5)

    def test_wrong_code_counts_attempts_then_locks(self):
        self._send()
        good = self.sent[-1]
        bad = "000000" if good != "000000" else "111111"
        for _i in range(5):
            self.assertFalse(self.Otp.leaders_verify_code(self.order, "+201001234567", bad))
        with self.assertRaises(UserError):
            self.Otp.leaders_verify_code(self.order, "+201001234567", good)
        self.assertFalse(self.order.leaders_cod_phone_verified)

    def test_expired_code_is_refused(self):
        record = self._send()
        record.expires_at = fields.Datetime.subtract(fields.Datetime.now(), seconds=1)
        with self.assertRaises(UserError):
            self.Otp.leaders_verify_code(self.order, "+201001234567", self.sent[-1])

    def test_code_is_bound_to_order_and_phone(self):
        self._send()
        other = self._order([(self.fbm_product, 1)])
        with self.assertRaises(UserError):
            self.Otp.leaders_verify_code(other, "+201001234567", self.sent[-1])
        with self.assertRaises(UserError):
            self.Otp.leaders_verify_code(self.order, "+201009999999", self.sent[-1])

    def test_resend_is_throttled(self):
        self._send()
        with self.assertRaises(UserError):
            self._send()

    def test_bad_phone_is_refused(self):
        with self.assertRaises(UserError):
            self._send(phone="abc")

    def _fallback_check(self):
        before = self.env["mail.mail"].sudo().search_count([("email_to", "=", "cod.buyer@example.com")])
        with self.assertLogs("odoo.addons.leaders_marketplace.models.cod_protection", "INFO") as logs:
            ok = self.Otp._leaders_send_otp("+201001234567", "123456", partner=self.customer)
        self.assertTrue(ok)
        self.assertTrue(any("123456" in line for line in logs.output))
        after = self.env["mail.mail"].sudo().search_count([("email_to", "=", "cod.buyer@example.com")])
        self.assertEqual(after, before + 1)
        # No email address and no gateway: nothing could be delivered.
        with self.assertLogs("odoo.addons.leaders_marketplace.models.cod_protection", "INFO"):
            self.assertFalse(self.Otp._leaders_send_otp("+201001234567", "123456", partner=None))

    def test_fallback_logs_and_emails_when_the_sms_gateway_fails(self):
        if "sms.sms" not in self.env:
            return self._fallback_check()  # no sms module at all: the fallback is the only path

        def failing(records, *args, **kwargs):
            records.write({"state": "error"})
        with patch("odoo.addons.sms.models.sms_sms.SmsSms.send", failing):
            self._fallback_check()

    def test_sms_module_is_used_when_installed(self):
        if "sms.sms" not in self.env:
            self.assertNotIn("sms.sms", self.env)  # fallback covered by the other test
            return
        calls = []

        def sent(records, *args, **kwargs):
            calls.append(records.mapped("number"))
            records.write({"state": "sent"})
        with patch("odoo.addons.sms.models.sms_sms.SmsSms.send", sent):
            self.assertTrue(self.Otp._leaders_send_otp("+201001234567", "123456", partner=None))
        self.assertEqual(calls, [["+201001234567"]])


@tagged("post_install", "-at_install")
class TestCodRefusal(CodProtectionCommon):

    def test_fbm_refusal_returns_goods_and_creates_no_money(self):
        location = self.vendor_a.vendor_location_id
        before = self._stock(self.fbm_product, location, self.vendor_a)
        vendor_order = self._cod_shipped(self.fbm_product, 2, "fbm")
        group = vendor_order.fulfillment_group_ids
        self.assertEqual(self._stock(self.fbm_product, location, self.vendor_a), before - 2)

        group.action_customer_refused(reason="Not at home", by=self.owner_user)

        self.assertEqual(group.state, "cancelled")
        self.assertTrue(group.refused_at_delivery)
        self.assertEqual(vendor_order.state, "cancelled")
        self.assertEqual(vendor_order.cancelled_by_type, "customer")
        self.assertEqual(vendor_order.cancel_reason_id,
                         self.env.ref("leaders_marketplace.cancel_reason_refused_at_delivery"))
        self.assertIn("Not at home", vendor_order.cancel_reason)
        picking = group.refusal_picking_ids
        self.assertEqual(picking.state, "done")
        self.assertEqual(picking.location_dest_id, group.source_location_id)
        self.assertEqual(picking.owner_id, self.vendor_a)
        self.assertEqual(self._stock(self.fbm_product, location, self.vendor_a), before)
        self.assertFalse(self._earnings(vendor_order))
        self.assertFalse(vendor_order.refund_payment_id)
        self.assertFalse(vendor_order.earning_move_id)
        self.assertEqual(self.customer.commercial_partner_id.leaders_cod_refusal_count, 1)
        self._assert_settled(self.vendor_a)
        self.assertEqual(self._receivable_balance(), 0.0)

    def test_fbl_refusal_leaves_a_receipt_for_the_warehouse_and_reverses_collection(self):
        location = self.vendor_b.vendor_fbl_location_id
        before = self._stock(self.b_product, location, self.vendor_b)
        vendor_order = self._cod_shipped(self.b_product, 1, "fbl")
        group = vendor_order.fulfillment_group_ids
        # A collection entry existing at this point (booked by hand) must be reversed.
        self.Accounting.post_collection_entry(vendor_order)
        self.assertTrue(vendor_order.collection_move_id)

        group.action_customer_refused(reason=None)

        picking = group.refusal_picking_ids
        self.assertTrue(picking)
        self.assertNotEqual(picking.state, "done", "the warehouse validates it when the parcel is back")
        self.assertEqual(picking.location_dest_id, location)
        self.assertEqual(picking.owner_id, self.vendor_b)
        self.assertEqual(self._stock(self.b_product, location, self.vendor_b), before - 1)
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.move_ids.picked = True
        picking.with_context(skip_backorder=True, skip_sms=True)._action_done()
        self.assertEqual(self._stock(self.b_product, location, self.vendor_b), before)
        self.assertEqual(vendor_order.state, "cancelled")
        self.assertTrue(vendor_order.collection_reversal_move_id)
        self.assertFalse(self._earnings(vendor_order))
        self.assertFalse(vendor_order.refund_payment_id)
        self._assert_settled(self.vendor_b)
        self.assertEqual(self._receivable_balance(), 0.0)

    def test_only_shipped_cod_groups_can_be_refused(self):
        online = self._order([(self.fbm_product, 1)])
        self._pay(online)
        vo = online.vendor_order_ids
        vo.fulfillment_group_ids.tracking_ref = "ONL-1"
        vo.fulfillment_group_ids.action_ship()
        with self.assertRaises(UserError):
            vo.fulfillment_group_ids.action_customer_refused()
        cod = self._order([(self.fbm_product, 1)], cod=True)
        cod.action_confirm()
        with self.assertRaises(UserError):
            cod.vendor_order_ids.fulfillment_group_ids.action_customer_refused()  # not shipped
        delivered = self._cod_shipped(self.fbm_product, 1, "dlv")
        delivered.fulfillment_group_ids.action_report_delivery(reported_by=self.owner_user)
        with self.assertRaises(UserError):
            delivered.fulfillment_group_ids.action_customer_refused()
        self.assertEqual(self.customer.commercial_partner_id.leaders_cod_refusal_count, 0)

    def test_refusal_needs_the_back_office(self):
        vendor_order = self._cod_shipped(self.fbm_product, 1, "acl")
        with self.assertRaises(AccessError):
            vendor_order.fulfillment_group_ids.with_user(self.owner_user).action_customer_refused()

    def test_customer_cancellation_before_shipping_is_not_a_refusal(self):
        self.set_param("leaders_marketplace.customer_can_cancel_orders", "True")
        order = self._order([(self.fbm_product, 1)], cod=True)
        order.action_confirm()
        vo = order.vendor_order_ids
        vo.action_customer_cancel(
            self.env.ref("leaders_marketplace.cancel_reason_changed_mind").id, scope="vendor")
        self.assertEqual(vo.cancelled_by_type, "customer")
        self.assertEqual(self.customer.commercial_partner_id.leaders_cod_refusal_count, 0)

    def test_threshold_hides_cod_and_manager_reset_restores_it(self):
        commercial = self.customer.commercial_partner_id
        self.set_param("leaders_marketplace.cod_refusal_block_count", "2")
        for i in range(2):
            self.assertTrue(self._order([(self.fbm_product, 1)])._leaders_cod_availability()[0])
            self._cod_shipped(self.fbm_product, 1, "thr%s" % i).fulfillment_group_ids.action_customer_refused()
        self.assertEqual(commercial.leaders_cod_refusal_count, 2)
        order = self._order([(self.fbm_product, 1)])
        allowed, _message = order._leaders_cod_availability()
        self.assertFalse(allowed)
        with self.assertRaises(UserError):
            order._leaders_cod_check_allowed()
        with self.assertRaises(AccessError):
            commercial.with_user(self.owner_user).action_reset_cod_refusals()
        commercial.action_reset_cod_refusals()
        self.assertEqual(commercial.leaders_cod_refusal_count, 0)
        self.assertTrue(order._leaders_cod_availability()[0])

    def test_threshold_zero_never_blocks(self):
        commercial = self.customer.commercial_partner_id
        commercial.leaders_cod_refusal_count = 50
        self.assertTrue(self._order([(self.fbm_product, 1)])._leaders_cod_availability()[0])


@tagged("post_install", "-at_install")
class TestCodProtectionHttp(CodProtectionCommon):

    def test_vendor_portal_refused_route(self):
        vendor_order = self._cod_shipped(self.fbm_product, 1, "http")
        group = vendor_order.fulfillment_group_ids
        page_url = "/my/vendor/orders/%s" % vendor_order.id
        # Another vendor's owner cannot touch it.
        self.authenticate(self.owner_b.login, PORTAL_PASSWORD)
        token = self._csrf("/my/vendor/finance")
        response = self.url_open("/my/vendor/fulfillment/%s/refused" % group.id,
                                 data={"csrf_token": token})
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(group.state, "shipped")
        # The owner sees the button and can use it.
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self.assertIn("Customer refused", self.url_open(page_url).text)
        self._post("/my/vendor/fulfillment/%s/refused" % group.id, page_url, reason="Refused")
        self.assertEqual(vendor_order.state, "cancelled")
        self.assertTrue(group.refused_at_delivery)
        self.assertEqual(self.customer.commercial_partner_id.leaders_cod_refusal_count, 1)

    def test_vendor_portal_refused_route_rejects_fbl_groups(self):
        vendor_order = self._cod_shipped(self.b_product, 1, "httpfbl")
        group = vendor_order.fulfillment_group_ids
        self.authenticate(self.owner_b.login, PORTAL_PASSWORD)
        token = self._csrf("/my/vendor/finance")
        response = self.url_open("/my/vendor/fulfillment/%s/refused" % group.id,
                                 data={"csrf_token": token})
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(group.state, "shipped")

    def test_checkout_otp_routes(self):
        self.set_param("leaders_marketplace.cod_require_otp", "True")
        tmpl = self.fbm_product.product_tmpl_id
        tmpl.website_published = True
        page = self.url_open("/shop")
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text).group(1)
        self.url_open("/shop/cart/update", data={
            "csrf_token": token, "product_id": self.fbm_product.id, "add_qty": 1})
        order = self.env["sale.order"].sudo().search([("website_id", "!=", False)], order="id desc", limit=1)
        self.assertTrue(order.order_line)
        sent = []

        def fake(this, phone, code, partner=None):
            sent.append(code)
            return True
        with patch(OTP_SEND, fake):
            response = self.url_open("/shop/payment/cod/otp/send", data={
                "csrf_token": token, "phone": "+201001234567"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(sent), 1)
        otp = self.env["leaders.cod.otp"].sudo().search([("order_id", "=", order.id)])
        self.assertEqual(len(otp), 1)
        self.assertNotIn(sent[0], otp.code_hash)
        self.url_open("/shop/payment/cod/otp/verify", data={
            "csrf_token": token, "phone": "+201001234567", "code": "000000" if sent[0] != "000000" else "111111"})
        self.assertFalse(order.leaders_cod_phone_verified)
        self.assertEqual(otp.attempts, 1)
        self.url_open("/shop/payment/cod/otp/verify", data={
            "csrf_token": token, "phone": "+201001234567", "code": sent[0]})
        self.assertTrue(order.leaders_cod_phone_verified)
        # Without csrf the routes are refused.
        self.assertEqual(self.url_open("/shop/payment/cod/otp/send", data={"phone": "+201001234567"}).status_code, 400)
