"""B1: vendor-funded product discounts."""
import importlib.util
import re
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules import get_module_path
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD
from .test_vendor_split import MarketplaceVendorCommon

PARAM = "leaders_marketplace.vendor_discounts_enabled"
THRESHOLD = "leaders_marketplace.max_vendor_discount_without_approval"


class DiscountCommon(MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Offer = cls.env["leaders.marketplace.offer"]
        cls.param = cls.env["ir.config_parameter"].sudo()
        cls.param.set_param(PARAM, "True")
        cls.param.set_param(THRESHOLD, "50")

    def _later(self, **kw):
        return fields.Datetime.now() + timedelta(**kw)

    def _cart(self, qty=1):
        """A cart whose line already belongs to the offer, as a website cart line does."""
        order = self._order([(self.fbm_product, qty)])
        order.order_line.marketplace_offer_id = self.fbm_offer
        return order

    def _second_offer(self, price, name="Rival Vendor", sku="RIVAL-1"):
        vendor = self.env["res.partner"].create({
            "name": name, "is_marketplace_vendor": True, "vendor_state": "approved"})
        vendor._marketplace_ensure_vendor_stock()
        offer = self.Offer.create({
            "product_tmpl_id": self.fbm_product.product_tmpl_id.id,
            "product_id": self.fbm_product.id, "vendor_id": vendor.id, "seller_sku": sku,
            "price": price, "fulfillment_type": "fbm",
            "stock_location_id": vendor._marketplace_stock_location("fbm").id,
        })
        self.env["stock.quant"]._leaders_adjust_vendor_stock(
            self.fbm_product, vendor.vendor_location_id, vendor, 5)
        offer.action_approve()
        return offer


@tagged("post_install", "-at_install")
class TestVendorDiscountRules(DiscountCommon):

    def test_percent_discount_sets_the_effective_price_and_keeps_the_regular_one(self):
        state = self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})
        self.assertEqual(state, "active")
        self.assertEqual(self.fbm_offer.price, 100.0)
        self.assertEqual(self.fbm_offer.effective_price, 80.0)
        self.assertAlmostEqual(self.fbm_offer._leaders_active_discount_percent(), 20.0)

    def test_sale_price_discount(self):
        self.fbm_offer._leaders_set_discount({"type": "price", "price": 75})
        self.assertEqual(self.fbm_offer.effective_price, 75.0)
        self.assertAlmostEqual(self.fbm_offer.discount_regular_percent, 25.0)

    def test_invalid_discounts_are_refused_and_write_nothing(self):
        offer = self.fbm_offer
        bad = [
            {"type": "percent", "percent": 0}, {"type": "percent", "percent": 100},
            {"type": "percent", "percent": "abc"}, {"type": "price", "price": 100},
            {"type": "price", "price": 0}, {"type": "price", "price": 120}, {"type": "other"},
            {"type": "percent", "percent": 10, "start": self._later(days=2), "end": self._later(days=1)},
            {"type": "percent", "percent": 10, "end": self._later(days=-1)},
        ]
        for values in bad:
            with self.subTest(values=values), self.assertRaises(UserError):
                offer._leaders_set_discount(values)
        self.assertEqual(offer.discount_state, "none")
        self.assertFalse(offer.discount_type)

    def test_master_switch_off_refuses_vendor_discounts_but_keeps_existing_ones(self):
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})
        self.param.set_param(PARAM, False)
        with self.assertRaises(UserError):
            self.fbl_offer._leaders_set_discount({"type": "percent", "percent": 10})
        self.assertEqual(self.fbm_offer.effective_price, 80.0)
        self.fbm_offer._leaders_clear_discount()          # removing stays possible
        self.assertEqual(self.fbm_offer.effective_price, 100.0)

    def test_the_switch_is_off_by_default(self):
        self.param.search([("key", "=", PARAM)]).unlink()
        self.assertFalse(self.Offer._leaders_discounts_enabled())

    def test_clear_discount(self):
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})
        self.fbm_offer._leaders_clear_discount()
        self.assertEqual(self.fbm_offer.discount_state, "none")
        self.assertEqual(self.fbm_offer.effective_price, 100.0)
        self.assertFalse(self.fbm_offer.discount_type)

    def test_a_sale_price_no_longer_below_the_regular_price_is_void(self):
        self.fbm_offer._leaders_set_discount({"type": "price", "price": 75})
        self.fbm_offer.price = 70.0
        self.assertEqual(self.fbm_offer.discount_state, "none")
        self.assertEqual(self.fbm_offer.effective_price, 70.0)

    # ---------------------------------------------------------- state machine
    def test_scheduled_discount_starts_and_ends_with_the_cron(self):
        offer = self.fbm_offer
        self.assertEqual(offer._leaders_set_discount(
            {"type": "percent", "percent": 20, "start": self._later(hours=1),
             "end": self._later(hours=3)}), "scheduled")
        self.assertEqual(offer.effective_price, 100.0)
        self.assertEqual(self.Offer.cron_refresh_discounts(), 0)       # nothing due: no-op
        offer.discount_start = self._later(hours=-2)
        self.assertEqual(self.Offer.cron_refresh_discounts(), 1)
        self.assertEqual(offer.discount_state, "active")
        self.assertEqual(offer.effective_price, 80.0)
        offer.discount_end = self._later(minutes=-1)
        self.assertEqual(self.Offer.cron_refresh_discounts(), 1)
        self.assertEqual(offer.discount_state, "expired")
        self.assertEqual(offer.effective_price, 100.0)
        self.assertEqual(self.Offer.cron_refresh_discounts(), 0)

    def test_the_cron_is_scheduled_every_15_minutes(self):
        cron = self.env.ref("leaders_marketplace.cron_refresh_offer_discounts")
        self.assertEqual((cron.interval_number, cron.interval_type), (15, "minutes"))

    # --------------------------------------------------------------- approval
    def test_a_discount_above_the_threshold_waits_for_a_reviewer(self):
        offer = self.fbm_offer
        self.assertEqual(offer._leaders_set_discount({"type": "percent", "percent": 60}), "pending_approval")
        self.assertEqual(offer.effective_price, 100.0)
        offer.action_approve_discount()
        self.assertEqual(offer.discount_state, "active")
        self.assertEqual(offer.effective_price, 40.0)

    def test_the_threshold_is_the_setting_and_50_percent_is_allowed(self):
        self.assertEqual(self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 50}), "active")
        self.param.set_param(THRESHOLD, "10")
        self.assertEqual(self.fbl_offer._leaders_set_discount({"type": "percent", "percent": 20}), "pending_approval")
        self.param.search([("key", "=", THRESHOLD)]).unlink()
        self.assertEqual(self.Offer._leaders_discount_threshold(), 50.0)

    def test_sale_price_is_checked_by_its_equivalent_percentage(self):
        self.assertEqual(self.fbm_offer._leaders_set_discount({"type": "price", "price": 30}), "pending_approval")

    def test_rejection_and_reapproval_rules(self):
        offer = self.fbm_offer
        offer._leaders_set_discount({"type": "percent", "percent": 60})
        offer.action_reject_discount()
        self.assertEqual(offer.discount_state, "rejected")
        self.assertEqual(offer.effective_price, 100.0)
        offer.action_approve_discount()                        # only pending ones
        self.assertEqual(offer.discount_state, "rejected")
        offer._leaders_set_discount({"type": "percent", "percent": 30})   # vendor tries again
        self.assertEqual(offer.discount_state, "active")

    def test_raising_the_price_of_an_approved_sale_price_asks_for_review_again(self):
        offer = self.fbm_offer
        offer._leaders_set_discount({"type": "price", "price": 60})       # 40%: no approval
        self.assertEqual(offer.discount_state, "active")
        offer.price = 200.0                                               # now 70%
        self.assertEqual(offer.discount_state, "pending_approval")
        self.assertEqual(offer.effective_price, 200.0)

    def test_only_reviewers_approve_discounts(self):
        offer = self.fbm_offer
        offer._leaders_set_discount({"type": "percent", "percent": 60})
        user = self.env["res.users"].create({
            "name": "Plain Internal", "login": "lm.plain.internal@example.com",
            "groups_id": [Command.set([self.env.ref("base.group_user").id])]})
        with self.assertRaises(AccessError):
            offer.with_user(user).action_approve_discount()
        reviewer = self.env["res.users"].create({
            "name": "Disc Reviewer", "login": "lm.disc.reviewer@example.com",
            "groups_id": [Command.set([
                self.env.ref("base.group_user").id,
                self.env.ref("leaders_marketplace.group_marketplace_reviewer").id])]})
        offer.with_user(reviewer).action_approve_discount()
        self.assertEqual(offer.discount_state, "active")

    def test_the_review_menu_and_action(self):
        self.assertTrue(self.env.ref("leaders_marketplace.menu_offer_discounts_to_review"))
        action = self.env.ref("leaders_marketplace.action_offer_discounts_to_review")
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 60})
        self.assertIn(self.fbm_offer, self.Offer.search(eval(action.domain)))


