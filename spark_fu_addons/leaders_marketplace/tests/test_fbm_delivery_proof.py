"""Proof of delivery for vendor-shipped (FBM) orders.

Default mode ``vendor`` is exactly what the marketplace did before: the vendor
marks their own shipment delivered, the order completes and the escrow earning
is released. Mode ``customer_confirm`` puts the group in ``delivery_reported``
instead and waits for the customer, a dispute, or the auto-confirm cron.
"""
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD


class FbmDeliveryCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["leaders.commission.rule"].create({
            "name": "10% vendor", "scope": "vendor", "vendor_id": cls.vendor.id,
            "rule_type": "percent", "percent": 10.0,
        })
        cls.customer_user = cls.env["res.users"].with_context(
            no_reset_password=True).create({
                "name": "Buying Bob",
                "login": "lm.buyer@example.com",
                "email": "lm.buyer@example.com",
                "password": PORTAL_PASSWORD,
                "partner_id": cls.customer.id,
                "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
            })

    def _set_mode(self, mode, auto_days=None):
        param = self.env["ir.config_parameter"].sudo()
        param.set_param("leaders_marketplace.fbm_delivery_mode", mode)
        if auto_days is not None:
            param.set_param("leaders_marketplace.fbm_auto_confirm_days", auto_days)

    def _shipped_fbm_group(self, qty=1):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({
                "product_id": self.fbm_product.id, "product_uom_qty": qty})],
        })
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "TRK-POD"
        group.action_ship()
        self.assertEqual(group.state, "shipped")
        return vendor_order, group

    def _earnings(self, vendor_order):
        return self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "earning")])


@tagged("post_install", "-at_install")
class TestDefaultVendorMode(FbmDeliveryCommon):
    """The default must reproduce today's behavior bit for bit."""

    def test_default_mode_is_vendor(self):
        self.assertEqual(
            self.env["leaders.fulfillment.group"]._leaders_fbm_delivery_mode(),
            "vendor")

    def test_reporting_delivers_outright_and_releases_the_earning(self):
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery()
        self.assertEqual(group.state, "delivered")
        self.assertEqual(vendor_order.state, "completed")
        self.assertTrue(self._earnings(vendor_order))

    def test_who_and_when_are_recorded_in_vendor_mode_too(self):
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)
        self.assertEqual(group.delivered_reported_by, self.owner_user)
        self.assertEqual(group.delivered_confirmed_by, self.owner_user)
        self.assertTrue(group.delivered_reported_date)
        self.assertTrue(group.delivered_date)

    def test_cron_does_nothing_in_vendor_mode(self):
        self.assertFalse(
            self.env["leaders.fulfillment.group"].cron_auto_confirm_delivery())


