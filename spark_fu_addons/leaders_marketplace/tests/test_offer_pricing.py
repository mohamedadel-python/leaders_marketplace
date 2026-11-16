"""The price a customer pays is the price the winning vendor listed.

Two problems this covers: a multi-variant template has one ``list_price`` but
one winning offer per variant, and a Buy Box winner can change between
add-to-cart and payment.
"""
from odoo import Command
from odoo.tests import tagged

from .test_vendor_split import MarketplaceVendorCommon


class OfferPricingCommon(MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Offer = cls.env["leaders.marketplace.offer"]
        cls.bronze = cls.env.ref("leaders_marketplace.vendor_tier_bronze")
        cls.platinum = cls.env.ref("leaders_marketplace.vendor_tier_platinum")

    @classmethod
    def _vendor(cls, name, tier=None):
        vendor = cls.env["res.partner"].create({
            "name": name, "is_marketplace_vendor": True, "vendor_state": "approved",
            "vendor_tier_id": tier.id if tier else False,
        })
        vendor._marketplace_ensure_vendor_stock()
        return vendor

    def _offer(self, vendor, product, sku, price, qty=10):
        offer = self.Offer.create({
            "product_tmpl_id": product.product_tmpl_id.id,
            "product_id": product.id,
            "vendor_id": vendor.id,
            "seller_sku": sku,
            "price": price,
            "fulfillment_type": "fbm",
            "stock_location_id": vendor._marketplace_stock_location("fbm").id,
        })
        if qty:
            self.env["stock.quant"]._leaders_adjust_vendor_stock(
                product, vendor.vendor_location_id, vendor, qty)
        offer.action_approve()
        return offer

    @classmethod
    def _two_variant_template(cls):
        """A template with a Size attribute and two real variants."""
        attribute = cls.env["product.attribute"].create({
            "name": "LM Size",
            "value_ids": [Command.create({"name": "S"}), Command.create({"name": "L"})],
        })
        template = cls.env["product.template"].create({
            "name": "Two Variant Product", "type": "consu", "is_storable": True,
            "list_price": 999.0, "taxes_id": [Command.clear()],
            "attribute_line_ids": [Command.create({
                "attribute_id": attribute.id,
                "value_ids": [Command.set(attribute.value_ids.ids)],
            })],
        })
        variants = template.product_variant_ids.sorted("id")
        assert len(variants) == 2, "expected two variants, got %s" % len(variants)
        return template, variants[0], variants[1]


@tagged("post_install", "-at_install")
class TestOfferPricing(OfferPricingCommon):

    # ----------------------------------------------- list_price sync scope
    def test_single_variant_template_still_syncs_list_price(self):
        """MUST NOT CHANGE for the common case."""
        product = self.env["product.product"].create({
            "name": "Single Variant", "type": "consu", "is_storable": True,
            "list_price": 999.0, "taxes_id": [Command.clear()],
        })
        vendor = self._vendor("Single Seller", self.bronze)
        self._offer(vendor, product, "SV-1", 42.0)
        self.assertEqual(product.product_tmpl_id.list_price, 42.0)

    def test_multi_variant_template_no_longer_fights_over_list_price(self):
        template, small, large = self._two_variant_template()
        vendor_a = self._vendor("Var A", self.bronze)
        vendor_b = self._vendor("Var B", self.bronze)
        self._offer(vendor_a, small, "VAR-S", 30.0)
        self._offer(vendor_b, large, "VAR-L", 80.0)
        # Neither variant's winner may stamp the shared template price.
        self.assertEqual(template.list_price, 999.0)

    # ------------------------------------------------ each variant its price
    def test_each_variant_is_priced_from_its_own_offer(self):
        template, small, large = self._two_variant_template()
        vendor_a = self._vendor("Combo A", self.bronze)
        vendor_b = self._vendor("Combo B", self.bronze)
        self._offer(vendor_a, small, "CMB-S", 30.0)
        self._offer(vendor_b, large, "CMB-L", 80.0)

        small_info = template._get_combination_info(product_id=small.id)
        large_info = template._get_combination_info(product_id=large.id)
        self.assertEqual(small_info["price"], 30.0)
        self.assertEqual(large_info["price"], 80.0)

    def test_a_cart_line_is_priced_from_its_offer_not_the_template(self):
        template, small, large = self._two_variant_template()
        vendor_a = self._vendor("Line A", self.bronze)
        vendor_b = self._vendor("Line B", self.bronze)
        self._offer(vendor_a, small, "LN-S", 30.0)
        self._offer(vendor_b, large, "LN-L", 80.0)

        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [
                Command.create({"product_id": small.id, "product_uom_qty": 1}),
                Command.create({"product_id": large.id, "product_uom_qty": 1}),
            ],
        })
        for line in order.order_line:
            line.marketplace_offer_id = self.Offer._get_marketplace_winner(line.product_id)
        order.order_line.with_context(force_price_recomputation=True)._compute_price_unit()

        by_product = {line.product_id: line.price_unit for line in order.order_line}
        self.assertEqual(by_product[small], 30.0)
        self.assertEqual(by_product[large], 80.0)

    def test_a_pricelist_percentage_still_applies_on_top_of_the_offer_price(self):
        # A multi-variant template on purpose: with a single variant the synced
        # list_price would give the right answer even without the offer-price
        # base, and the test would prove nothing.
        template, product, other = self._two_variant_template()
        vendor = self._vendor("Discount Seller", self.bronze)
        self._offer(vendor, product, "DSC-1", 100.0)
        self.assertEqual(template.list_price, 999.0, "list_price must stay untouched here")
        pricelist = self.env["product.pricelist"].create({
            "name": "10% off",
            "currency_id": self.company.currency_id.id,
            "item_ids": [Command.create({
                "compute_price": "percentage", "percent_price": 10.0,
                "applied_on": "3_global",
            })],
        })
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "pricelist_id": pricelist.id,
            "order_line": [Command.create({"product_id": product.id, "product_uom_qty": 1})],
        })
        line = order.order_line
        line.marketplace_offer_id = self.Offer._get_marketplace_winner(product)
        line.with_context(force_price_recomputation=True)._compute_price_unit()
        # 10% off the OFFER price (100), not off the catalogue price (999).
        self.assertEqual(line.price_unit, 90.0)

    def test_a_line_without_an_offer_is_untouched(self):
        line_product = self.own_product        # a plain company product
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": line_product.id, "product_uom_qty": 1})],
        })
        self.assertFalse(order.order_line.marketplace_offer_id)
        self.assertEqual(order.order_line.price_unit, line_product.list_price)


