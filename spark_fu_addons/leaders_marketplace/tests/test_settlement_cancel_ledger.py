"""Cancelling a settlement must leave the ledger exactly as it was before it."""
from odoo import fields
from odoo.tests import tagged

from .test_ledger_consistency import LedgerCommon, TestSettlementCancel


@tagged("post_install", "-at_install")
class TestSettlementCancelLedger(LedgerCommon):
    """Reuses the ``_settled`` fixture of the settlement cancel tests."""

    _settled = TestSettlementCancel._settled

    def _ledger(self):
        return self.env["leaders.marketplace.accounting"]._leaders_check_ledger(self.vendor)

    def _commission_income_net(self):
        accounts = self.env["leaders.marketplace.accounting"]._get_accounts()
        lines = self.env["account.move.line"].search([
            ("account_id", "=", accounts["commission_income"]),
            ("parent_state", "=", "posted")])
        return sum(lines.mapped("credit")) - sum(lines.mapped("debit"))

    def _reversals(self, move):
        return self.env["account.move"].search([("reversed_entry_id", "=", move.id)])

    def test_a_draft_cancel_changes_no_accounting(self):
        self._configure_accounts()
        settlement = self._settled()
        payable = self._payable_balance()
        income = self._commission_income_net()
        moves = self.env["account.move"].search_count([])
        settlement.action_cancel()
        self.assertEqual(self._payable_balance(), payable)
        self.assertEqual(self._commission_income_net(), income)
        self.assertEqual(self.env["account.move"].search_count([]), moves,
                         "nothing was posted, so nothing may be booked")

    def test_an_approved_cancel_restores_the_pre_approval_state(self):
        self._configure_accounts()
        settlement = self._settled()
        payable_draft = self._payable_balance()
        ledger_draft = self._ledger()
        income_draft = self._commission_income_net()
        settlement.action_approve()
        move = settlement.commission_move_id
        self.assertTrue(move)
        self.assertEqual(self._payable_balance(), payable_draft + 10.0)

        settlement.action_cancel()

        self.assertEqual(self._payable_balance(), payable_draft,
                         "cancel must not credit the vendor payable twice")
        self.assertEqual(self._ledger()["payable"], ledger_draft["payable"])
        self.assertEqual(self._ledger()["escrow"], ledger_draft["escrow"])
        self.assertEqual(self._commission_income_net(), income_draft)
        # Wallet: the earnings are back in the pool and commission + credit net to 0.
        txs = self.Wallet.search([("vendor_id", "=", self.vendor.id)])
        self.assertEqual(
            sum(txs.filtered(lambda t: t.type in ("commission", "adjustment")).mapped("amount")), 0.0)
        earnings = txs.filtered(lambda t: t.type == "earning")
        self.assertEqual(self._ledger()["wallet"]["unsettled"], sum(earnings.mapped("amount")),
                         "the wallet must be back to the pre-settlement pool")
        self.assertFalse(earnings.mapped("settlement_id"))
        self.assertEqual(set(earnings.mapped("state")), {"available"})
        self.assertEqual(len(self._reversals(move)), 1)
        adjustment = txs.filtered(lambda t: t.type == "adjustment")
        self.assertFalse(adjustment.move_id, "the reversal already undid the commission entry")

    def test_cancel_is_exactly_once(self):
        self._configure_accounts()
        settlement = self._settled()
        settlement.action_approve()
        move = settlement.commission_move_id
        settlement.action_cancel()
        payable = self._payable_balance()
        moves = self.env["account.move"].search_count([])
        settlement.action_cancel()
        self.assertEqual(self._payable_balance(), payable)
        self.assertEqual(self.env["account.move"].search_count([]), moves)
        self.assertEqual(len(self._reversals(move)), 1)

    def test_a_new_settlement_after_a_cancel_ends_clean(self):
        self._configure_accounts()
        settlement = self._settled()
        settlement.action_approve()
        settlement.action_cancel()
        today = fields.Date.today()
        again = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": today, "period_to": today,
            "currency_id": self.company.currency_id.id,
        })
        again.action_collect_transactions()
        again.action_approve()
        again.action_mark_paid()
        self.assertEqual(self.company.currency_id.round(self._payable_balance()), 0.0,
                         "residue left on the vendor payable")
        self.assertEqual(self._commission_income_net(), 10.0,
                         "the commission must be recognised exactly once")