@tagged("post_install", "-at_install")
class TestCustomerConfirmMode(FbmDeliveryCommon):

    def setUp(self):
        super().setUp()
        self._set_mode("customer_confirm", auto_days=3)

    def test_vendor_report_does_not_complete_the_order_or_pay(self):
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)
        self.assertEqual(group.state, "delivery_reported")
        self.assertEqual(vendor_order.state, "fulfilled")
        self.assertFalse(self._earnings(vendor_order))
        self.assertEqual(group.delivered_reported_by, self.owner_user)
        self.assertFalse(group.delivered_confirmed_by)

    def test_customer_confirmation_completes_and_pays(self):
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)
        group.action_delivered(confirmed_by=self.customer_user)
        self.assertEqual(group.state, "delivered")
        self.assertEqual(vendor_order.state, "completed")
        self.assertEqual(group.delivered_confirmed_by, self.customer_user)
        self.assertTrue(self._earnings(vendor_order))

    def test_not_received_opens_a_dispute_and_pays_nothing(self):
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)
        dispute = group.action_delivery_disputed(
            reason="Nothing ever arrived", raised_by=self.customer_user)
        self.assertEqual(len(dispute), 1)
        self.assertEqual(dispute.vendor_order_id, vendor_order)
        self.assertEqual(dispute.state, "open")
        self.assertIn("Nothing ever arrived", dispute.reason)
        self.assertEqual(group.state, "delivery_reported")
        self.assertFalse(self._earnings(vendor_order))
        self.assertTrue(any(
            "never received" in body for body in vendor_order.message_ids.mapped("body")))

    def test_cron_confirms_only_after_the_delay(self):
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)
        Group = self.env["leaders.fulfillment.group"]

        Group.cron_auto_confirm_delivery()
        self.assertEqual(group.state, "delivery_reported", "confirmed far too early")

        group.delivered_reported_date = fields.Datetime.now() - timedelta(days=4)
        Group.cron_auto_confirm_delivery()
        self.assertEqual(group.state, "delivered")
        self.assertEqual(vendor_order.state, "completed")
        self.assertTrue(self._earnings(vendor_order))

    def test_zero_days_waits_for_the_customer_forever(self):
        self._set_mode("customer_confirm", auto_days=0)
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)
        group.delivered_reported_date = fields.Datetime.now() - timedelta(days=400)
        self.env["leaders.fulfillment.group"].cron_auto_confirm_delivery()
        self.assertEqual(group.state, "delivery_reported")

    def test_backend_delivered_button_overrides_in_both_modes(self):
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)
        group.action_delivered()          # the back-office button, no arguments
        self.assertEqual(group.state, "delivered")
        self.assertEqual(group.delivered_confirmed_by, self.env.user)

    def test_a_reported_delivery_cannot_be_reported_again(self):
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)
        with self.assertRaises(UserError):
            group.action_report_delivery(reported_by=self.owner_user)

    # ------------------------------------------ delivery_reported == shipped
    def test_order_stays_returnable_while_awaiting_confirmation(self):
        vendor_order, group = self._shipped_fbm_group(qty=2)
        group.action_report_delivery(reported_by=self.owner_user)
        record = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id,
            "reason": "Damaged",
            "line_ids": [Command.create({
                "vendor_line_id": vendor_order.line_ids[:1].id, "quantity": 1})],
        })
        self.assertTrue(record)

    def test_a_reported_order_can_no_longer_be_cancelled(self):
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)
        with self.assertRaises(UserError):
            vendor_order.action_cancel()

    def test_partial_report_leaves_the_order_partially_fulfilled(self):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [
                Command.create({"product_id": self.fbm_product.id, "product_uom_qty": 1}),
                Command.create({"product_id": self.fbl_product.id, "product_uom_qty": 1}),
            ],
        })
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        fbm = vendor_order.fulfillment_group_ids.filtered(
            lambda g: g.fulfillment_type == "fbm")
        fbm.tracking_ref = "TRK-PART"
        fbm.action_ship()
        fbm.action_report_delivery(reported_by=self.owner_user)
        self.assertEqual(fbm.state, "delivery_reported")
        self.assertEqual(vendor_order.state, "partially_fulfilled")

    def test_fbl_is_untouched_by_the_setting(self):
        """The setting is about FBM; the warehouse still delivers FBL directly."""
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({
                "product_id": self.fbl_product.id, "product_uom_qty": 1})],
        })
        order.action_confirm()
        group = order.vendor_order_ids.fulfillment_group_ids
        group.action_ship()
        group.action_report_delivery()
        self.assertEqual(group.state, "delivered")