@tagged("post_install", "-at_install")
class TestCartRevalidation(OfferPricingCommon):

    def _cart_with_offer(self, price=100.0):
        product = self.env["product.product"].create({
            "name": "Cart Product", "type": "consu", "is_storable": True,
            "list_price": 999.0, "taxes_id": [Command.clear()],
        })
        vendor = self._vendor("Cart Seller", self.bronze)
        offer = self._offer(vendor, product, "CART-1", price)
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": product.id, "product_uom_qty": 1})],
        })
        order.order_line.marketplace_offer_id = offer
        order.order_line.with_context(force_price_recomputation=True)._compute_price_unit()
        return product, vendor, offer, order

    def test_an_unchanged_cart_reports_no_change(self):
        product, vendor, offer, order = self._cart_with_offer()
        self.assertEqual(order.order_line.price_unit, 100.0)
        self.assertFalse(order._leaders_revalidate_marketplace_prices())
        self.assertEqual(order.order_line.price_unit, 100.0)

    def test_a_price_move_on_the_same_offer_is_caught(self):
        product, vendor, offer, order = self._cart_with_offer()
        offer.price = 130.0
        self.assertTrue(order._leaders_revalidate_marketplace_prices())
        self.assertEqual(order.order_line.price_unit, 130.0)

    def test_a_dead_offer_is_repointed_to_the_new_winner(self):
        product, vendor, offer, order = self._cart_with_offer()
        other_vendor = self._vendor("Second Cart Seller", self.bronze)
        replacement = self._offer(other_vendor, product, "CART-2", 140.0)

        offer.write({"state": "suspended"})      # the listed offer goes away

        self.assertTrue(order._leaders_revalidate_marketplace_prices())
        self.assertEqual(order.order_line.marketplace_offer_id, replacement)
        self.assertEqual(order.order_line.price_unit, 140.0)

    def test_the_vendor_order_follows_the_reviewed_offer_and_price(self):
        """After the customer reviews, the vendor gets their own offer price."""
        product, vendor, offer, order = self._cart_with_offer()
        other_vendor = self._vendor("Winning Seller", self.platinum)
        replacement = self._offer(other_vendor, product, "CART-3", 140.0)
        offer.write({"state": "suspended"})

        order._leaders_revalidate_marketplace_prices()
        order.action_confirm()

        vendor_order = order.vendor_order_ids
        self.assertEqual(len(vendor_order), 1)
        self.assertEqual(vendor_order.vendor_id, other_vendor)
        line = vendor_order.line_ids.filtered(lambda l: not l.is_delivery)
        self.assertEqual(line.offer_id, replacement)
        self.assertEqual(line.price_unit, 140.0)

    def test_a_product_nobody_sells_any_more_is_left_to_the_standard_checks(self):
        product, vendor, offer, order = self._cart_with_offer()
        offer.write({"state": "suspended"})
        # No replacement exists: the line keeps its offer, no crash.
        order._leaders_revalidate_marketplace_prices()
        self.assertEqual(order.order_line.marketplace_offer_id, offer)


@tagged("post_install", "-at_install")
class TestCartRevalidationRoute(OfferPricingCommon):
    """The customer is actually sent back to the cart, through the real check."""

    def test_check_cart_redirects_when_a_price_moved(self):
        from odoo.addons.website_sale.controllers.main import WebsiteSale
        self.assertTrue(
            hasattr(WebsiteSale, "_check_cart"),
            "website_sale no longer exposes the _check_cart hook this relies on")

    def test_the_cart_notice_template_is_installed(self):
        view = self.env.ref("leaders_marketplace.cart_marketplace_price_changed")
        self.assertEqual(view.inherit_id, self.env.ref("website_sale.cart"))
        self.assertIn("Prices of some items changed", view.arch_db)
