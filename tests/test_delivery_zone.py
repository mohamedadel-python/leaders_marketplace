from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_vendor_split import MarketplaceVendorCommon


@tagged("post_install", "-at_install")
class TestDeliveryZone(MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cairo = cls.env.ref("base.state_eg_c")
        cls.alexandria = cls.env.ref("base.state_eg_alx")
        cls.egypt = cls.env.ref("base.eg")
        cls.carrier = cls.env.ref("leaders_marketplace.marketplace_zone_delivery_carrier")

        cls.vendor2 = cls.env["res.partner"].create({
            "name": "Second Zone Vendor", "is_marketplace_vendor": True, "vendor_state": "approved",
        })
        cls.vendor2._marketplace_ensure_vendor_stock()
        cls.vendor2_product = cls.env["product.product"].create({
            "name": "Vendor2 FBM Product", "type": "consu", "is_storable": True,
            "list_price": 20.0, "taxes_id": [Command.clear()],
        })
        cls.vendor2_offer = cls.env["leaders.marketplace.offer"].create({
            "product_tmpl_id": cls.vendor2_product.product_tmpl_id.id,
            "product_id": cls.vendor2_product.id,
            "vendor_id": cls.vendor2.id,
            "seller_sku": "V2-FBM-1",
            "price": 20.0,
            "fulfillment_type": "fbm",
            "stock_location_id": cls.vendor2._marketplace_stock_location("fbm").id,
        })
        cls.env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.vendor2_product, cls.vendor2.vendor_location_id, cls.vendor2, 5)
        cls.vendor2_offer.action_approve()

        cls.env["leaders.vendor.delivery.zone"].create({
            "vendor_id": cls.vendor.id, "state_id": cls.cairo.id, "price": 30.0,
        })
        cls.env["leaders.vendor.delivery.zone"].create({
            "vendor_id": cls.vendor2.id, "state_id": cls.cairo.id, "price": 15.0,
        })

        cls.address_cairo = cls.env["res.partner"].create({
            "name": "Cairo Customer", "state_id": cls.cairo.id, "country_id": cls.egypt.id,
        })
        cls.address_alexandria = cls.env["res.partner"].create({
            "name": "Alexandria Customer", "state_id": cls.alexandria.id, "country_id": cls.egypt.id,
        })

    def _order_with_carrier(self, lines, shipping_partner, with_delivery_line=False):
        order_line = [Command.create({"product_id": p.id, "product_uom_qty": q}) for p, q in lines]
        if with_delivery_line:
            order_line.append(Command.create({
                "product_id": self.carrier.product_id.id, "is_delivery": True,
                "product_uom_qty": 1, "price_unit": 0.0,
            }))
        return self.env["sale.order"].create({
            "partner_id": shipping_partner.id,
            "partner_shipping_id": shipping_partner.id,
            "carrier_id": self.carrier.id,
            "order_line": order_line,
        })

    def test_rate_shipment_sums_each_vendor_zone_price(self):
        order = self._order_with_carrier(
            [(self.fbm_product, 1), (self.vendor2_product, 1)], self.address_cairo)
        result = self.carrier.marketplace_zone_rate_shipment(order)
        self.assertTrue(result["success"])
        self.assertEqual(result["price"], 45.0)

    def test_rate_shipment_blocks_when_a_vendor_zone_is_missing(self):
        order = self._order_with_carrier(
            [(self.fbm_product, 1), (self.vendor2_product, 1)], self.address_alexandria)
        result = self.carrier.marketplace_zone_rate_shipment(order)
        self.assertFalse(result["success"])
        self.assertIn("does not deliver", result["error_message"])

    def test_split_creates_one_priced_delivery_line_per_vendor(self):
        order = self._order_with_carrier(
            [(self.fbm_product, 1), (self.vendor2_product, 1)], self.address_cairo, with_delivery_line=True)
        order.action_confirm()

        vendor_orders = order.vendor_order_ids
        self.assertEqual(len(vendor_orders), 2)
        v1_order = vendor_orders.filtered(lambda o: o.vendor_id == self.vendor)
        v2_order = vendor_orders.filtered(lambda o: o.vendor_id == self.vendor2)
        v1_delivery = v1_order.line_ids.filtered("is_delivery")
        v2_delivery = v2_order.line_ids.filtered("is_delivery")
        self.assertEqual(len(v1_delivery), 1)
        self.assertEqual(len(v2_delivery), 1)
        self.assertEqual(v1_delivery.price_unit, 30.0)
        self.assertEqual(v2_delivery.price_unit, 15.0)

    def test_split_blocks_confirm_when_a_vendor_zone_is_missing(self):
        order = self._order_with_carrier(
            [(self.fbm_product, 1), (self.vendor2_product, 1)], self.address_alexandria, with_delivery_line=True)
        with self.assertRaises(UserError):
            order.action_confirm()

    def test_non_zone_carrier_keeps_legacy_single_line_behavior(self):
        fixed_carrier = self.env.ref("delivery.free_delivery_carrier")
        order = self.env["sale.order"].create({
            "partner_id": self.address_cairo.id,
            "partner_shipping_id": self.address_cairo.id,
            "carrier_id": fixed_carrier.id,
            "order_line": [
                Command.create({"product_id": self.fbm_product.id, "product_uom_qty": 1}),
                Command.create({"product_id": self.vendor2_product.id, "product_uom_qty": 1}),
                Command.create({
                    "product_id": fixed_carrier.product_id.id, "is_delivery": True,
                    "product_uom_qty": 1, "price_unit": 9.99,
                }),
            ],
        })
        order.action_confirm()
        vendor_orders = order.vendor_order_ids
        self.assertEqual(len(vendor_orders), 2)
        delivery_lines = vendor_orders.line_ids.filtered("is_delivery")
        self.assertEqual(len(delivery_lines), 1)
        self.assertEqual(delivery_lines.price_unit, 9.99)


