"""Vendor stock correction and order rejection from the portal."""
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from . import test_cod_cash_and_refunds as refunds
from .test_cod_cash_and_refunds import CodRefundCommon
from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD


@tagged("post_install", "-at_install")
class TestVendorStockAndCancel(HttpCase, MarketplacePortalUserCommon, CodRefundCommon):

    _paid_online_order = refunds.TestCancellationRefund._paid_online_order

    def setUp(self):
        super().setUp()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)

    def _quants(self):
        return self.env["stock.quant"].sudo().search([
            ("product_id", "=", self.fbm_product.id),
            ("location_id", "=", self.fbm_offer.stock_location_id.id),
            ("owner_id", "=", self.vendor.id),
        ])

    def _set_cancel_enabled(self, value="True"):
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.vendor_can_cancel_orders", value)

    def _cancel(self, vendor_order, reason="Out of stock"):
        url = "/my/vendor/orders/%s" % vendor_order.id
        return self._post(url + "/cancel", url, reason=reason)

    # ------------------------------------------------------------------ stock
    def _ten_on_hand_two_reserved(self):
        self.env["stock.quant"]._leaders_adjust_vendor_stock(
            self.fbm_product, self.fbm_offer.stock_location_id, self.vendor, 5)  # 5 + 5 = 10
        self._order(self.fbm_product, 2, cod=False)
        quants = self._quants()
        self.assertEqual(sum(quants.mapped("quantity")), 10)
        self.assertEqual(sum(quants.mapped("reserved_quantity")), 2)

    def test_set_mode_corrects_stock_down(self):
        self._ten_on_hand_two_reserved()
        self._post("/my/vendor/products/%s/stock" % self.fbm_offer.id,
                   "/my/vendor/products", mode="set", quantity="3")
        self.assertEqual(sum(self._quants().mapped("quantity")), 3)
        self.assertEqual(self.fbm_offer.available_qty(), 1)

    def test_set_mode_refuses_going_below_reserved(self):
        self._ten_on_hand_two_reserved()
        response = self._post("/my/vendor/products/%s/stock" % self.fbm_offer.id,
                              "/my/vendor/products", mode="set", quantity="1")
        self.assertIn("already reserved", response.text)
        self.assertEqual(sum(self._quants().mapped("quantity")), 10)

    def test_default_mode_still_adds(self):
        self._post("/my/vendor/products/%s/stock" % self.fbm_offer.id,
                   "/my/vendor/products", quantity="4")
        self.assertEqual(sum(self._quants().mapped("quantity")), 9)

    def test_set_mode_recomputes_the_buy_box(self):
        Offer = type(self.env["leaders.marketplace.offer"])
        with patch.object(Offer, "_recompute_buybox_winner", autospec=True) as recompute:
            self._post("/my/vendor/products/%s/stock" % self.fbm_offer.id,
                       "/my/vendor/products", mode="set", quantity="2")
        recompute.assert_called()
        self.assertEqual(recompute.call_args.kwargs["product_ids"], self.fbm_product.ids)

    # ----------------------------------------------------------------- cancel
    def test_vendor_cancels_an_unshipped_paid_order(self):
        self._set_cancel_enabled()
        vendor_order = self._paid_online_order()
        self._cancel(vendor_order)
        self.assertEqual(vendor_order.state, "cancelled")
        self.assertEqual(vendor_order.cancel_reason, "Out of stock")
        self.assertEqual(vendor_order.cancelled_by, self.owner_user)
        self.assertEqual(vendor_order.refund_payment_id.state, "draft")
        self.assertTrue(any("Out of stock" in body for body in vendor_order.message_ids.mapped("body")))

    def test_cancel_needs_a_reason(self):
        self._set_cancel_enabled()
        vendor_order = self._order(self.fbm_product, 1)
        response = self._cancel(vendor_order, reason="   ")
        self.assertIn("reason", response.text)
        self.assertNotEqual(vendor_order.state, "cancelled")

    def test_cancel_after_shipping_is_refused(self):
        self._set_cancel_enabled()
        vendor_order = self._order(self.fbm_product, 1)
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "CXL-TRK"
        group.action_ship()
        state = vendor_order.state
        response = self._cancel(vendor_order)
        self.assertIn("already shipped", response.text)
        self.assertEqual(vendor_order.state, state)
        self.assertFalse(vendor_order.cancel_reason)

    def test_cancel_is_off_by_default(self):
        vendor_order = self._order(self.fbm_product, 1)
        url = "/my/vendor/orders/%s" % vendor_order.id
        response = self._post(url + "/cancel", url, reason="x")
        self.assertIn("cannot cancel", response.text)
        self.assertNotEqual(vendor_order.state, "cancelled")

    def test_customer_sees_the_reason(self):
        self._set_cancel_enabled()
        vendor_order = self._order(self.fbm_product, 1)
        vendor_order.sudo().partner_id = self.owner_user.partner_id.commercial_partner_id
        self._cancel(vendor_order, reason="Supplier delay")
        # The owner is now also the "customer" of this order, so the customer page opens.
        page = self.url_open("/my/marketplace/orders/%s" % vendor_order.id)
        self.assertIn("Supplier delay", page.text)
