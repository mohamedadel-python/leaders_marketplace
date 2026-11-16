"""Optional fallback commission for vendors with no commission plan.

Historically a vendor not covered by a plan paid nothing at settlement, even
though their vendor tier advertises a rate. The default keeps that exactly;
switching ``leaders_marketplace.no_plan_commission_mode`` to ``rate`` charges
the tier percentage on the same netted sales the plans are judged on.
"""
from odoo import Command, fields
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD


class NoPlanCommissionCommon(MarketplacePortalUserCommon):

    def _set_mode(self, mode, default_rate=None):
        param = self.env["ir.config_parameter"].sudo()
        param.set_param("leaders_marketplace.no_plan_commission_mode", mode)
        if default_rate is not None:
            param.set_param("leaders_marketplace.default_commission_rate", default_rate)

    def _earn(self, amount, on_date=None):
        return self.env["leaders.wallet.transaction"].create({
            "vendor_id": self.vendor.id,
            "type": "earning",
            "amount": amount,
            "currency_id": self.company.currency_id.id,
            "available_date": on_date or fields.Date.today(),
            "state": "available",
        })

    def _give_back(self, amount, on_date=None):
        return self.env["leaders.wallet.transaction"].create({
            "vendor_id": self.vendor.id,
            "type": "return",
            "amount": -abs(amount),
            "currency_id": self.company.currency_id.id,
            "available_date": on_date or fields.Date.today(),
            "state": "available",
        })

    def _settle(self):
        today = fields.Date.today()
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": today, "period_to": today,
            "currency_id": self.company.currency_id.id,
        })
        settlement.action_collect_transactions()
        return settlement

    @staticmethod
    def _commission(settlement):
        return settlement.transaction_ids.filtered(lambda t: t.type == "commission")


