"""Configuration that was stored but never read.

Every item here is off or empty by default, and an empty configuration must
behave exactly as the marketplace did before it was wired up.
"""
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from odoo import Command, fields
from odoo.exceptions import ValidationError
import types

from . import test_vendor_application as va
from odoo.tests import tagged

from .test_security_rpc import MarketplacePortalUserCommon


class DormantCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Offer = cls.env["leaders.marketplace.offer"]
        cls.Policy = cls.env["leaders.fulfillment.policy"]
        cls.website = cls.env["website"].search([], limit=1)

    def _set(self, key, value):
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.%s" % key, value)


# =============================================================== 1. markets
@tagged("post_install", "-at_install")
class TestMarkets(DormantCommon):

    def test_no_market_means_every_market(self):
        """The default: market_ids empty everywhere, so nothing is filtered."""
        self.assertFalse(self.fbm_offer.market_ids)
        self.assertEqual(
            self.Offer._get_marketplace_winner(self.fbm_product), self.fbm_offer)

    def test_an_offer_is_hidden_from_a_market_it_does_not_target(self):
        other = self.env["website"].create({"name": "Second Market"})
        self.fbm_offer.market_ids = [Command.set([other.id])]
        self.assertFalse(
            self.fbm_offer._leaders_filter_by_market(website=self.website))
        self.assertEqual(
            self.fbm_offer._leaders_filter_by_market(website=other), self.fbm_offer)

    def test_outside_a_request_nothing_is_filtered(self):
        """Crons and the back office must keep seeing every offer."""
        other = self.env["website"].create({"name": "Third Market"})
        self.fbm_offer.market_ids = [Command.set([other.id])]
        self.assertEqual(self.fbm_offer._leaders_filter_by_market(), self.fbm_offer)
        self.assertEqual(
            self.Offer._get_marketplace_winner(self.fbm_product), self.fbm_offer)

    def test_a_vendor_cannot_list_on_a_market_they_are_barred_from(self):
        allowed = self.env["website"].create({"name": "Allowed Market"})
        barred = self.env["website"].create({"name": "Barred Market"})
        self.vendor.vendor_market_ids = [Command.set([allowed.id])]
        self.fbm_offer.market_ids = [Command.set([allowed.id])]      # fine
        with self.assertRaises(ValidationError):
            self.fbm_offer.market_ids = [Command.set([barred.id])]

    def test_no_allowed_markets_means_no_restriction(self):
        """The default: vendor_market_ids empty, so any market is fine."""
        self.assertFalse(self.vendor.vendor_market_ids)
        anywhere = self.env["website"].create({"name": "Anywhere"})
        self.fbm_offer.market_ids = [Command.set([anywhere.id])]     # no raise
        self.assertEqual(self.fbm_offer.market_ids, anywhere)


# =================================================== 2. fulfillment policies
@tagged("post_install", "-at_install")
class TestFulfillmentPolicy(DormantCommon):

    def _new_offer(self, sku, fulfillment="fbm", categories=None):
        product = self.env["product.product"].create({
            "name": "Policy Product %s" % sku, "type": "consu", "is_storable": True,
            "taxes_id": [Command.clear()],
        })
        if categories:
            product.product_tmpl_id.public_categ_ids = [Command.set(categories.ids)]
        return self.Offer.create({
            "product_tmpl_id": product.product_tmpl_id.id,
            "product_id": product.id,
            "vendor_id": self.vendor.id,
            "seller_sku": sku,
            "price": 10.0,
            "fulfillment_type": fulfillment,
            "stock_location_id": self.vendor._marketplace_stock_location(fulfillment).id,
        })

    def test_no_policy_leaves_the_vendors_choice_alone(self):
        self.assertFalse(self.Policy.search([]))
        offer = self._new_offer("POL-NONE", "fbm")
        self.assertEqual(offer.fulfillment_type, "fbm")

    def test_a_vendor_policy_forces_the_type(self):
        self.Policy.create({
            "name": "This vendor is FBL", "vendor_id": self.vendor.id,
            "forced_fulfillment_type": "fbl",
        })
        offer = self._new_offer("POL-V", "fbm")
        self.assertEqual(offer.fulfillment_type, "fbl")
        self.assertEqual(
            offer.stock_location_id, self.vendor.vendor_fbl_location_id)

    def test_a_category_policy_forces_the_type(self):
        category = self.env["product.public.category"].create({"name": "Bulky"})
        self.Policy.create({
            "name": "Bulky is FBL", "category_id": category.id,
            "forced_fulfillment_type": "fbl",
        })
        offer = self._new_offer("POL-C", "fbm", categories=category)
        self.assertEqual(offer.fulfillment_type, "fbl")

    def test_vendor_plus_category_beats_vendor_alone(self):
        category = self.env["product.public.category"].create({"name": "Fragile"})
        self.Policy.create({
            "name": "Vendor is FBL", "vendor_id": self.vendor.id,
            "forced_fulfillment_type": "fbl",
        })
        self.Policy.create({
            "name": "This vendor's fragile goods are FBM",
            "vendor_id": self.vendor.id, "category_id": category.id,
            "forced_fulfillment_type": "fbm",
        })
        offer = self._new_offer("POL-VC", "fbl", categories=category)
        self.assertEqual(offer.fulfillment_type, "fbm", "the specific policy lost")

    def test_an_inactive_policy_is_ignored(self):
        self.Policy.create({
            "name": "Retired policy", "vendor_id": self.vendor.id,
            "forced_fulfillment_type": "fbl", "active": False,
        })
        offer = self._new_offer("POL-OFF", "fbm")
        self.assertEqual(offer.fulfillment_type, "fbm")

    def test_editing_an_offer_re_applies_the_policy(self):
        self.Policy.create({
            "name": "Always FBL", "vendor_id": self.vendor.id,
            "forced_fulfillment_type": "fbl",
        })
        offer = self._new_offer("POL-EDIT", "fbm")
        offer.write({"fulfillment_type": "fbm"})      # the vendor tries again
        self.assertEqual(offer.fulfillment_type, "fbl")