@tagged("post_install", "-at_install")
class TestDeliveryPortalRoutes(HttpCase, FbmDeliveryCommon):

    def test_vendor_route_stores_the_proof_and_the_reporter(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        vendor_order, group = self._shipped_fbm_group()
        url = "/my/vendor/fulfillment/%s/delivered" % group.id
        self._post(url, "/my/vendor/orders/%s" % vendor_order.id,
                   files={"delivery_proof": ("pod.png", b"\x89PNG\r\n\x1a\nproof", "image/png")})
        self.assertEqual(group.delivered_reported_by, self.owner_user)
        self.assertEqual(group.delivery_proof_name, "pod.png")
        self.assertTrue(group.delivery_proof)
        # Default mode: the route still delivers outright.
        self.assertEqual(group.state, "delivered")

    def test_customer_confirms_through_the_portal(self):
        self._set_mode("customer_confirm", auto_days=3)
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)

        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        order_url = "/my/marketplace/orders/%s" % vendor_order.id
        page = self.url_open(order_url)
        self.assertIn("Confirm I received it", page.text)
        self._post("/my/marketplace/fulfillment/%s/confirm-delivery" % group.id, order_url)
        self.assertEqual(group.state, "delivered")
        self.assertEqual(group.delivered_confirmed_by, self.customer_user)
        self.assertTrue(self._earnings(vendor_order))

    def test_customer_reports_a_missing_shipment_through_the_portal(self):
        self._set_mode("customer_confirm", auto_days=3)
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)

        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        order_url = "/my/marketplace/orders/%s" % vendor_order.id
        self._post("/my/marketplace/fulfillment/%s/not-received" % group.id, order_url,
                   reason="Never showed up")
        self.assertEqual(self.env["leaders.vendor.dispute"].search_count([
            ("vendor_order_id", "=", vendor_order.id)]), 1)
        self.assertFalse(self._earnings(vendor_order))

    def test_another_customer_cannot_confirm_someone_elses_shipment(self):
        self._set_mode("customer_confirm", auto_days=3)
        vendor_order, group = self._shipped_fbm_group()
        group.action_report_delivery(reported_by=self.owner_user)

        # The vendor's own portal user is not the buyer on this order.
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        response = self.url_open(
            "/my/marketplace/fulfillment/%s/confirm-delivery" % group.id,
            data={"csrf_token": self._csrf("/my/vendor/products")})
        self.assertIn(response.status_code, (403, 400))
        self.assertEqual(group.state, "delivery_reported")


@tagged("post_install", "-at_install")
class TestCustomerStageOfAMultiPartOrder(FbmDeliveryCommon):
    """The track page's stage is where the slowest part of the order is: a vendor's shipment that left is shipped,
    not delivered, and an order with a part nobody has shipped yet is never shown delivered."""

    def _stage(self, order):
        from odoo.addons.leaders_marketplace.controllers.storefront_orders import StorefrontOrders
        return StorefrontOrders()._stage(order)[0]

    def test_a_vendors_shipment_is_shipped_until_it_is_delivered(self):
        vendor_order, group = self._shipped_fbm_group()
        order = vendor_order.sale_order_id
        self.assertEqual(self._stage(order), "shipped")  # the picking is done: it left, it did not arrive
        group.action_delivered()
        self.assertEqual(group.state, "delivered")
        self.assertEqual(self._stage(order), "delivered")

    def test_a_part_still_waiting_keeps_the_order_from_being_delivered(self):
        own = self.env["product.product"].create({"name": "Platform part", "is_storable": True,
                                                  "leaders_platform_sold": True, "list_price": 50}) \
            if "leaders_platform_sold" in self.env["product.product"]._fields else None
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": self.fbm_product.id, "product_uom_qty": 1}),
                           Command.create({"product_id": (own or self.fbm_product).id, "product_uom_qty": 2,
                                           "leaders_platform_sold": True})]})
        order.action_confirm()
        group = order.vendor_order_ids.fulfillment_group_ids
        group.tracking_ref = "TRK-MIX"
        group.action_ship()
        group.action_delivered()
        self.assertEqual(group.state, "delivered")
        # the platform's own part is not shipped: the order is not delivered, and it is being prepared
        self.assertEqual(self._stage(order), "preparing")


@tagged("post_install", "-at_install")
class TestStockArrivingAfterTheShipmentWasMade(FbmDeliveryCommon):
    """A vendor who had too little stock ships the rest once he adds it: the waiting delivery takes the new stock."""

    def test_the_waiting_delivery_is_reserved_when_the_vendor_adds_stock(self):
        product = self.fbm_product
        offer = self.env["leaders.marketplace.offer"].search(
            [("product_id", "=", product.id), ("vendor_id", "=", self.vendor.id)], limit=1)
        owner = self.vendor.commercial_partner_id
        Quant = self.env["stock.quant"]
        Quant._leaders_adjust_vendor_stock(product, offer.stock_location_id, owner, 1 - offer.available_qty())
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": product.id, "product_uom_qty": 3})]})
        order.action_confirm()
        move = order.vendor_order_ids.fulfillment_group_ids.picking_ids.move_ids
        self.assertNotEqual(move.state, "assigned")  # the vendor holds 1 of the 3
        Quant._leaders_adjust_vendor_stock(product, offer.stock_location_id, owner, 5)
        self.assertEqual((move.state, move.quantity), ("assigned", 3))  # the stock he added is reserved at once