@tagged("post_install", "-at_install")
class TestNoPlanCommission(NoPlanCommissionCommon):

    def test_default_mode_is_none(self):
        self.assertEqual(
            self.env["leaders.settlement"]._leaders_no_plan_commission_mode(), "none")

    def test_mode_none_charges_a_planless_vendor_nothing(self):
        """Today's behavior, and the default."""
        self.vendor.vendor_tier_id = self.env.ref("leaders_marketplace.vendor_tier_bronze")
        self._earn(1000.0)
        settlement = self._settle()
        self.assertFalse(self._commission(settlement))
        self.assertEqual(settlement.net_amount, 1000.0)

    def test_mode_rate_charges_the_bronze_tier_rate(self):
        self._set_mode("rate")
        self.vendor.vendor_tier_id = self.env.ref("leaders_marketplace.vendor_tier_bronze")
        self._earn(1000.0)
        settlement = self._settle()
        commission = self._commission(settlement)
        self.assertEqual(len(commission), 1)
        self.assertEqual(commission.amount, -150.0)          # 15% of 1000
        self.assertEqual(settlement.net_amount, 850.0)
        self.assertIn("15.00%", commission.note)

    def test_mode_rate_follows_the_vendor_tier(self):
        self._set_mode("rate")
        self.vendor.vendor_tier_id = self.env.ref("leaders_marketplace.vendor_tier_gold")
        rate = self.vendor.vendor_tier_id.commission_rate
        self._earn(1000.0)
        self.assertEqual(self._commission(self._settle()).amount, -1000.0 * rate / 100.0)

    def test_mode_rate_falls_back_to_the_default_rate_without_a_tier(self):
        self._set_mode("rate", default_rate=7.5)
        self.vendor.vendor_tier_id = False
        self._earn(1000.0)
        self.assertEqual(self._commission(self._settle()).amount, -75.0)

    def test_mode_rate_nets_returns_like_the_plans_do(self):
        self._set_mode("rate")
        self.vendor.vendor_tier_id = self.env.ref("leaders_marketplace.vendor_tier_bronze")
        self._earn(1000.0)
        self._give_back(200.0)
        self.assertEqual(self._commission(self._settle()).amount, -120.0)   # 15% of 800

    def test_mode_rate_never_pays_the_vendor_on_a_negative_period(self):
        """Deduction only, like every commission plan."""
        self._set_mode("rate")
        self.vendor.vendor_tier_id = self.env.ref("leaders_marketplace.vendor_tier_bronze")
        self._earn(100.0)
        self._give_back(300.0)
        self.assertFalse(self._commission(self._settle()))

    def test_zero_rate_creates_no_transaction(self):
        self._set_mode("rate", default_rate=0.0)
        self.vendor.vendor_tier_id = False
        self._earn(1000.0)
        self.assertFalse(self._commission(self._settle()))

    # ------------------------------------------------------- the plan wins
    def test_a_vendor_with_a_plan_is_untouched_by_the_fallback(self):
        self._set_mode("rate")
        self.vendor.vendor_tier_id = self.env.ref("leaders_marketplace.vendor_tier_bronze")
        plan = self.env["leaders.commission.rule"].create({
            "name": "Flat per amount", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "amount_sold", "achieve_rate": 0.0,
            "achieve_is_tiered": False,
        })
        plan.action_approve()
        self._earn(1000.0)
        settlement = self._settle()
        # The plan applies (and charges 0 here); the 15% tier rate must not.
        self.assertFalse(self._commission(settlement))
        self.assertTrue(settlement.commission_achievement_ids)
        self.assertEqual(settlement.net_amount, 1000.0)

    # ------------------------------------------------------------ accounting
    def test_the_fallback_is_booked_like_any_commission(self):
        self._configure_accounts()
        self._set_mode("rate")
        self.vendor.vendor_tier_id = self.env.ref("leaders_marketplace.vendor_tier_bronze")
        self._earn(1000.0)
        settlement = self._settle()
        settlement.action_approve()
        move = settlement.commission_move_id
        self.assertTrue(move, "no commission journal entry was posted")
        self.assertEqual(move.state, "posted")
        self.assertEqual(sum(move.line_ids.mapped("debit")), 150.0)
        self.assertEqual(sum(move.line_ids.mapped("credit")), 150.0)

    def test_it_skips_gracefully_without_configured_accounts(self):
        self._set_mode("rate")
        self.vendor.vendor_tier_id = self.env.ref("leaders_marketplace.vendor_tier_bronze")
        self._earn(1000.0)
        settlement = self._settle()
        settlement.action_approve()          # must not raise
        self.assertEqual(settlement.state, "approved")
        self.assertEqual(self._commission(settlement).amount, -150.0)

    # ---------------------------------------------------------- idempotence
    def test_collecting_twice_does_not_charge_twice(self):
        self._set_mode("rate")
        self.vendor.vendor_tier_id = self.env.ref("leaders_marketplace.vendor_tier_bronze")
        self._earn(1000.0)
        settlement = self._settle()
        settlement.action_collect_transactions()
        self.assertEqual(len(self._commission(settlement)), 1)


@tagged("post_install", "-at_install")
class TestNoPlanCommissionPortal(HttpCase, NoPlanCommissionCommon):

    def setUp(self):
        super().setUp()
        self.vendor.vendor_tier_id = self.env.ref("leaders_marketplace.vendor_tier_bronze")
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)

    def test_finance_page_says_nothing_is_charged_in_mode_none(self):
        page = self.url_open("/my/vendor/finance")
        self.assertEqual(page.status_code, 200)
        self.assertIn("No commission plan assigned", page.text)
        self.assertIn("No marketplace commission is deducted", page.text)
        self.assertNotIn("15.00%", page.text)

    def test_finance_page_shows_the_applied_rate_in_mode_rate(self):
        self._set_mode("rate")
        page = self.url_open("/my/vendor/finance")
        self.assertEqual(page.status_code, 200)
        self.assertIn("No commission plan assigned", page.text)
        self.assertIn("15.00%", page.text)
        self.assertNotIn("No marketplace commission is deducted", page.text)