@tagged("post_install", "-at_install")
class TestOfferPriceHistory(DiscountCommon):

    def _age_history(self, offer, days):
        offer.price_history_ids.sudo().write({"date": fields.Datetime.now() - timedelta(days=days)})

    def test_history_is_written_on_create_and_on_a_real_price_change_only(self):
        offer = self.fbm_offer
        self.assertEqual(offer.price_history_ids.mapped("price"), [100.0])
        offer.write({"price": 100.0})
        self.assertEqual(len(offer.price_history_ids), 1)
        offer.price = 90.0
        self.assertEqual(sorted(offer.price_history_ids.mapped("price")), [90.0, 100.0])

    def test_reference_price_is_the_lowest_of_the_last_30_days(self):
        offer = self.fbm_offer
        self._age_history(offer, 45)
        offer.price = 80.0
        offer.price_history_ids.filtered(lambda h: h.price == 80.0).write(
            {"date": fields.Datetime.now() - timedelta(days=10)})
        offer.price = 120.0
        self.assertEqual(offer._leaders_reference_price(), 80.0)
        # The 100 that was in force when the window opened counts, so does 120 now.
        self._age_history(offer, 45)
        offer.price_history_ids.filtered(lambda h: h.price == 120.0).write({"date": fields.Datetime.now()})
        self.assertEqual(offer._leaders_reference_price(), 80.0)

    def test_a_price_raised_before_a_promotion_earns_no_badge(self):
        offer = self.fbm_offer
        self._age_history(offer, 40)            # 100 for a long time
        offer.price = 200.0                     # raised yesterday-ish
        offer._leaders_set_discount({"type": "percent", "percent": 30})   # 140 "promotion"
        self.assertEqual(offer.effective_price, 140.0)
        self.assertEqual(offer._leaders_reference_price(), 100.0)
        self.assertEqual(offer._leaders_display_discount(), {})

    def test_a_real_promotion_shows_the_percentage_from_the_reference(self):
        offer = self.fbm_offer
        self._age_history(offer, 40)
        offer.price = 150.0
        offer._leaders_set_discount({"type": "price", "price": 75})       # 50% of 150, 25% under 100
        display = offer._leaders_display_discount()
        self.assertEqual(display["reference"], 100.0)
        self.assertEqual(display["percent"], 25)

    def test_no_display_without_a_running_discount(self):
        self.assertEqual(self.fbm_offer._leaders_display_discount(), {})
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20, "start": self._later(days=1)})
        self.assertEqual(self.fbm_offer._leaders_display_discount(), {})

    def test_the_migration_seeds_offers_without_history(self):
        path = get_module_path("leaders_marketplace") + "/migrations/18.0.4.0.55/post-B1-price-history-seed.py"
        spec = importlib.util.spec_from_file_location("post_b1_seed", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.env.cr.execute("DELETE FROM leaders_marketplace_offer_price_history WHERE offer_id = %s",
                            [self.fbm_offer.id])
        self.env.invalidate_all()
        module.migrate(self.env.cr, "18.0.4.0.54")
        module.migrate(self.env.cr, "18.0.4.0.54")          # idempotent
        self.assertEqual(self.fbm_offer.price_history_ids.mapped("price"), [100.0])


@tagged("post_install", "-at_install")
class TestDiscountEffects(DiscountCommon):

    def test_buy_box_ranks_on_the_effective_price(self):
        rival = self._second_offer(90.0)
        self.assertTrue(rival.is_buybox_winner)
        self.assertFalse(self.fbm_offer.is_buybox_winner)
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})     # 80 < 90
        self.assertTrue(self.fbm_offer.is_buybox_winner)
        self.assertFalse(rival.is_buybox_winner)
        # list_price stays the REGULAR price of the winner (lines are priced from it).
        self.assertEqual(self.fbm_product.product_tmpl_id.list_price, 100.0)
        self.fbm_offer._leaders_clear_discount()
        self.assertTrue(rival.is_buybox_winner)
        self.assertEqual(self.fbm_product.product_tmpl_id.list_price, 90.0)

    def test_the_cron_re_ranks_the_buy_box_when_a_discount_ends(self):
        rival = self._second_offer(90.0)
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20, "end": self._later(hours=1)})
        self.assertTrue(self.fbm_offer.is_buybox_winner)
        self.fbm_offer.discount_end = self._later(minutes=-1)
        self.Offer.cron_refresh_discounts()
        self.assertTrue(rival.is_buybox_winner)

    def test_website_price_info_uses_the_effective_price_and_the_reference(self):
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})
        website = self.env["website"].search([], limit=1)
        template = self.fbm_product.product_tmpl_id
        info = template._get_additionnal_combination_info(
            self.fbm_product, 1, fields.Date.today(), website)
        self.assertTrue(info["has_discounted_price"])
        self.assertAlmostEqual(info["price"] / info["list_price"], 0.8, places=3)
        self.assertEqual(info["leaders_discount_percent"], 20)

    def test_the_cart_line_keeps_the_regular_price_and_carries_the_discount(self):
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})
        order = self._cart(2)
        line = order.order_line
        self.assertEqual(line.marketplace_offer_id, self.fbm_offer)
        self.assertEqual(line.price_unit, 100.0)
        self.assertAlmostEqual(line.discount, 20.0)
        self.assertAlmostEqual(line.price_subtotal, 160.0)

    def test_the_vendor_order_and_invoice_show_the_original_price_and_the_discount(self):
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})
        order = self._order([(self.fbm_product, 2)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        line = vendor_order.line_ids.filtered(lambda l: not l.is_delivery)
        self.assertEqual(line.price_unit, 100.0)
        self.assertAlmostEqual(line.discount, 20.0)
        self.assertAlmostEqual(line.price_subtotal, 160.0)
        invoice = self.env["leaders.vendor.invoice"]._create_for_vendor_order(vendor_order)
        inv_line = invoice.line_ids
        self.assertEqual(inv_line.price_unit, 100.0)
        self.assertAlmostEqual(inv_line.discount, 20.0)
        self.assertAlmostEqual(inv_line.price_subtotal, 160.0)

    def test_commission_achievement_uses_the_discounted_amount(self):
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})
        order = self._order([(self.fbm_product, 4)])
        order.action_confirm()
        plan = self.env["leaders.commission.rule"].create({
            "name": "Discount amount plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "amount_sold", "achieve_rate": 0.10,
        })
        today = fields.Date.today()
        self.assertAlmostEqual(
            plan._achieve_metric_value(self.vendor, order.vendor_order_ids, today, today), 320.0)

    def test_a_discount_that_ended_leaves_no_discount_on_an_open_cart_line(self):
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20, "end": self._later(hours=1)})
        order = self._cart()
        self.assertAlmostEqual(order.order_line.discount, 20.0)
        self.fbm_offer.discount_end = self._later(minutes=-1)
        self.Offer.cron_refresh_discounts()
        self.assertEqual(order.order_line.discount, 0.0)
        self.assertEqual(order.order_line.price_unit, 100.0)

    def test_checkout_revalidation_notices_a_discount_change(self):
        order = self._cart()
        self.assertFalse(order._leaders_revalidate_marketplace_prices())
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})
        self.assertTrue(order._leaders_revalidate_marketplace_prices())
        self.assertAlmostEqual(order.order_line.discount, 20.0)

    def test_without_a_discount_nothing_changes(self):
        order = self._order([(self.fbm_product, 1)])
        self.assertEqual(order.order_line.price_unit, 100.0)
        self.assertEqual(order.order_line.discount, 0.0)


