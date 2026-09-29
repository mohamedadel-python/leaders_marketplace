"""The wallet ledger and the vendor payable account must tell the same story.

Storage fees are charged on what the *platform* warehouse holds, once per
month; every fee and adjustment carries its own journal entry; and a settlement
made in error can be undone without deleting an immutable transaction.
"""
from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_vendor_split import MarketplaceVendorCommon


class LedgerCommon(MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wallet = cls.env["leaders.wallet.transaction"]
        cls.Fee = cls.env["leaders.fulfillment.fee"]

    def _storage_rule(self, amount=1.0):
        return self.Fee.create({
            "name": "Storage per CBM/day", "fee_type": "storage",
            "calc_method": "per_cbm_day", "amount": amount,
        })

    def _storage_fees(self):
        return self.Wallet.search([
            ("vendor_id", "=", self.vendor.id), ("type", "=", "storage_fee")])

    def _payable_balance(self, vendor=None):
        """Balance of the vendor payable account for one vendor."""
        accounts = self.env["leaders.marketplace.accounting"]._get_accounts()
        lines = self.env["account.move.line"].search([
            ("account_id", "=", accounts["vendor_payable"]),
            ("partner_id", "=", (vendor or self.vendor).commercial_partner_id.id),
            ("parent_state", "=", "posted"),
        ])
        return sum(lines.mapped("debit")) - sum(lines.mapped("credit"))


@tagged("post_install", "-at_install")
class TestStorageFee(LedgerCommon):

    def test_storage_is_measured_on_fbl_stock_not_the_vendors_own(self):
        self._storage_rule()
        self.fbm_product.volume = 1.0
        self.fbl_product.volume = 0.0        # only FBM stock has volume
        self.Fee.cron_apply_storage_fees()
        self.assertFalse(self._storage_fees(),
                         "the vendor's own FBM warehouse must not be charged")

    def test_fbl_stock_is_charged(self):
        self._storage_rule(amount=2.0)
        self.fbl_product.volume = 0.5        # 3 units in FBL -> 1.5 CBM
        self.fbm_product.volume = 0.0
        self.Fee.cron_apply_storage_fees()
        fees = self._storage_fees()
        self.assertEqual(len(fees), 1)
        self.assertEqual(fees.amount, -(2.0 * 1.5 * 30))

    def test_running_the_cron_twice_in_a_month_charges_once(self):
        self._storage_rule()
        self.fbl_product.volume = 0.5
        self.Fee.cron_apply_storage_fees()
        self.Fee.cron_apply_storage_fees()
        self.Fee.cron_apply_storage_fees()
        self.assertEqual(len(self._storage_fees()), 1)

    def test_the_month_charged_is_on_the_reference(self):
        self._storage_rule()
        self.fbl_product.volume = 0.5
        self.Fee.cron_apply_storage_fees()
        expected = "STORAGE/%s" % fields.Date.context_today(self.Fee).strftime("%Y-%m")
        self.assertEqual(self._storage_fees().reference, expected)

    def test_a_new_month_is_charged_again(self):
        self._storage_rule()
        self.fbl_product.volume = 0.5
        self.Fee.cron_apply_storage_fees()
        # Pretend the existing charge was for another month.
        self.env.cr.execute(
            "UPDATE leaders_wallet_transaction SET reference = 'STORAGE/1999-01' "
            "WHERE id = %s", [self._storage_fees().id])
        self.Wallet.invalidate_model(["reference"])
        self.Fee.cron_apply_storage_fees()
        self.assertEqual(len(self._storage_fees()), 2)


@tagged("post_install", "-at_install")
class TestFeeJournalEntries(LedgerCommon):

    def _fee(self, tx_type, amount):
        return self.Wallet.create({
            "vendor_id": self.vendor.id, "type": tx_type, "amount": amount,
            "currency_id": self.company.currency_id.id,
            "available_date": fields.Date.today(), "state": "available",
            "reference": "TEST-%s" % tx_type.upper(),
        })

    def test_a_charge_debits_the_vendor_payable(self):
        self._configure_accounts()
        tx = self._fee("storage_fee", -40.0)
        self.assertTrue(tx.move_id, "no journal entry for the storage fee")
        self.assertEqual(tx.move_id.state, "posted")
        self.assertEqual(self._payable_balance(), 40.0)   # we owe the vendor 40 less

    def test_a_penalty_is_booked_too(self):
        self._configure_accounts()
        tx = self._fee("penalty", -25.0)
        self.assertTrue(tx.move_id)
        self.assertEqual(self._payable_balance(), 25.0)

    def test_a_positive_adjustment_runs_the_other_way(self):
        self._configure_accounts()
        tx = self._fee("adjustment", 15.0)
        self.assertTrue(tx.move_id)
        self.assertEqual(self._payable_balance(), -15.0)  # we owe the vendor 15 more

    def test_an_earning_gets_no_fee_entry(self):
        """Earnings are booked by the escrow release, not here."""
        self._configure_accounts()
        tx = self._fee("earning", 100.0)
        self.assertFalse(tx.move_id)

    def test_it_skips_gracefully_without_configured_accounts(self):
        tx = self._fee("storage_fee", -40.0)     # must not raise
        self.assertFalse(tx.move_id)

    def test_the_payable_nets_to_zero_once_the_settlement_is_paid(self):
        """The whole point: wallet and ledger end up agreeing."""
        self._configure_accounts()
        vendor_order = self._deliver_fbm(2)
        self._fee("storage_fee", -30.0)

        today = fields.Date.today()
        self.Wallet.search([
            ("vendor_id", "=", self.vendor.id), ("type", "=", "earning"),
        ]).write({"state": "available", "available_date": today})
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": today, "period_to": today,
            "currency_id": self.company.currency_id.id,
        })
        settlement.action_collect_transactions()
        settlement.action_approve()
        settlement.action_mark_paid()

        self.assertEqual(settlement.state, "paid")
        self.assertEqual(
            self.company.currency_id.round(self._payable_balance()), 0.0,
            "vendor payable did not net to zero after the payout")

    def _deliver_fbm(self, qty):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({
                "product_id": self.fbm_product.id, "product_uom_qty": qty})],
        })
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "LEDGER-TRK"
        group.action_ship()
        group.action_delivered()
        return vendor_order