# ============================================== 3. automatic draft settlements
@tagged("post_install", "-at_install")
class TestAutoSettlements(DormantCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Settlement = cls.env["leaders.settlement"]
        cls.tier = cls.env["leaders.vendor.tier"].create({
            "name": "Daily Payout", "code": "daily", "sequence": 12,
            "payout_frequency": "daily",
        })
        cls.vendor.vendor_tier_id = cls.tier

    def _earn(self, on_date):
        return self.env["leaders.wallet.transaction"].create({
            "vendor_id": self.vendor.id, "type": "earning", "amount": 500.0,
            "currency_id": self.company.currency_id.id,
            "available_date": on_date, "state": "available",
        })

    def _settlements(self):
        return self.Settlement.search([("vendor_id", "=", self.vendor.id)])

    def test_off_by_default(self):
        self._earn(fields.Date.today() - timedelta(days=1))
        self.assertFalse(self.Settlement.cron_auto_create_settlements())
        self.assertFalse(self._settlements())

    def test_enabled_it_opens_a_draft_for_yesterday(self):
        self._set("auto_settlements", True)
        yesterday = fields.Date.today() - timedelta(days=1)
        self._earn(yesterday)

        self.Settlement.cron_auto_create_settlements()

        settlement = self._settlements()
        self.assertEqual(len(settlement), 1)
        self.assertEqual(settlement.state, "draft", "approval must stay manual")
        self.assertEqual(settlement.period_from, yesterday)
        self.assertEqual(settlement.period_to, yesterday)
        self.assertEqual(settlement.net_amount, 500.0)

    def test_nothing_to_collect_opens_nothing(self):
        self._set("auto_settlements", True)
        self.Settlement.cron_auto_create_settlements()
        self.assertFalse(self._settlements())

    def test_an_overlapping_settlement_blocks_a_second_one(self):
        self._set("auto_settlements", True)
        yesterday = fields.Date.today() - timedelta(days=1)
        self._earn(yesterday)
        self.Settlement.cron_auto_create_settlements()
        self._earn(yesterday)
        self.Settlement.cron_auto_create_settlements()
        self.assertEqual(len(self._settlements()), 1)

    def test_a_monthly_tier_only_fires_on_the_first(self):
        self._set("auto_settlements", True)
        self.tier.payout_frequency = "monthly"
        first = date(2026, 5, 1)
        self.assertTrue(self.Settlement._leaders_period_for("monthly", first))
        self.assertFalse(self.Settlement._leaders_period_for("monthly", date(2026, 5, 17)))
        self.assertEqual(
            self.Settlement._leaders_period_for("monthly", first),
            (date(2026, 4, 1), date(2026, 4, 30)))

    def test_the_weekly_period_is_the_week_that_just_closed(self):
        monday = date(2026, 5, 4)
        self.assertEqual(monday.weekday(), 0)
        self.assertEqual(
            self.Settlement._leaders_period_for("weekly", monday),
            (date(2026, 4, 27), date(2026, 5, 3)))
        self.assertFalse(self.Settlement._leaders_period_for("weekly", date(2026, 5, 6)))

    def test_the_biweekly_periods_split_the_month(self):
        self.assertEqual(
            self.Settlement._leaders_period_for("biweekly", date(2026, 5, 16)),
            (date(2026, 5, 1), date(2026, 5, 15)))
        self.assertEqual(
            self.Settlement._leaders_period_for("biweekly", date(2026, 5, 1)),
            (date(2026, 4, 16), date(2026, 4, 30)))
        self.assertFalse(self.Settlement._leaders_period_for("biweekly", date(2026, 5, 9)))


# ================================================= 4. automatic tier upgrades
@tagged("post_install", "-at_install")
class TestAutoTierUpgrade(DormantCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Tier = cls.env["leaders.vendor.tier"]
        cls.bronze = cls.env.ref("leaders_marketplace.vendor_tier_bronze")
        cls.silver = cls.env.ref("leaders_marketplace.vendor_tier_silver")
        cls.vendor.write({
            "vendor_tier_id": cls.bronze.id,
            "vendor_approved_date": fields.Date.today() - relativedelta(years=3),
        })
        # No threshold is touched here: each test sets the ones it is about, so
        # the shipped values stay observable.

    def test_off_by_default(self):
        self.assertFalse(self.Tier.cron_auto_upgrade_tiers())
        self.assertEqual(self.vendor.vendor_tier_id, self.bronze)

    def test_enabled_it_moves_the_vendor_up(self):
        self._set("auto_tier_upgrade", True)
        self.Tier.cron_auto_upgrade_tiers()
        self.assertGreater(self.vendor.vendor_tier_id.sequence, self.bronze.sequence)
        self.assertTrue(any("tier upgraded" in body.lower()
                            for body in self.vendor.message_ids.mapped("body")))

    def test_an_unmet_threshold_leaves_the_tier_alone(self):
        self._set("auto_tier_upgrade", True)
        self.env["leaders.vendor.tier"].search([]).write({"min_months": 600})
        self.bronze.min_months = 0
        self.Tier.cron_auto_upgrade_tiers()
        self.assertEqual(self.vendor.vendor_tier_id, self.bronze)

    def test_it_never_moves_a_vendor_down(self):
        self._set("auto_tier_upgrade", True)
        platinum = self.env.ref("leaders_marketplace.vendor_tier_platinum")
        platinum.write({"min_months": 0, "min_sales": 0.0, "min_rating": 0.0})
        self.vendor.vendor_tier_id = platinum
        self.silver.min_months = 0
        self.Tier.cron_auto_upgrade_tiers()
        self.assertEqual(self.vendor.vendor_tier_id, platinum)

    def test_a_vendor_with_no_approval_date_counts_as_new(self):
        self._set("auto_tier_upgrade", True)
        self.vendor.vendor_approved_date = False
        # Every tier above bronze needs seniority, so a vendor with no known
        # approval date (zero months) stays where they are.
        self.Tier.search([("sequence", ">", self.bronze.sequence)]).write(
            {"min_months": 6})
        self.Tier.cron_auto_upgrade_tiers()
        self.assertEqual(self.vendor.vendor_tier_id, self.bronze)

    def test_seniority_actually_gates_the_upgrade(self):
        self._set("auto_tier_upgrade", True)
        self.Tier.search([("sequence", ">", self.bronze.sequence)]).write(
            {"min_months": 24})
        self.vendor.vendor_approved_date = fields.Date.today() - relativedelta(months=3)
        self.Tier.cron_auto_upgrade_tiers()
        self.assertEqual(self.vendor.vendor_tier_id, self.bronze)

        self.vendor.vendor_approved_date = fields.Date.today() - relativedelta(months=30)
        self.Tier.cron_auto_upgrade_tiers()
        self.assertGreater(self.vendor.vendor_tier_id.sequence, self.bronze.sequence)

    def test_sales_actually_gate_the_upgrade(self):
        self._set("auto_tier_upgrade", True)
        self.Tier.search([("sequence", ">", self.bronze.sequence)]).write(
            {"min_months": 0, "min_sales": 10000.0})
        self.Tier.cron_auto_upgrade_tiers()
        self.assertEqual(self.vendor.vendor_tier_id, self.bronze,
                         "a vendor with no sales was promoted")

    def test_the_shipped_tiers_carry_no_thresholds(self):
        """Documents a trap for whoever switches this on.

        data/vendor_tier_data.xml sets no min_months / min_sales / min_rating,
        so out of the box every threshold is zero and the first cron run would
        move every vendor to the highest tier -- and therefore to its commission
        rate. Thresholds must be filled in before the setting is enabled.
        """
        for tier in self.Tier.search([]):
            with self.subTest(tier=tier.code):
                self.assertEqual(
                    (tier.min_months, tier.min_sales, tier.min_rating), (0, 0.0, 0.0),
                    "a shipped tier gained a threshold; update this note")

    def test_approving_an_application_stamps_the_date(self):
        # Its own application, so the result never depends on what the database holds.
        self.category = self.env["product.public.category"].create({"name": "Stamp Category"})
        self._doc = types.MethodType(va.TestVendorApplication._doc, self)
        application = va.TestVendorApplication._application(self, owner_email="stamp.owner@example.com")
        application.action_submit()
        application.action_start_review()
        application.document_ids.action_verify()
        application.action_approve()
        self.assertTrue(application.partner_id.vendor_approved_date)


# ============================ 5. the commission recognition switch does nothing
@tagged("post_install", "-at_install")
class TestCommissionRecognitionHidden(DormantCommon):

    def test_the_field_is_kept_on_the_model(self):
        self.assertIn(
            "marketplace_commission_recognition",
            self.env["res.config.settings"]._fields)

    def test_it_is_gone_from_the_settings_screen(self):
        view = self.env.ref("leaders_marketplace.res_config_settings_view_form_marketplace")
        self.assertNotIn("marketplace_commission_recognition", view.arch_db)
