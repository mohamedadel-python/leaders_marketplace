"""F5: company-only checkouts in the unified "My orders" page (cancel + contact support)."""
from datetime import timedelta

from lxml import html as lxml_html

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import PORTAL_PASSWORD
from .test_unified_orders import PARAM, UnifiedOrdersCommon

CANCEL_PARAM = "leaders_marketplace.customer_can_cancel_orders"


@tagged("post_install", "-at_install")
class TestUnifiedOrdersCompanyActions(HttpCase, UnifiedOrdersCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        other = cls.env["res.partner"].create({"name": "F5 Other Buyer", "email": "lm.f5.other@example.com"})
        cls.other_cust_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "F5 Other Buyer", "login": "lm.f5.other@example.com", "email": "lm.f5.other@example.com",
            "password": PORTAL_PASSWORD, "partner_id": other.id,
            "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
        })

    def _company_order(self, cod=False, pay=False):
        order = self._sale([(self.own_product, 1)], cod=cod)
        if pay:
            self._pay_online(order)
        else:
            order.action_confirm()
        self.assertFalse(order.vendor_order_ids)
        self.assertEqual(order.state, "sale")
        return order

    def _url(self, order, tail=""):
        return "/my/marketplace/checkouts/%s%s" % (order.id, tail)

    def _login(self):
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)

    def _cancel(self, order, **data):
        data.setdefault("reason_id", self.reason.id)
        return self._post(self._url(order, "/cancel"), self._url(order), **data)

    def _receivable_residual(self):
        lines = self.env["account.move.line"].search([
            ("partner_id", "=", self.customer.commercial_partner_id.id),
            ("account_id.account_type", "=", "asset_receivable"),
            ("parent_state", "=", "posted"), ("reconciled", "=", False)])
        return self.company.currency_id.round(sum(lines.mapped("amount_residual")))

    # -- model ---------------------------------------------------------------
    def test_online_paid_prepares_drafts_and_releases_pickings(self):
        self._configure_accounts()
        order = self._company_order(pay=True)
        pickings = order.picking_ids
        self.assertTrue(pickings)
        order.action_customer_cancel_checkout(self.reason.id, "no longer needed")
        self.assertEqual(order.state, "cancel")
        self.assertEqual(order.leaders_cancelled_by_type, "customer")
        self.assertEqual(order.leaders_cancel_reason_id, self.reason)
        self.assertEqual(order.leaders_cancel_note, "no longer needed")
        self.assertTrue(order.leaders_cancel_date)
        self.assertTrue(all(p.state == "cancel" for p in pickings))
        self.assertFalse(pickings.move_ids.move_line_ids.filtered(lambda l: l.quantity))
        credit_note = self.env["account.move"].search([
            ("reversed_entry_id", "in", order.invoice_ids.ids)])
        self.assertTrue(credit_note)
        self.assertEqual(credit_note.state, "draft")
        refund = order.leaders_refund_payment_id
        self.assertTrue(refund)
        self.assertEqual(refund.state, "draft")
        self.assertEqual(refund.payment_type, "outbound")
        self.assertEqual(refund.amount, order.amount_total)
        credit_note.action_post()
        refund.action_post()
        self.assertEqual(self._receivable_residual(), 0.0)

    def test_stock_reservation_is_released(self):
        order = self._company_order()
        Quant = self.env["stock.quant"]
        reserved = sum(Quant.search([("product_id", "=", self.own_product.id)]).mapped("reserved_quantity"))
        self.assertGreater(reserved, 0.0)
        order.action_customer_cancel_checkout(self.reason.id)
        after = sum(Quant.search([("product_id", "=", self.own_product.id)]).mapped("reserved_quantity"))
        self.assertEqual(after, reserved - 1.0)

    def test_cod_has_nothing_to_refund(self):
        order = self._company_order(cod=True)
        order.action_customer_cancel_checkout(self.reason.id)
        self.assertEqual(order.state, "cancel")
        self.assertFalse(order.leaders_refund_payment_id)
        self.assertFalse(order.invoice_ids.filtered(lambda m: m.move_type == "out_refund"))
        self.assertEqual(order.leaders_cancelled_by_type, "customer")

    def test_unpaid_confirmed_order_cancels_without_refund(self):
        order = self._company_order()
        order.action_customer_cancel_checkout(self.reason.id)
        self.assertEqual(order.state, "cancel")
        self.assertFalse(order.leaders_refund_payment_id)

    def test_refusals_change_nothing(self):
        order = self._company_order()
        with self.assertRaises(UserError):
            order.action_customer_cancel_checkout(False)
        with self.assertRaises(UserError):
            order.action_customer_cancel_checkout(999999)
        self.assertEqual(order.state, "sale")
        self._deliver_company(order)
        self.assertEqual(order._leaders_customer_cancel_all_status(), "shipped")
        with self.assertRaises(UserError):
            order.action_customer_cancel_checkout(self.reason.id)
        self.assertEqual(order.state, "sale")

    def test_window(self):
        order = self._company_order()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.cancel_window_hours", "2")
        order.date_order = fields.Datetime.now() - timedelta(hours=3)
        self.assertEqual(order._leaders_customer_cancel_all_status(), "window")
        with self.assertRaises(UserError):
            order.action_customer_cancel_checkout(self.reason.id)
        order.date_order = fields.Datetime.now() - timedelta(hours=1)
        order.action_customer_cancel_checkout(self.reason.id)
        self.assertEqual(order.state, "cancel")

    def test_switch_off_refuses(self):
        order = self._company_order()
        self.env["ir.config_parameter"].sudo().set_param(CANCEL_PARAM, False)
        with self.assertRaises(UserError):
            order.action_customer_cancel_checkout(self.reason.id)
        self.assertEqual(order.state, "sale")

    def test_with_vendor_orders_it_is_the_same_as_scope_all(self):
        self._configure_accounts()
        order = self._sale([(self.own_product, 1), (self.fbm_product, 1)])
        self._pay_online(order)
        order.action_customer_cancel_checkout(self.reason.id, "x")
        self.assertEqual(order.state, "cancel")
        self.assertEqual(order.vendor_order_ids.state, "cancelled")
        self.assertEqual(order.vendor_order_ids.cancelled_by_type, "customer")
        self.assertEqual(order.leaders_cancelled_by_type, "customer")
        self.assertTrue(order.leaders_refund_payment_id)
        self.assertTrue(order.vendor_order_ids.refund_payment_id)

    def test_emails_follow_the_settings(self):
        order = self._company_order()
        since = self._last_mail()
        order.action_customer_cancel_checkout(self.reason.id)
        mails = self._mails(since)
        self.assertEqual(len(mails), 1)
        self.assertIn(self.customer.email, "%s %s" % (mails.email_to, " ".join(mails.recipient_ids.mapped("email"))))
        order2 = self._company_order()
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.notify_customer_order_cancelled_by_customer", "False")
        since = self._last_mail()
        order2.action_customer_cancel_checkout(self.reason.id)
        self.assertFalse(self._mails(since))

    # -- portal --------------------------------------------------------------
    def test_route_cancels_and_page_shows_the_result(self):
        self._configure_accounts()
        order = self._company_order(pay=True)
        self._login()
        response = self._cancel(order, note="oops")
        self.assertIn("Your order was cancelled", response.text)
        self.assertEqual(order.state, "cancel")
        page = self.url_open(self._url(order))
        self.assertIn("You cancelled this order", page.text)
        self.assertIn("Your refund is being processed", page.text)
        self.assertNotIn("lm_checkout_cancel_form", page.text)
        dom = self._dom_of(order)
        self.assertEqual(dom.xpath("string(//div[@id='lm_customer_checkout']//span[@data-state])").strip(), "Cancelled")
        listing = self._dom("/my/marketplace/checkouts")
        self.assertIn(str(order.id), listing.xpath("//tbody/tr/@data-checkout"))
        self.assertTrue(listing.xpath("//tr[@data-checkout='%s']//span[@data-state='cancelled']" % order.id))

    def test_cod_page_says_nothing_was_charged(self):
        order = self._company_order(cod=True)
        self._login()
        self._cancel(order)
        page = self.url_open(self._url(order))
        self.assertIn("nothing was charged", page.text)
        self.assertNotIn("Your refund is being processed", page.text)

    def _dom(self, path, code=200):
        response = self.url_open(path)
        self.assertEqual(response.status_code, code, path)
        return lxml_html.fromstring(response.content)

    def _dom_of(self, order):
        return self._dom(self._url(order))

    def test_route_without_reason_shows_an_error(self):
        order = self._company_order()
        self._login()
        response = self._post(self._url(order, "/cancel"), self._url(order))
        self.assertIn("Please choose a reason", response.text)
        self.assertEqual(order.state, "sale")

    def test_route_refused_after_delivery_and_window(self):
        order = self._company_order()
        self._deliver_company(order)
        self._login()
        response = self._cancel(order)
        self.assertIn("already shipped", response.text)
        self.assertEqual(order.state, "sale")
        order2 = self._company_order()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.cancel_window_hours", "1")
        order2.date_order = fields.Datetime.now() - timedelta(hours=5)
        response = self._cancel(order2)
        self.assertIn("The time to cancel this order has passed", response.text)
        self.assertEqual(order2.state, "sale")

    def test_route_ownership_and_unknown(self):
        order = self._company_order()
        self.authenticate(self.other_cust_user.login, PORTAL_PASSWORD)
        token = self._csrf("/my/account")
        response = self.url_open(self._url(order, "/cancel"), data={
            "csrf_token": token, "reason_id": self.reason.id})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(order.state, "sale")
        response = self.url_open("/my/marketplace/checkouts/99999999/cancel", data={
            "csrf_token": token, "reason_id": self.reason.id})
        self.assertEqual(response.status_code, 404)
        draft = self._sale([(self.own_product, 1)])
        self._login()
        token = self._csrf("/my/account")
        response = self.url_open(self._url(draft, "/cancel"), data={
            "csrf_token": token, "reason_id": self.reason.id})
        self.assertEqual(response.status_code, 404)
        response = self.url_open(self._url(order, "/cancel"), data={"reason_id": self.reason.id})
        self.assertEqual(response.status_code, 400)

    def test_route_404_when_either_switch_is_off(self):
        order = self._company_order()
        self._login()
        token = self._csrf(self._url(order))
        set_param = self.env["ir.config_parameter"].sudo().set_param
        for key in (CANCEL_PARAM, PARAM):
            set_param(key, False)
            self.env.cr.flush()
            response = self.url_open(self._url(order, "/cancel"), data={
                "csrf_token": token, "reason_id": self.reason.id})
            self.assertEqual(response.status_code, 404, key)
            self.assertEqual(order.state, "sale")
            set_param(key, "True")

    def test_route_with_vendor_orders_cancels_them_too(self):
        order = self._sale([(self.own_product, 1), (self.fbm_product, 1)])
        order.action_confirm()
        self._login()
        self._cancel(order)
        self.assertEqual(order.state, "cancel")
        self.assertEqual(order.vendor_order_ids.state, "cancelled")
        self.assertEqual(order.vendor_order_ids.cancelled_by_type, "customer")

    def test_detail_form_only_when_allowed(self):
        order = self._company_order()
        self._login()
        page = self.url_open(self._url(order))
        self.assertIn("lm_checkout_cancel_form", page.text)
        self.assertIn(self.reason.name, page.text)
        self.assertIn("data-lm-confirm-ref", page.text)
        self.assertIn("data-lm-loading", page.text)
        self.assertNotIn("Contact support", page.text)
        self.env["ir.config_parameter"].sudo().set_param(CANCEL_PARAM, False)
        page = self.url_open(self._url(order))
        self.assertNotIn("lm_checkout_cancel_form", page.text)
        self.env["ir.config_parameter"].sudo().set_param(CANCEL_PARAM, "True")
        self._deliver_company(order)
        page = self.url_open(self._url(order))
        self.assertNotIn("lm_checkout_cancel_form", page.text)
        self.assertIn("Request a return after delivery", page.text)
        self.assertIn("lm_checkout_contact_support", page.text)
        self.assertIn("/contactus?subject=%s" % order.name.replace("/", "%2F"), page.text)

    def test_detail_window_notice(self):
        order = self._company_order()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.cancel_window_hours", "1")
        order.date_order = fields.Datetime.now() - timedelta(hours=4)
        self._login()
        page = self.url_open(self._url(order))
        self.assertIn("lm_checkout_cancel_window", page.text)
        self.assertNotIn("lm_checkout_cancel_form", page.text)

    def test_detail_in_arabic(self):
        self.env["res.lang"]._activate_lang("ar_001")
        self.customer_user.lang = "ar_001"
        order = self._company_order()
        self._login()
        page = self.url_open(self._url(order))
        self.assertEqual(page.status_code, 200)
        self.assertIn("lm_checkout_cancel_form", page.text)
        self._deliver_company(order)
        page = self.url_open(self._url(order))
        self.assertIn("lm_checkout_contact_support", page.text)
        self.assertNotIn("lm_checkout_cancel_form", page.text)
