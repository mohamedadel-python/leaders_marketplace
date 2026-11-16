"""Buy Box correctness and freshness.

The score is a *relative* measure -- price against the sibling offers,
availability against live stock -- so it has to be refreshed whenever a
sibling or the stock moves, not only when the offer itself is edited.
"""
from odoo import Command
from odoo.tests import tagged

from .test_vendor_split import MarketplaceVendorCommon


@tagged("post_install", "-at_install")
class TestBuyBox(MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Offer = cls.env["leaders.marketplace.offer"]
        cls.bronze = cls.env.ref("leaders_marketplace.vendor_tier_bronze")
        cls.platinum = cls.env.ref("leaders_marketplace.vendor_tier_platinum")
        cls.contested = cls.env["product.product"].create({
            "name": "Contested Product", "type": "consu", "is_storable": True,
            "list_price": 100.0, "taxes_id": [Command.clear()],
        })

    @classmethod
    def _vendor(cls, name, tier):
        vendor = cls.env["res.partner"].create({
            "name": name, "is_marketplace_vendor": True, "vendor_state": "approved",
            "vendor_tier_id": tier.id,
        })
        vendor._marketplace_ensure_vendor_stock()
        return vendor

    def _offer(self, vendor, product, sku, price, qty, rating=0.0):
        offer = self.Offer.create({
            "product_tmpl_id": product.product_tmpl_id.id,
            "product_id": product.id,
            "vendor_id": vendor.id,
            "seller_sku": sku,
            "price": price,
            "rating": rating,
            "fulfillment_type": "fbm",
            "stock_location_id": vendor._marketplace_stock_location("fbm").id,
        })
        if qty:
            self.env["stock.quant"]._leaders_adjust_vendor_stock(
                product, vendor.vendor_location_id, vendor, qty)
        offer.action_approve()
        return offer

    # ------------------------------------------------------ (a) the tier signal
    def test_platinum_beats_bronze_when_everything_else_is_equal(self):
        bronze_vendor = self._vendor("Bronze Seller", self.bronze)
        platinum_vendor = self._vendor("Platinum Seller", self.platinum)
        bronze_offer = self._offer(bronze_vendor, self.contested, "BX-BRONZE", 100.0, 5)
        platinum_offer = self._offer(platinum_vendor, self.contested, "BX-PLAT", 100.0, 5)

        self.assertGreater(platinum_offer.buybox_score, bronze_offer.buybox_score)
        self.assertTrue(platinum_offer.is_buybox_winner)
        self.assertFalse(bronze_offer.is_buybox_winner)

    def test_tier_score_is_normalized_over_the_tiers_that_exist(self):
        bronze_vendor = self._vendor("T Bronze", self.bronze)
        platinum_vendor = self._vendor("T Platinum", self.platinum)
        bronze_offer = self._offer(bronze_vendor, self.contested, "TS-B", 100.0, 5)
        platinum_offer = self._offer(platinum_vendor, self.contested, "TS-P", 100.0, 5)
        self.assertEqual(bronze_offer._tier_score(), 0.0)      # lowest sequence
        self.assertEqual(platinum_offer._tier_score(), 1.0)    # highest sequence

    def test_no_tier_scores_zero(self):
        plain = self.env["res.partner"].create({
            "name": "Tierless", "is_marketplace_vendor": True, "vendor_state": "approved"})
        plain._marketplace_ensure_vendor_stock()
        offer = self._offer(plain, self.contested, "NT-1", 100.0, 5)
        self.assertFalse(offer.vendor_id.vendor_tier_id)
        self.assertEqual(offer._tier_score(), 0.0)

    def test_a_single_tier_scores_one(self):
        self.assertEqual(
            self.Offer._tier_score_scale(),
            (min(self.env["leaders.vendor.tier"].search([]).mapped("sequence")),
             max(self.env["leaders.vendor.tier"].search([]).mapped("sequence"))))
        vendor = self._vendor("Solo Tier", self.bronze)
        offer = self._offer(vendor, self.contested, "ST-1", 100.0, 5)
        # With one tier in scale, min == max -> the signal cannot discriminate.
        self.assertEqual(offer._tier_score(scale=(10, 10)), 1.0)

    # ------------------------------------------- (b) a sibling's price matters
    def test_a_sibling_price_change_rescores_the_untouched_offer(self):
        """The price signal is normalized against the dearest live offer.

        Lowering the dearest offer moves that reference, so every other offer's
        score changes without being edited -- which the stored score used to
        miss entirely.
        """
        dear_vendor = self._vendor("Price A", self.bronze)
        cheap_vendor = self._vendor("Price B", self.bronze)
        dear = self._offer(dear_vendor, self.contested, "PR-A", 200.0, 5)
        cheap = self._offer(cheap_vendor, self.contested, "PR-B", 100.0, 5)
        self.assertTrue(cheap.is_buybox_winner)
        cheap_score_before = cheap.buybox_score

        dear.price = 120.0          # only the sibling is edited

        self.assertLess(cheap.buybox_score, cheap_score_before,
                        "the untouched offer kept a stale score")
        self.assertTrue(cheap.is_buybox_winner)

    def test_a_price_cut_can_take_the_buy_box(self):
        first_vendor = self._vendor("Cut A", self.bronze)
        second_vendor = self._vendor("Cut B", self.bronze)
        first = self._offer(first_vendor, self.contested, "CUT-A", 100.0, 5)
        second = self._offer(second_vendor, self.contested, "CUT-B", 100.0, 5)

        second.price = 50.0

        self.assertGreater(second.buybox_score, first.buybox_score)
        self.assertTrue(second.is_buybox_winner)
        self.assertFalse(first.is_buybox_winner)

    # -------------------------------------------------- (c) stock freshness
    def test_the_winner_selling_out_hands_the_buy_box_over(self):
        winner_vendor = self._vendor("Sellout", self.platinum)
        backup_vendor = self._vendor("Backup", self.bronze)
        winner = self._offer(winner_vendor, self.contested, "SO-WIN", 100.0, 3)
        backup = self._offer(backup_vendor, self.contested, "SO-BAK", 100.0, 7)
        self.assertTrue(winner.is_buybox_winner)

        # Stock drops to zero. No offer is edited.
        self.env["stock.quant"]._leaders_adjust_vendor_stock(
            self.contested, winner_vendor.vendor_location_id, winner_vendor, -3)

        self.assertEqual(winner.available_qty(), 0.0)
        self.assertTrue(backup.is_buybox_winner, "the sold-out offer kept the Buy Box")
        self.assertFalse(winner.is_buybox_winner)

    def test_the_website_sells_the_in_stock_vendors_quantity(self):
        winner_vendor = self._vendor("Web Sellout", self.platinum)
        backup_vendor = self._vendor("Web Backup", self.bronze)
        self._offer(winner_vendor, self.contested, "WEB-WIN", 100.0, 3)
        self._offer(backup_vendor, self.contested, "WEB-BAK", 100.0, 7)
        website = self.env["website"].search([], limit=1)

        self.env["stock.quant"]._leaders_adjust_vendor_stock(
            self.contested, winner_vendor.vendor_location_id, winner_vendor, -3)

        self.assertEqual(website._get_product_available_qty(self.contested), 7.0)

    def test_a_stale_flag_is_stepped_over_at_read_time(self):
        """Even if nothing refreshed the flag, the website must not sell air."""
        winner_vendor = self._vendor("Stale", self.platinum)
        backup_vendor = self._vendor("Stale Backup", self.bronze)
        winner = self._offer(winner_vendor, self.contested, "STL-WIN", 100.0, 3)
        backup = self._offer(backup_vendor, self.contested, "STL-BAK", 100.0, 7)

        # Empty the stock behind the winner's back, then force the stale flag.
        self.env["stock.quant"]._leaders_adjust_vendor_stock(
            self.contested, winner_vendor.vendor_location_id, winner_vendor, -3)
        backup.is_buybox_winner = False
        winner.is_buybox_winner = True

        self.assertEqual(self.Offer._get_marketplace_winner(self.contested), backup)

    def test_the_cron_repairs_a_stale_winner(self):
        winner_vendor = self._vendor("Cron Sellout", self.platinum)
        backup_vendor = self._vendor("Cron Backup", self.bronze)
        winner = self._offer(winner_vendor, self.contested, "CR-WIN", 100.0, 3)
        backup = self._offer(backup_vendor, self.contested, "CR-BAK", 100.0, 7)

        self.env["stock.quant"]._leaders_adjust_vendor_stock(
            self.contested, winner_vendor.vendor_location_id, winner_vendor, -3)
        backup.is_buybox_winner = False
        winner.is_buybox_winner = True

        self.Offer.cron_refresh_buybox()
        self.assertTrue(backup.is_buybox_winner)
        self.assertFalse(winner.is_buybox_winner)

    def test_shipping_a_vendor_order_refreshes_the_buy_box(self):
        """A real delivery out of vendor stock, not a quant adjustment."""
        winner_vendor = self._vendor("Ship Sellout", self.platinum)
        backup_vendor = self._vendor("Ship Backup", self.bronze)
        winner = self._offer(winner_vendor, self.contested, "SH-WIN", 100.0, 2)
        backup = self._offer(backup_vendor, self.contested, "SH-BAK", 100.0, 9)
        self.assertTrue(winner.is_buybox_winner)

        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({
                "product_id": self.contested.id, "product_uom_qty": 2})],
        })
        order.action_confirm()
        group = order.vendor_order_ids.fulfillment_group_ids
        self.assertEqual(group.vendor_id, winner_vendor)
        group.tracking_ref = "SH-TRK"
        group.action_ship()

        self.assertEqual(winner.available_qty(), 0.0)
        self.assertTrue(backup.is_buybox_winner, "shipping did not refresh the Buy Box")

    # -------------------------------------------------- nothing else changed
    def test_the_winner_still_drives_the_website_price(self):
        cheap_vendor = self._vendor("Cheap", self.bronze)
        self._offer(cheap_vendor, self.contested, "LP-1", 42.0, 5)
        self.assertEqual(self.contested.product_tmpl_id.list_price, 42.0)

    def test_a_single_offer_product_keeps_its_winner(self):
        vendor = self._vendor("Only Seller", self.bronze)
        offer = self._offer(vendor, self.contested, "ONE-1", 100.0, 5)
        self.assertTrue(offer.is_buybox_winner)
        self.Offer.cron_refresh_buybox()
        self.assertTrue(offer.is_buybox_winner)
