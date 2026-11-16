"""Vendor-only checkouts: how the customer sees them, and reporting."""
import odoo
from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_vendor_split import MarketplaceVendorCommon

CUSTOMER_PASSWORD = "lm-customer-pass"


@tagged("post_install", "-at_install")
class TestVendorOnlyCheckout(HttpCase, MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer.email = "lm.customer@example.com"
        cls.customer_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "LM Customer", "login": "lm.customer@example.com",
            "email": "lm.customer@example.com", "password": CUSTOMER_PASSWORD,
            "partner_id": cls.customer.id,
            "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
        })
        cls.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.cod_enabled", "True")

    def _enable(self, value="True"):
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.vendor_only_presentation", value)

    def _checkout(self, products, cod=True):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": p.id, "product_uom_qty": 1}) for p in products],
        })
        order.message_subscribe(partner_ids=self.customer.ids)
        if cod:
            order.leaders_payment_method = "cod"
        order.with_context(send_email=True).action_confirm()
        return order

    def _mails(self, order):
        return self.env["mail.mail"].sudo().search([
            ("model", "=", "sale.order"), ("res_id", "=", order.id),
            ("subject", "ilike", "marketplace order")])

    def _login(self, last_order=None):
        session = self.authenticate(self.customer_user.login, CUSTOMER_PASSWORD)
        if last_order:
            session["sale_last_order_id"] = last_order.id
            odoo.http.root.session_store.save(session)

    def test_vendor_only_checkout_is_flagged(self):
        order = self._checkout([self.fbm_product])
        self.assertEqual(order.state, "cancel")
        self.assertTrue(order.marketplace_vendor_only)

    def test_vendor_only_cod_checkout(self):
        self._enable()
        order = self._checkout([self.fbm_product])
        self._login(order)

        self.assertNotIn(order.name, self.url_open("/my/orders").text)
        self.assertIn(order.vendor_order_ids.name, self.url_open("/my/marketplace/orders").text)

        page = self.url_open("/shop/confirmation").text
        self.assertIn("%.2f" % order._marketplace_checkout_total(), page)
        self.assertNotIn("Your cart is empty", page)
        self.assertNotIn('id="cart_total"', page)
        self.assertIn(order.vendor_order_ids.name, page)

        self.assertEqual(len(self._mails(order)), 1)

    def test_mixed_cart_stays_in_my_orders(self):
        self._enable()
        order = self._checkout([self.own_product, self.fbm_product])
        self.assertEqual(order.state, "sale")
        self.assertFalse(order.marketplace_vendor_only)
        self._login(order)
        self.assertIn(order.name, self.url_open("/my/orders").text)
        self.assertEqual(len(self._mails(order)), 1)

    def test_presentation_is_off_by_default(self):
        order = self._checkout([self.fbm_product])
        self.assertFalse(self._mails(order))

    def test_marketplace_sales_report_is_reachable(self):
        action = self.env.ref("leaders_marketplace.action_vendor_order_report")
        self.assertEqual(action.view_mode, "pivot,graph,list,form")
        self.assertTrue(self.env.ref("leaders_marketplace.menu_marketplace_sales_report").action)
        pivot = self.env["leaders.vendor.order"].get_view(
            self.env.ref("leaders_marketplace.view_vendor_order_pivot").id, "pivot")
        self.assertIn("vendor_net", pivot["arch"])