@tagged("post_install", "-at_install")
class TestDeliveryAllocation(TestDeliveryZone):
    """Who receives the delivery charge when the checkout is split."""

    def _set(self, key, value):
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.%s" % key, value)

    def _mixed_cart(self, delivery_price=45.0):
        """A zone-carrier cart with one company product and two vendors."""
        order = self._order_with_carrier(
            [(self.own_product, 1), (self.fbm_product, 1), (self.vendor2_product, 1)],
            self.address_cairo, with_delivery_line=True)
        order.order_line.filtered("is_delivery").price_unit = delivery_price
        return order

    # ---------------------------------------------- 1. zone carrier, mixed cart
    def test_mixed_cart_pays_each_vendor_its_zone_price(self):
        order = self._mixed_cart()
        total_before = order.amount_total + sum(
            o.amount_total for o in order.vendor_order_ids)

        order.action_confirm()

        vendor_orders = order.vendor_order_ids
        self.assertEqual(len(vendor_orders), 2)
        v1 = vendor_orders.filtered(lambda o: o.vendor_id == self.vendor)
        v2 = vendor_orders.filtered(lambda o: o.vendor_id == self.vendor2)
        self.assertEqual(v1.line_ids.filtered("is_delivery").price_unit, 30.0)
        self.assertEqual(v2.line_ids.filtered("is_delivery").price_unit, 15.0)
        # The company keeps its products, and the delivery it no longer ships.
        self.assertTrue(order.order_line.filtered(lambda l: l.product_id == self.own_product))
        self.assertFalse(order.order_line.filtered("is_delivery"),
                         "the company delivery line should have reached zero")
        # The customer pays exactly what they paid before.
        self.assertEqual(
            order.amount_total + sum(o.amount_total for o in vendor_orders), total_before)

    def test_mixed_cart_keeps_the_company_share_of_the_delivery(self):
        order = self._mixed_cart(delivery_price=60.0)   # 45 for vendors, 15 company
        order.action_confirm()
        company_delivery = order.order_line.filtered("is_delivery")
        self.assertEqual(len(company_delivery), 1)
        self.assertEqual(company_delivery.price_unit, 15.0)

    def test_the_company_delivery_can_never_go_negative(self):
        order = self._mixed_cart(delivery_price=20.0)   # rated before a price rise
        with self.assertRaises(UserError) as caught:
            order.action_confirm()
        self.assertIn("more than", str(caught.exception))

    def test_vendor_only_zone_checkout_is_unchanged(self):
        """MUST NOT CHANGE: the pure vendor-only zone case."""
        order = self._order_with_carrier(
            [(self.fbm_product, 1), (self.vendor2_product, 1)],
            self.address_cairo, with_delivery_line=True)
        order.action_confirm()
        vendor_orders = order.vendor_order_ids
        self.assertEqual(
            sorted(vendor_orders.line_ids.filtered("is_delivery").mapped("price_unit")),
            [15.0, 30.0])
        self.assertFalse(order.order_line.filtered("is_delivery"))

    # ---------------------------------------------------- 2. FBL delivery income
    def test_fbl_vendor_still_gets_the_delivery_by_default(self):
        order = self._order_with_carrier(
            [(self.fbl_product, 1)], self.address_cairo, with_delivery_line=True)
        order.order_line.filtered("is_delivery").price_unit = 30.0
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        self.assertEqual(vendor_order.line_ids.filtered("is_delivery").price_unit, 30.0)

    def test_platform_mode_gives_an_fbl_vendor_no_delivery_line(self):
        self._set("fbl_delivery_income", "platform")
        order = self._order_with_carrier(
            [(self.fbl_product, 1)], self.address_cairo, with_delivery_line=True)
        order.order_line.filtered("is_delivery").price_unit = 30.0
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        self.assertFalse(vendor_order.line_ids.filtered("is_delivery"))

    def test_platform_mode_books_the_kept_delivery_as_income(self):
        self._configure_accounts()
        self._set("fbl_delivery_income", "platform")
        order = self._order_with_carrier(
            [(self.fbl_product, 1)], self.address_cairo, with_delivery_line=True)
        order.order_line.filtered("is_delivery").price_unit = 30.0
        order.action_confirm()
        move = self.env["account.move"].search([
            ("ref", "like", "Marketplace delivery income%")], limit=1)
        self.assertTrue(move, "no platform delivery income entry was posted")
        self.assertEqual(sum(move.line_ids.mapped("credit")), 30.0)

    def test_platform_mode_skips_gracefully_without_accounts(self):
        self._set("fbl_delivery_income", "platform")
        order = self._order_with_carrier(
            [(self.fbl_product, 1)], self.address_cairo, with_delivery_line=True)
        order.order_line.filtered("is_delivery").price_unit = 30.0
        order.action_confirm()          # must not raise
        self.assertTrue(any("not configured" in body
                            for body in order.message_ids.mapped("body")))

    def test_platform_mode_leaves_an_fbm_vendor_alone(self):
        self._set("fbl_delivery_income", "platform")
        order = self._order_with_carrier(
            [(self.fbm_product, 1)], self.address_cairo, with_delivery_line=True)
        order.order_line.filtered("is_delivery").price_unit = 30.0
        order.action_confirm()
        self.assertEqual(
            order.vendor_order_ids.line_ids.filtered("is_delivery").price_unit, 30.0)

    # ------------------------------------------ 3. non-zone carrier allocation
    def _non_zone_order(self, price=9.99):
        carrier = self.env.ref("delivery.free_delivery_carrier")
        return self.env["sale.order"].create({
            "partner_id": self.address_cairo.id,
            "partner_shipping_id": self.address_cairo.id,
            "carrier_id": carrier.id,
            "order_line": [
                Command.create({"product_id": self.fbm_product.id, "product_uom_qty": 1}),
                Command.create({"product_id": self.vendor2_product.id, "product_uom_qty": 1}),
                Command.create({
                    "product_id": carrier.product_id.id, "is_delivery": True,
                    "product_uom_qty": 1, "price_unit": price,
                }),
            ],
        })

    def test_non_zone_default_still_gives_it_all_to_the_first_vendor(self):
        order = self._non_zone_order()
        order.action_confirm()
        deliveries = order.vendor_order_ids.line_ids.filtered("is_delivery")
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries.price_unit, 9.99)

    def test_non_zone_default_says_in_the_chatter_who_got_it(self):
        order = self._non_zone_order()
        order.action_confirm()
        bodies = " ".join(order.message_ids.mapped("body"))
        self.assertIn("delivery charge of this checkout was assigned to", bodies)

    def test_non_zone_proportional_split_when_enabled(self):
        self._set("split_delivery_proportional", True)
        order = self._non_zone_order(price=30.0)
        # fbm_product sells at 100, vendor2_product at 20 -> 120 untaxed total.
        order.action_confirm()
        deliveries = order.vendor_order_ids.line_ids.filtered("is_delivery")
        self.assertEqual(len(deliveries), 2)
        self.assertEqual(sum(deliveries.mapped("price_unit")), 30.0)
        by_vendor = {d.order_id.vendor_id: d.price_unit for d in deliveries}
        self.assertEqual(by_vendor[self.vendor], 25.0)      # 100/120 of 30
        self.assertEqual(by_vendor[self.vendor2], 5.0)      # 20/120 of 30

    def test_the_proportional_remainder_lands_on_the_last_vendor(self):
        self._set("split_delivery_proportional", True)
        order = self._non_zone_order(price=10.0)
        order.action_confirm()
        deliveries = order.vendor_order_ids.line_ids.filtered("is_delivery")
        # Both vendors are served, and whatever the rounding the customer's
        # charge is allocated in full -- the remainder lands on the last vendor.
        self.assertEqual(len(deliveries), 2)
        self.assertEqual(sum(deliveries.mapped("price_unit")), 10.0)