@tagged("post_install", "-at_install")
class TestSettlementCancel(LedgerCommon):

    def _settled(self, with_commission=True):
        if with_commission:
            plan = self.env["leaders.commission.rule"].create({
                "name": "Flat 10 per order", "scope": "vendor", "vendor_id": self.vendor.id,
                "type": "achieve", "plan_effect": "deduction",
                "achieve_type": "order_count", "achieve_rate": 10.0,
                "achieve_is_tiered": False,
            })
            plan.action_approve()
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({
                "product_id": self.fbm_product.id, "product_uom_qty": 2})],
        })
        order.action_confirm()
        group = order.vendor_order_ids.fulfillment_group_ids
        group.tracking_ref = "CANCEL-TRK"
        group.action_ship()
        group.action_delivered()
        today = fields.Date.today()
        self.Wallet.search([
            ("vendor_id", "=", self.vendor.id), ("type", "=", "earning"),
        ]).write({"state": "available", "available_date": today})
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": today, "period_to": today,
            "currency_id": self.company.currency_id.id,
        })
        settlement.action_collect_transactions()
        return settlement

    def test_cancelling_releases_the_earnings(self):
        settlement = self._settled()
        earnings = settlement.transaction_ids.filtered(lambda t: t.type == "earning")
        self.assertTrue(earnings)

        settlement.action_cancel()

        self.assertEqual(settlement.state, "cancelled")
        self.assertFalse(earnings.settlement_id, "earnings were not released")
        self.assertEqual(set(earnings.mapped("state")), {"available"})

    def test_the_commission_is_neutralised_not_deleted(self):
        settlement = self._settled()
        commission = settlement.transaction_ids.filtered(lambda t: t.type == "commission")
        self.assertEqual(commission.amount, -10.0)

        settlement.action_cancel()

        # Immutable: the original charge is still there...
        self.assertTrue(commission.exists())
        self.assertEqual(commission.amount, -10.0)
        # ...and an equal credit cancels it out.
        credit = settlement.transaction_ids.filtered(lambda t: t.type == "adjustment")
        self.assertEqual(credit.amount, 10.0)
        self.assertEqual(
            sum(settlement.transaction_ids.filtered(
                lambda t: t.type in ("commission", "adjustment")).mapped("amount")), 0.0)

    def test_an_approved_settlement_reverses_its_commission_entry(self):
        self._configure_accounts()
        settlement = self._settled()
        settlement.action_approve()
        move = settlement.commission_move_id
        self.assertTrue(move)

        settlement.action_cancel()

        reversal = self.env["account.move"].search([("reversed_entry_id", "=", move.id)])
        self.assertTrue(reversal, "the commission entry was not reversed")

    def test_a_paid_settlement_cannot_be_cancelled(self):
        self._configure_accounts()
        settlement = self._settled()
        settlement.action_approve()
        settlement.action_mark_paid()
        with self.assertRaises(UserError):
            settlement.action_cancel()

    def test_cancel_is_finance_only(self):
        settlement = self._settled()
        portal_user = self.env["res.users"].create({
            "name": "Cancel Portal", "login": "lm.cancel@example.com",
            "groups_id": [Command.set([
                self.env.ref("leaders_marketplace.group_vendor_owner").id])],
        })
        with self.assertRaises(AccessError):
            settlement.with_user(portal_user).action_cancel()

    def test_cancelled_achievements_stop_counting_as_already_charged(self):
        settlement = self._settled()
        achievements = settlement.commission_achievement_ids
        self.assertTrue(achievements)
        settlement.action_cancel()
        self.assertTrue(all(achievements.mapped("cancelled")))

    def test_cancelling_twice_is_harmless(self):
        settlement = self._settled()
        settlement.action_cancel()
        settlement.action_cancel()
        self.assertEqual(settlement.state, "cancelled")
        self.assertEqual(len(settlement.transaction_ids.filtered(
            lambda t: t.type == "adjustment")), 1)


