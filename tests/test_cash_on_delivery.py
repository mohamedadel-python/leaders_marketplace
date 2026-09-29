from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_vendor_split import MarketplaceVendorCommon


@tagged("post_install", "-at_install")
class TestCashOnDelivery(MarketplaceVendorCommon):
    """Stage (a)/(b) of the COD feature: dormant plumbing, exercised directly
    against manually-created COD vendor orders (nothing in checkout can set
    payment_method="cod" yet -- that's the stage (d) checkout controller).
    """

    def _cod_order(self, product, qty, tracking_ref):
        order = self._order([(product, qty)])
        order.leaders_payment_method = "cod"
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = tracking_ref
        group.action_ship()
        group.action_delivered()
        return vendor_order

    def _plan(self, achieve_rate, achieve_type="amount_sold"):
        plan = self.env["leaders.commission.rule"].create({
            "name": "Deduct %s" % achieve_type, "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": achieve_type, "achieve_rate": achieve_rate,
        })
        plan.action_approve()
        return plan

    def _settle(self, period_from=None, period_to=None):
        today = fields.Date.today()
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": period_from or today,
            "period_to": period_to or today, "currency_id": self.company.currency_id.id,
        })
        settlement.action_collect_transactions()
        return settlement

    # ------------------------------------------------------------ FBM + COD
    def test_cod_fbm_delivery_does_not_record_the_debt_by_itself(self):
        """Delivery alone must NOT create the cod_collected debt -- the vendor
        has to explicitly confirm they collected the cash first."""
        vendor_order = self._cod_order(self.fbm_product, 1, "COD-FBM-0")
        self.assertEqual(vendor_order.state, "completed")
        self.assertFalse(vendor_order.cod_cash_confirmed)
        self.assertFalse(self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "cod_collected"),
        ]))

    def test_cod_fbm_confirm_payment_guards(self):
        vendor_order = self._cod_order(self.fbm_product, 1, "COD-FBM-0B")
        online_order = self._order([(self.fbm_product, 1)])
        online_order.action_confirm()
        with self.assertRaises(UserError):
            online_order.vendor_order_ids.action_confirm_cod_payment()  # not COD

        fbl_cod = self._cod_order(self.fbl_product, 1, "COD-FBM-0C")
        with self.assertRaises(UserError):
            fbl_cod.action_confirm_cod_payment()  # COD but not pure FBM

        vendor_order.action_confirm_cod_payment()
        self.assertTrue(vendor_order.cod_cash_confirmed)
        due = self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "cod_collected"),
        ])
        self.assertEqual(len(due), 1)
        vendor_order.action_confirm_cod_payment()  # idempotent, no duplicate
        self.assertEqual(self.env["leaders.wallet.transaction"].search_count([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "cod_collected"),
        ]), 1)

    def test_cod_fbm_order_never_touches_escrow(self):
        vendor_order = self._cod_order(self.fbm_product, 1, "COD-FBM-1")
        vendor_order.action_confirm_cod_payment()
        self.assertEqual(vendor_order.payment_method, "cod")
        self.assertEqual(vendor_order.state, "completed")
        self.assertFalse(vendor_order.collection_move_id)
        self.assertFalse(vendor_order.earning_move_id)
        due = self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "cod_collected"),
        ])
        self.assertEqual(due.amount, vendor_order.vendor_net)
        self.assertEqual(due.state, "available")
        self.assertFalse(self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "earning"),
        ]))

    def test_cod_fbm_settlement_goes_negative_and_blocks_mark_paid(self):
        self._plan(0.10, "amount_sold")  # 10% deduction
        vendor_order = self._cod_order(self.fbm_product, 1, "COD-FBM-2")  # 100.0
        vendor_order.action_confirm_cod_payment()
        settlement = self._settle()

        cod_tx = settlement.transaction_ids.filtered(lambda t: t.type == "cod_collected")
        commission_tx = settlement.transaction_ids.filtered(lambda t: t.type == "commission")
        self.assertEqual(cod_tx.amount, 100.0)
        self.assertEqual(commission_tx.amount, -10.0)  # 10% of the 100.0 COD sale

        # cod_collected is excluded from the payout math -- only the commission
        # deduction counts, so the vendor owes the marketplace.
        self.assertEqual(settlement.gross_amount, 0.0)
        self.assertEqual(settlement.deductions, 10.0)
        self.assertEqual(settlement.net_amount, -10.0)

        settlement.action_approve()
        with self.assertRaises(UserError):
            settlement.action_mark_paid()

        settlement.action_register_vendor_payment()
        self.assertEqual(settlement.state, "paid")
        self.assertTrue(all(t.state == "settled" for t in settlement.transaction_ids))

    def test_cod_fbm_register_vendor_payment_posts_the_reverse_entry(self):
        escrow, income, payable, receivable = self._configure_accounts()
        self._plan(0.10, "amount_sold")
        vendor_order = self._cod_order(self.fbm_product, 1, "COD-FBM-3")
        vendor_order.action_confirm_cod_payment()
        settlement = self._settle()
        settlement.action_approve()

        # A non-negative settlement cannot register a vendor payment.
        empty = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": fields.Date.today(),
            "period_to": fields.Date.today(), "currency_id": self.company.currency_id.id,
        })
        with self.assertRaises(UserError):
            empty.action_register_vendor_payment()

        settlement.action_register_vendor_payment()
        move = settlement.payment_move_id
        self.assertEqual(move.state, "posted")
        self.assertEqual(move.line_ids.filtered(lambda l: l.account_id == payable).credit, 10.0)
        bank_line = move.line_ids - move.line_ids.filtered(lambda l: l.account_id == payable)
        self.assertEqual(bank_line.debit, 10.0)

    # ------------------------------------------------------------ FBL + COD
    def test_cod_fbl_behaves_like_an_online_order(self):
        self._configure_accounts()
        vendor_order = self._cod_order(self.fbl_product, 1, "COD-FBL-1")
        self.assertEqual(vendor_order.payment_method, "cod")
        self.assertEqual(vendor_order.state, "completed")
        # Collection is posted at delivery (not confirm) for COD+FBL, then the
        # normal earning/escrow-release flow runs exactly like an online order.
        self.assertTrue(vendor_order.collection_move_id)
        self.assertTrue(vendor_order.earning_move_id)
        earning = self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "earning"),
        ])
        self.assertEqual(earning.amount, vendor_order.vendor_net)
        self.assertFalse(self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "cod_collected"),
        ]))

    # ------------------------------------------------------------ COD returns
    def test_cod_fbm_return_relieves_debt_not_increases_it(self):
        vendor_order = self._cod_order(self.fbm_product, 2, "COD-FBM-RET-1")
        vendor_order.action_confirm_cod_payment()
        line = vendor_order.line_ids.filtered(lambda l: not l.is_delivery)[:1]
        # Simulate a per-order commission having been charged on this line
        # (normally 0 -- commission is settled at the period instead -- but the
        # field is a plain stored Monetary, so this exercises the cod_reversal
        # branch without depending on that legacy per-order charging path).
        line.commission_amount = 10.0

        ret = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Wrong size",
            "line_ids": [Command.create({"vendor_line_id": line.id, "quantity": 1})],
        })
        ret.action_vendor_approve()
        ret.action_receive()

        self.assertEqual(ret.state, "refunded")
        relief = ret.wallet_transaction_id
        self.assertEqual(relief.type, "return")
        self.assertEqual(relief.amount, 5.0)  # half the line returned -> half the commission
        self.assertGreater(relief.amount, 0.0)  # positive = debt relief, never an increase

    def test_cod_fbm_return_accounting_reduces_vendor_debt(self):
        escrow, income, payable, receivable = self._configure_accounts()
        vendor_order = self._cod_order(self.fbm_product, 1, "COD-FBM-RET-2")
        vendor_order.action_confirm_cod_payment()
        line = vendor_order.line_ids.filtered(lambda l: not l.is_delivery)[:1]
        line.commission_amount = 8.0

        ret = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": line.id, "quantity": 1})],
        })
        ret.action_vendor_approve()
        ret.action_receive()

        move = ret.refund_move_id
        self.assertTrue(move)
        self.assertEqual(move.state, "posted")
        # No customer receivable at all for COD -- only the commission/payable wash.
        self.assertFalse(move.line_ids.filtered(lambda l: l.account_id == receivable))
        self.assertEqual(move.line_ids.filtered(lambda l: l.account_id == income).debit, 8.0)
        self.assertEqual(move.line_ids.filtered(lambda l: l.account_id == payable).credit, 8.0)
