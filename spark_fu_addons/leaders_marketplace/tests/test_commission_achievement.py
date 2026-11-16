from datetime import date, timedelta

from odoo import Command, fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .test_vendor_split import MarketplaceVendorCommon


@tagged("post_install", "-at_install")
class TestCommissionAchievement(MarketplaceVendorCommon):
    """The 4 achievement metrics (order count, qty sold, amount sold, listing
    days), flat rate vs. tiered, how returns net out, and the rule that a
    vendor is covered by exactly one active commission plan at a time.
    """

    def _deliver(self, product, qty, tracking_ref):
        order = self._order([(product, qty)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = tracking_ref
        group.action_ship()
        group.action_delivered()
        return vendor_order

    def _return(self, vendor_order, quantity, line=None):
        line = line or vendor_order.line_ids.filtered(lambda l: not l.is_delivery)[:1]
        ret = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": line.id, "quantity": quantity})],
        })
        ret.action_vendor_approve()
        ret.action_receive()
        return ret

    def _settle(self, vendor_orders, period_from=None, period_to=None):
        today = fields.Date.today()
        period_from = period_from or today
        period_to = period_to or today
        Wallet = self.env["leaders.wallet.transaction"]
        earnings = Wallet.search([
            ("vendor_order_id", "in", vendor_orders.ids), ("type", "=", "earning"),
        ])
        earnings.write({"state": "available", "available_date": today})
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": period_from, "period_to": period_to,
            "currency_id": self.company.currency_id.id,
        })
        settlement.action_collect_transactions()
        return settlement

    # ------------------------------------------------------------- flat metrics
    def test_order_count_achievement_pays_a_rate_per_order(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Order count plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "order_count", "achieve_rate": 7.0,
        })
        plan.action_approve()
        vo1 = self._deliver(self.fbm_product, 1, "OC-1")
        vo2 = self._deliver(self.fbm_product, 1, "OC-2")
        settlement = self._settle(vo1 | vo2)
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 14.0)

    def test_listing_days_achievement_counts_published_days_in_period(self):
        today = fields.Date.today()
        Log = self.env["leaders.marketplace.offer.publish.log"]
        for i in range(5):
            Log.create({"offer_id": self.fbm_offer.id, "date": today - timedelta(days=i)})
        # One log falls outside the plan's window and must not be counted.
        Log.create({"offer_id": self.fbm_offer.id, "date": today - timedelta(days=20)})
        plan = self.env["leaders.commission.rule"].create({
            "name": "Listing days plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "date_from": today - timedelta(days=10), "date_to": today,
            "achieve_type": "listing_days", "achieve_rate": 3.0,
        })
        plan.action_approve()
        vo = self._deliver(self.fbm_product, 1, "LD-1")
        settlement = self._settle(vo, period_from=today - timedelta(days=10), period_to=today)
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 15.0)

    def test_qty_sold_achievement_pays_a_rate_per_unit(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Qty sold plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "qty_sold", "achieve_rate": 5.0,
        })
        plan.action_approve()
        vo = self._deliver(self.fbm_product, 4, "QTY-1")
        settlement = self._settle(vo)
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 20.0)

    def test_amount_sold_achievement_pays_a_percentage(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Amount sold plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "amount_sold", "achieve_rate": 0.10,
        })
        plan.action_approve()
        vo = self._deliver(self.fbm_product, 4, "AMT-1")  # 4 x 100 = 400
        settlement = self._settle(vo)
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 40.0)

    # -------------------------------------------------------------- tiered plans
    def test_tiered_achievement_pays_the_highest_reached_tier(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Tiered order count plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "order_count", "achieve_is_tiered": True, "achieve_target_amount": 4,
            "target_commission_ids": [
                Command.create({"target_rate": 0.0, "amount": 0.0}),
                Command.create({"target_rate": 0.5, "amount": 20.0}),
                Command.create({"target_rate": 1.0, "amount": 50.0}),
            ],
        })
        plan.action_approve()
        vos = self.env["leaders.vendor.order"]
        for i in range(2):  # 2 of 4 target = 50% completion, not 100%
            vos |= self._deliver(self.fbm_product, 1, f"TIER-{i}")
        settlement = self._settle(vos)
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 20.0)
        self.assertEqual(settlement.commission_achievement_ids.target_amount, 4.0)
        self.assertEqual(settlement.commission_achievement_ids.achievement_percent, 50.0)

    def test_listing_days_cannot_be_tiered(self):
        with self.assertRaises(ValidationError):
            self.env["leaders.commission.rule"].create({
                "name": "Bad tiered listing days", "scope": "vendor", "vendor_id": self.vendor.id,
                "type": "achieve", "achieve_type": "listing_days",
                "achieve_is_tiered": True, "achieve_target_amount": 5,
            })

    def test_achieve_plan_requires_a_metric(self):
        with self.assertRaises(ValidationError):
            self.env["leaders.commission.rule"].create({
                "name": "No metric plan", "scope": "vendor", "vendor_id": self.vendor.id,
                "type": "achieve",
            })

    # --------------------------------------------------- one plan per vendor rule
    def test_second_plan_for_same_vendor_is_blocked(self):
        """A vendor is covered by exactly one active plan; a second one --

        even on a different metric -- is rejected while the first is active
        for an overlapping period.
        """
        first = self.env["leaders.commission.rule"].create({
            "name": "First plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "order_count", "achieve_rate": 5.0,
        })
        first.action_approve()
        second = self.env["leaders.commission.rule"].create({
            "name": "Second plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "amount_sold", "achieve_rate": 0.10,
        })
        with self.assertRaises(ValidationError):
            second.action_approve()

    def test_second_plan_is_allowed_once_the_first_is_cancelled(self):
        first = self.env["leaders.commission.rule"].create({
            "name": "First plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "order_count", "achieve_rate": 5.0,
        })
        first.action_approve()
        first.action_cancel()
        second = self.env["leaders.commission.rule"].create({
            "name": "Second plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "amount_sold", "achieve_rate": 0.10,
        })
        second.action_approve()  # does not raise
        self.assertEqual(second.state, "approved")

    def test_second_plan_for_a_different_vendor_is_not_blocked(self):
        other_vendor = self.env["res.partner"].create({
            "name": "Other Vendor", "is_marketplace_vendor": True, "vendor_state": "approved",
        })
        first = self.env["leaders.commission.rule"].create({
            "name": "Vendor A plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "order_count", "achieve_rate": 5.0,
        })
        second = self.env["leaders.commission.rule"].create({
            "name": "Vendor B plan", "scope": "vendor", "vendor_id": other_vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "order_count", "achieve_rate": 5.0,
        })
        (first | second).action_approve()  # does not raise
        self.assertEqual((first | second).mapped("state"), ["approved", "approved"])

    # ------------------------------------------------------------------ returns
    def test_qty_sold_nets_a_partial_return(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Qty plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "qty_sold", "achieve_rate": 5.0,
        })
        plan.action_approve()
        vendor_order = self._deliver(self.fbm_product, 4, "RET-NET-QTY")  # 4 units
        self._return(vendor_order, 2)  # 2 units given back
        settlement = self._settle(vendor_order)
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 10.0)  # 2 kept x 5

    def test_amount_sold_nets_a_partial_return(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Amount plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "amount_sold", "achieve_rate": 0.10,
        })
        plan.action_approve()
        vendor_order = self._deliver(self.fbm_product, 4, "RET-NET-AMT")  # 4 x 100 = 400
        self._return(vendor_order, 2)  # 200 given back
        settlement = self._settle(vendor_order)
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 20.0)  # 200 kept x 10%

    def test_order_count_excludes_a_fully_returned_order(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Order count return plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "order_count", "achieve_rate": 10.0,
        })
        plan.action_approve()
        vo_kept = self._deliver(self.fbm_product, 1, "OC-KEEP")
        vo_returned = self._deliver(self.fbm_product, 1, "OC-RET")
        self._return(vo_returned, 1)
        settlement = self._settle(vo_kept | vo_returned)
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 10.0)

    def test_target_plan_sales_amount_nets_a_return(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Target net return plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "target_amount": 1000.0, "plan_effect": "deduction",
            "target_commission_ids": [Command.create({"target_rate": 0.0, "amount": 0.0})],
        })
        plan.action_approve()
        vendor_order = self._deliver(self.fbm_product, 2, "TGT-RET")  # 2 x 100 = 200 vendor_net
        self._return(vendor_order, 1)  # returns 100
        settlement = self._settle(vendor_order)
        self.assertEqual(settlement.commission_achievement_ids.sales_amount, 100.0)

    # -------------------------------------------- deduction: we take it, not pay it
    # The real usage of this whole engine is plan_effect="deduction": the plan is
    # the marketplace's own commission, taken FROM the vendor, not a bonus paid to
    # them. These tests check every metric/mode actually deducts (a negative
    # "commission" wallet transaction), never accidentally pays a bonus. Each one
    # gets its own plan/vendor -- a vendor only ever has one plan at a time.

    def test_order_count_flat_deducts_from_the_vendor(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Deduct order_count", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "order_count", "achieve_rate": 8.0,
        })
        plan.action_approve()
        vendor_order = self._deliver(self.fbm_product, 4, "DEDUCT-OC-1")  # 1 order
        settlement = self._settle(vendor_order)
        deduction = settlement.transaction_ids.filtered(lambda t: t.type == "commission")
        self.assertFalse(settlement.transaction_ids.filtered(lambda t: t.type == "bonus"))
        self.assertEqual(deduction.amount, -8.0)

    def test_qty_sold_flat_deducts_from_the_vendor(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Deduct qty_sold", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "qty_sold", "achieve_rate": 3.0,
        })
        plan.action_approve()
        vendor_order = self._deliver(self.fbm_product, 4, "DEDUCT-QTY-1")
        settlement = self._settle(vendor_order)
        deduction = settlement.transaction_ids.filtered(lambda t: t.type == "commission")
        self.assertFalse(settlement.transaction_ids.filtered(lambda t: t.type == "bonus"))
        self.assertEqual(deduction.amount, -12.0)  # 4 units x 3

    def test_amount_sold_flat_deducts_from_the_vendor(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Deduct amount_sold", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "amount_sold", "achieve_rate": 0.05,
        })
        plan.action_approve()
        vendor_order = self._deliver(self.fbm_product, 4, "DEDUCT-AMT-1")  # 400
        settlement = self._settle(vendor_order)
        deduction = settlement.transaction_ids.filtered(lambda t: t.type == "commission")
        self.assertFalse(settlement.transaction_ids.filtered(lambda t: t.type == "bonus"))
        self.assertEqual(deduction.amount, -20.0)  # 400 x 5%

    def test_listing_days_flat_deducts_from_the_vendor(self):
        today = fields.Date.today()
        for i in range(3):
            self.env["leaders.marketplace.offer.publish.log"].create({
                "offer_id": self.fbm_offer.id, "date": today - timedelta(days=i),
            })
        plan = self.env["leaders.commission.rule"].create({
            "name": "Deduct listing_days", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "listing_days", "achieve_rate": 2.0,
        })
        plan.action_approve()
        vendor_order = self._deliver(self.fbm_product, 1, "DEDUCT-LD-1")
        settlement = self._settle(vendor_order, period_from=today - timedelta(days=2), period_to=today)
        deduction = settlement.transaction_ids.filtered(lambda t: t.type == "commission")
        self.assertFalse(settlement.transaction_ids.filtered(lambda t: t.type == "bonus"))
        self.assertEqual(deduction.amount, -6.0)  # 3 days x 2

    def test_order_count_tiered_deducts_from_the_vendor(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Deduct order_count tiered", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "order_count", "achieve_is_tiered": True, "achieve_target_amount": 2,
            "target_commission_ids": [
                Command.create({"target_rate": 0.0, "amount": 0.0}),
                Command.create({"target_rate": 0.5, "amount": 10.0}),
                Command.create({"target_rate": 1.0, "amount": 30.0}),
            ],
        })
        plan.action_approve()
        vendor_order = self._deliver(self.fbm_product, 4, "DEDUCT-OC-TIER-1")  # 1 of 2 orders = 50%
        settlement = self._settle(vendor_order)
        deduction = settlement.transaction_ids.filtered(lambda t: t.type == "commission")
        self.assertEqual(deduction.amount, -10.0)
        self.assertEqual(settlement.commission_achievement_ids.achievement_percent, 50.0)

    def test_qty_sold_tiered_deducts_from_the_vendor(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Deduct qty_sold tiered", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "qty_sold", "achieve_is_tiered": True, "achieve_target_amount": 8,
            "target_commission_ids": [
                Command.create({"target_rate": 0.0, "amount": 0.0}),
                Command.create({"target_rate": 0.5, "amount": 10.0}),
                Command.create({"target_rate": 1.0, "amount": 30.0}),
            ],
        })
        plan.action_approve()
        vendor_order = self._deliver(self.fbm_product, 4, "DEDUCT-QTY-TIER-1")  # 4 of 8 = 50%
        settlement = self._settle(vendor_order)
        deduction = settlement.transaction_ids.filtered(lambda t: t.type == "commission")
        self.assertEqual(deduction.amount, -10.0)
        self.assertEqual(settlement.commission_achievement_ids.achievement_percent, 50.0)

    def test_amount_sold_tiered_deducts_from_the_vendor(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Deduct amount_sold tiered", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "amount_sold", "achieve_is_tiered": True, "achieve_target_amount": 800,
            "target_commission_ids": [
                Command.create({"target_rate": 0.0, "amount": 0.0}),
                Command.create({"target_rate": 0.5, "amount": 10.0}),
                Command.create({"target_rate": 1.0, "amount": 30.0}),
            ],
        })
        plan.action_approve()
        vendor_order = self._deliver(self.fbm_product, 4, "DEDUCT-AMT-TIER-1")  # 400 of 800 = 50%
        settlement = self._settle(vendor_order)
        deduction = settlement.transaction_ids.filtered(lambda t: t.type == "commission")
        self.assertEqual(deduction.amount, -10.0)
        self.assertEqual(settlement.commission_achievement_ids.achievement_percent, 50.0)

    def test_target_plan_deducts_from_the_vendor(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Deduct target plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "target_amount": 200.0, "plan_effect": "deduction",
            "target_commission_ids": [
                Command.create({"target_rate": 0.0, "amount": 0.0}),
                Command.create({"target_rate": 0.5, "amount": 5.0}),
                Command.create({"target_rate": 1.0, "amount": 10.0}),
            ],
        })
        plan.action_approve()
        vendor_order = self._deliver(self.fbm_product, 1, "DEDUCT-TARGET-1")  # 1 x 100 = 100 = 50%
        settlement = self._settle(vendor_order)
        deduction = settlement.transaction_ids.filtered(lambda t: t.type == "commission")
        self.assertFalse(settlement.transaction_ids.filtered(lambda t: t.type == "bonus"))
        self.assertEqual(deduction.amount, -5.0)
        self.assertEqual(settlement.commission_achievement_ids.achievement_percent, 50.0)