@tagged("post_install", "-at_install")
class TestPayoutJournal(LedgerCommon):

    def test_the_chosen_journal_is_used_for_the_payout(self):
        self._configure_accounts()
        other = self.env["account.journal"].create({
            "name": "Second Bank", "code": "LMB2", "type": "bank",
        })
        today = fields.Date.today()
        self.Wallet.create({
            "vendor_id": self.vendor.id, "type": "earning", "amount": 100.0,
            "currency_id": self.company.currency_id.id,
            "available_date": today, "state": "available",
        })
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": today, "period_to": today,
            "currency_id": self.company.currency_id.id,
            "payout_journal_id": other.id,
        })
        settlement.action_collect_transactions()
        settlement.action_approve()
        settlement.action_mark_paid()
        self.assertEqual(settlement.payment_move_id.journal_id, other)

    def test_without_a_choice_the_first_bank_journal_is_used(self):
        """MUST NOT CHANGE: today's behaviour when the field is empty."""
        self._configure_accounts()
        today = fields.Date.today()
        self.Wallet.create({
            "vendor_id": self.vendor.id, "type": "earning", "amount": 100.0,
            "currency_id": self.company.currency_id.id,
            "available_date": today, "state": "available",
        })
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": today, "period_to": today,
            "currency_id": self.company.currency_id.id,
        })
        settlement.action_collect_transactions()
        settlement.action_approve()
        settlement.action_mark_paid()
        expected = self.env["account.journal"].search([
            ("type", "in", ("bank", "cash")), ("company_id", "=", self.company.id)], limit=1)
        self.assertEqual(settlement.payment_move_id.journal_id, expected)