@tagged("post_install", "-at_install")
class TestVendorDiscountPortal(HttpCase, MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.param = cls.env["ir.config_parameter"].sudo()
        cls.param.set_param(PARAM, "True")
        cls.param.set_param(THRESHOLD, "50")
        cls.other_vendor = cls.env["res.partner"].create({
            "name": "Foreign Vendor", "is_marketplace_vendor": True, "vendor_state": "approved"})
        cls.other_vendor._marketplace_ensure_vendor_stock()
        cls.foreign_offer = cls.env["leaders.marketplace.offer"].create({
            "product_tmpl_id": cls.own_product.product_tmpl_id.id, "product_id": cls.own_product.id,
            "vendor_id": cls.other_vendor.id, "seller_sku": "FOREIGN-1", "price": 50.0,
            "fulfillment_type": "fbm",
            "stock_location_id": cls.other_vendor._marketplace_stock_location("fbm").id})

    def setUp(self):
        super().setUp()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)

    def _edit_url(self, offer):
        return "/my/vendor/products/%s/edit" % offer.id

    def test_owner_sets_and_removes_a_discount(self):
        offer = self.fbm_offer
        self._post("/my/vendor/products/%s/discount" % offer.id, self._edit_url(offer),
                   action="set", discount_type="percent", discount_percent="25")
        self.assertEqual(offer.discount_state, "active")
        self.assertEqual(offer.effective_price, 75.0)
        self._post("/my/vendor/products/%s/discount" % offer.id, self._edit_url(offer),
                   action="set", discount_type="price", discount_price="60",
                   discount_start="2999-01-01T10:00")
        self.assertEqual(offer.discount_state, "scheduled")
        self.assertEqual(offer.discount_price, 60.0)
        self._post("/my/vendor/products/%s/discount" % offer.id, self._edit_url(offer), action="clear")
        self.assertEqual(offer.discount_state, "none")

    def test_a_big_discount_from_the_portal_waits_for_approval(self):
        offer = self.fbm_offer
        self._post("/my/vendor/products/%s/discount" % offer.id, self._edit_url(offer),
                   action="set", discount_type="percent", discount_percent="70")
        self.assertEqual(offer.discount_state, "pending_approval")
        self.assertEqual(offer.effective_price, 100.0)

    def test_an_invalid_discount_shows_an_error_and_writes_nothing(self):
        offer = self.fbm_offer
        response = self._post("/my/vendor/products/%s/discount" % offer.id, self._edit_url(offer),
                              action="set", discount_type="price", discount_price="150")
        self.assertIn("below the regular price", response.text)
        self.assertEqual(offer.discount_state, "none")

    def test_switch_off_hides_the_form_and_refuses_the_route(self):
        self.param.set_param(PARAM, False)
        offer = self.fbm_offer
        self.assertNotIn("lm_discount_card", self.url_open(self._edit_url(offer)).text)
        self.assertNotIn("lm_bulk_discount_form", self.url_open("/my/vendor/products").text)
        response = self._post("/my/vendor/products/%s/discount" % offer.id, self._edit_url(offer),
                              action="set", discount_type="percent", discount_percent="10")
        self.assertIn("not enabled", response.text)
        self.assertEqual(offer.discount_state, "none")

    def test_switch_on_shows_the_form_and_the_bulk_box(self):
        self.assertIn("lm_discount_card", self.url_open(self._edit_url(self.fbm_offer)).text)
        self.assertIn("lm_bulk_discount_form", self.url_open("/my/vendor/products").text)

    def test_another_vendors_offer_cannot_be_discounted(self):
        token = self._csrf("/my/vendor/products")
        response = self.url_open("/my/vendor/products/%s/discount" % self.foreign_offer.id, data={
            "csrf_token": token, "action": "set", "discount_type": "percent", "discount_percent": "10"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.foreign_offer.discount_state, "none")

    def test_bulk_apply_and_remove(self):
        offers = self.fbm_offer | self.fbl_offer
        self._post("/my/vendor/products/discount-bulk", "/my/vendor/products",
                   action="apply", offer_ids=[str(i) for i in offers.ids], discount_percent="10")
        self.assertEqual(offers.mapped("discount_state"), ["active", "active"])
        self.assertEqual(self.fbm_offer.effective_price, 90.0)
        self.assertEqual(self.fbl_offer.effective_price, 36.0)
        self._post("/my/vendor/products/discount-bulk", "/my/vendor/products",
                   action="clear", offer_ids=[str(i) for i in offers.ids])
        self.assertEqual(offers.mapped("discount_state"), ["none", "none"])

    def test_bulk_with_a_foreign_offer_is_refused_entirely(self):
        token = self._csrf("/my/vendor/products")
        response = self.url_open("/my/vendor/products/discount-bulk", data={
            "csrf_token": token, "action": "apply", "discount_percent": "10",
            "offer_ids": [str(self.fbm_offer.id), str(self.foreign_offer.id)]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.fbm_offer.discount_state, "none")

    def test_bulk_needs_a_selection(self):
        response = self._post("/my/vendor/products/discount-bulk", "/my/vendor/products",
                              action="apply", discount_percent="10")
        self.assertIn("Select at least one offer", response.text)


@tagged("post_install", "-at_install")
class TestDiscountWebsite(HttpCase, DiscountCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmpl = cls.fbm_product.product_tmpl_id
        cls.tmpl.write({"marketplace_state": "published", "is_published": True, "sale_ok": True})

    def test_product_page_strikes_the_original_price_and_shows_the_badge(self):
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})
        page = self.url_open(self.tmpl.website_url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("lm-discount-badge", page.text)
        self.assertIn("-20%", page.text)
        struck = re.search(r'<span[^>]*oe_default_price[^>]*>', page.text)
        self.assertTrue(struck)
        self.assertNotIn("d-none", struck.group(0))

    def test_product_page_without_a_discount_has_no_badge(self):
        page = self.url_open(self.tmpl.website_url)
        self.assertNotIn("lm-discount-badge", page.text)

    def test_shop_grid_shows_the_badge(self):
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})
        page = self.url_open("/shop?search=Vendor+FBM+Product")
        self.assertEqual(page.status_code, 200)
        self.assertIn("lm-discount-badge", page.text)
        self.assertIn("-20%", page.text)

    def test_cart_shows_the_discount(self):
        self.fbm_offer._leaders_set_discount({"type": "percent", "percent": 20})
        page = self.url_open(self.tmpl.website_url)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text).group(1)
        self.url_open("/shop/cart/update", data={
            "csrf_token": token, "product_id": self.fbm_product.id, "add_qty": 1})
        order = self.env["sale.order"].sudo().search([("website_id", "!=", False)], order="id desc", limit=1)
        line = order.order_line.filtered(lambda l: l.product_id == self.fbm_product)
        self.assertEqual(line.price_unit, 100.0)
        self.assertAlmostEqual(line.discount, 20.0)
        cart = self.url_open("/shop/cart")
        self.assertEqual(cart.status_code, 200)
        self.assertIn("-20%", cart.text)