@tagged("post_install", "-at_install")
class TestCumulativeTrueUp(MarketplaceVendorCommon):
    """A vendor must not be able to price their own commission by slicing.

    The tier of a target (or tiered achievement) plan is judged on the metric
    accumulated from the start of the plan's target period, and what earlier
    settlements already charged for that same period is subtracted. Settling a
    quarter once, monthly, or daily therefore costs exactly the same.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A quarter that is entirely in the past, whatever day the suite runs.
        year = fields.Date.today().year - 1
        cls.q1_from = date(year, 1, 1)
        cls.q1_to = date(year, 3, 31)
        cls.q2_from = date(year, 4, 1)
        cls.q2_to = date(year, 6, 30)

    # ------------------------------------------------------------------ helpers
    def _plan(self, date_from, date_to, **overrides):
        vals = {
            "name": "Quarterly target", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "target", "plan_effect": "deduction", "periodicity": "quarter",
            "date_from": date_from, "date_to": date_to, "target_amount": 1000.0,
            "target_commission_ids": [
                Command.create({"target_rate": 0.0, "amount": 0.0}),
                Command.create({"target_rate": 0.5, "amount": 50.0}),
                Command.create({"target_rate": 1.0, "amount": 200.0}),
            ],
        }
        vals.update(overrides)
        plan = self.env["leaders.commission.rule"].create(vals)
        plan.action_approve()
        return plan

    def _earn(self, amount, on_date):
        """An available earning of ``amount`` for the vendor on ``on_date``."""
        return self.env["leaders.wallet.transaction"].create({
            "vendor_id": self.vendor.id,
            "type": "earning",
            "amount": amount,
            "currency_id": self.company.currency_id.id,
            "available_date": on_date,
            "state": "available",
        })

    def _give_back(self, amount, on_date):
        """A return transaction (negative) on ``on_date``."""
        return self.env["leaders.wallet.transaction"].create({
            "vendor_id": self.vendor.id,
            "type": "return",
            "amount": -abs(amount),
            "currency_id": self.company.currency_id.id,
            "available_date": on_date,
            "state": "available",
        })

    def _settle(self, period_from, period_to):
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id,
            "period_from": period_from, "period_to": period_to,
            "currency_id": self.company.currency_id.id,
        })
        settlement.action_collect_transactions()
        return settlement

    def _charged(self, settlements):
        """Total commission actually deducted across these settlements."""
        return sum(-tx.amount for s in settlements
                   for tx in s.transaction_ids if tx.type == "commission")

    # ------------------------------------------------- the loophole itself
    def _seed_quarter(self):
        """1200 earned across the quarter: 120% of the 1000 target."""
        self._earn(400.0, date(self.q1_from.year, 1, 15))
        self._earn(400.0, date(self.q1_from.year, 2, 15))
        self._earn(400.0, date(self.q1_from.year, 3, 15))

    def test_one_settlement_for_the_whole_quarter(self):
        self._plan(self.q1_from, self.q1_to)
        self._seed_quarter()
        settlement = self._settle(self.q1_from, self.q1_to)
        self.assertEqual(self._charged(settlement), 200.0)

    def test_three_monthly_settlements_cost_the_same(self):
        self._plan(self.q1_from, self.q1_to)
        self._seed_quarter()
        year = self.q1_from.year
        settlements = self.env["leaders.settlement"]
        for start, end in ((date(year, 1, 1), date(year, 1, 31)),
                           (date(year, 2, 1), date(year, 2, 28)),
                           (date(year, 3, 1), date(year, 3, 31))):
            settlements |= self._settle(start, end)
        self.assertEqual(self._charged(settlements), 200.0)
        # And it was billed as a staircase, never twice for the same tier.
        self.assertEqual(
            sorted(settlements.commission_achievement_ids.mapped("commission_amount")),
            [0.0, 50.0, 150.0])

    def test_ninety_daily_settlements_cost_the_same(self):
        self._plan(self.q1_from, self.q1_to)
        self._seed_quarter()
        settlements = self.env["leaders.settlement"]
        day = self.q1_from
        while day <= self.q1_to:
            settlements |= self._settle(day, day)
            day += timedelta(days=1)
        self.assertGreaterEqual(len(settlements), 89)
        self.assertEqual(self._charged(settlements), 200.0)

    # --------------------------------------------------- deduction only
    def test_returns_after_a_tier_never_produce_a_negative_charge(self):
        self._plan(self.q1_from, self.q1_to)
        year = self.q1_from.year
        self._earn(400.0, date(year, 1, 15))
        self._earn(400.0, date(year, 2, 15))
        first = self._settle(date(year, 1, 1), date(year, 2, 28))
        self.assertEqual(self._charged(first), 50.0)      # 80% -> the 50% tier

        self._give_back(400.0, date(year, 3, 15))          # back down to 40%
        second = self._settle(date(year, 3, 1), date(year, 3, 31))
        self.assertEqual(self._charged(second), 0.0)
        achievement = second.commission_achievement_ids
        self.assertEqual(achievement.commission_amount, 0.0)
        self.assertEqual(achievement.already_charged, 50.0)
        self.assertEqual(achievement.cumulative_sales, 400.0)
        # Nothing was refunded: the plan is deduction-only.
        self.assertEqual(self._charged(first | second), 50.0)

    # ------------------------------------------- a settlement crossing quarters
    def test_settlement_across_two_quarters_creates_one_row_per_quarter(self):
        plan = self._plan(self.q1_from, self.q2_to)
        self.assertEqual(len(plan.target_ids), 2)
        year = self.q1_from.year
        self._earn(1200.0, date(year, 2, 15))   # Q1: 120%
        self._earn(600.0, date(year, 5, 15))    # Q2: 60%

        settlement = self._settle(date(year, 2, 1), date(year, 5, 31))
        rows = settlement.commission_achievement_ids.sorted("period_from")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows.mapped("target_id"), plan.target_ids.sorted("date_from"))
        self.assertEqual(rows[0].cumulative_sales, 1200.0)
        self.assertEqual(rows[0].commission_amount, 200.0)
        self.assertEqual(rows[1].cumulative_sales, 600.0)
        self.assertEqual(rows[1].commission_amount, 50.0)
        self.assertEqual(self._charged(settlement), 250.0)

    def test_each_quarter_is_trued_up_independently(self):
        """Reaching a tier in Q1 must not offset what Q2 owes."""
        plan = self._plan(self.q1_from, self.q2_to)
        year = self.q1_from.year
        self._earn(1200.0, date(year, 2, 15))
        first = self._settle(self.q1_from, self.q1_to)
        self.assertEqual(self._charged(first), 200.0)

        self._earn(1200.0, date(year, 5, 15))
        second = self._settle(self.q2_from, self.q2_to)
        self.assertEqual(self._charged(second), 200.0)
        self.assertNotEqual(
            first.commission_achievement_ids.target_id,
            second.commission_achievement_ids.target_id)

    # ------------------------------------------------- untouched calculations
    def test_a_plan_without_target_periods_keeps_the_old_path(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "No periods", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "target", "plan_effect": "deduction", "target_amount": 1000.0,
            "target_commission_ids": [
                Command.create({"target_rate": 0.0, "amount": 0.0}),
                Command.create({"target_rate": 1.0, "amount": 200.0}),
            ],
        })
        plan.action_approve()
        self.assertFalse(plan.target_ids)
        self._earn(1200.0, date(self.q1_from.year, 2, 15))
        settlement = self._settle(self.q1_from, self.q1_to)
        achievement = settlement.commission_achievement_ids
        self.assertFalse(achievement.target_id)
        self.assertEqual(achievement.commission_amount, 200.0)
        self.assertEqual(achievement.already_charged, 0.0)

    def test_a_flat_rate_achievement_plan_stays_linear(self):
        """achieve_is_tiered=False is a rate per unit: no true-up, no target row."""
        plan = self.env["leaders.commission.rule"].create({
            "name": "Flat per order", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction", "periodicity": "quarter",
            "date_from": self.q1_from, "date_to": self.q1_to,
            "achieve_type": "order_count", "achieve_rate": 8.0,
            "achieve_is_tiered": False,
        })
        plan.action_approve()
        self.assertTrue(plan.target_ids)          # periods exist...
        self.assertFalse(plan._true_up_target_periods(self.q1_from, self.q1_to))
        self._earn(500.0, date(self.q1_from.year, 2, 15))
        settlement = self._settle(self.q1_from, self.q1_to)
        self.assertFalse(settlement.commission_achievement_ids.target_id)

    def test_a_cancelled_settlement_does_not_count_as_already_charged(self):
        self._plan(self.q1_from, self.q1_to)
        year = self.q1_from.year
        self._earn(1200.0, date(year, 2, 15))
        first = self._settle(self.q1_from, self.q1_to)
        self.assertEqual(self._charged(first), 200.0)
        first.state = "cancelled"

        self._earn(0.01, date(year, 3, 20))
        second = self._settle(self.q1_from, self.q1_to)
        # The cancelled settlement's money and its charge are both ignored.
        self.assertEqual(second.commission_achievement_ids.already_charged, 0.0)
        self.assertEqual(second.commission_achievement_ids.cumulative_sales, 0.01)
