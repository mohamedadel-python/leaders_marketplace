"""F2: cancelling the remainder of a partial shipment resizes the money.

Four combinations (online paid / online unpaid / COD FBM / COD FBL) x (earning
created after the cancel / earning already there). The ledger check must end
with escrow == 0, vendor payable == wallet unsettled and no customer receivable.
"""
from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_vendor_split import MarketplaceVendorCommon


class CancelRemainingCommon(MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.cod_enabled", "True")
        cls.Wallet = cls.env["leaders.wallet.transaction"]
        cls.Accounting = cls.env["leaders.marketplace.accounting"]
        tier = cls.env["leaders.vendor.tier"].create({
            "name": "F2 on ship tier", "code": "f2_on_ship", "escrow_release_policy": "on_ship", "reserve_percent": 0.0})
        cls.vendor.vendor_tier_id = tier

    # ------------------------------------------------------------- fixtures
    def _place(self, product, qty, cod=False, paid=False):
        self._configure_accounts()
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": product.id, "product_uom_qty": qty})],
        })
        if cod:
            order.leaders_payment_method = "cod"
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        if paid:
            journal = self.env["account.journal"].search(
                [("type", "=", "bank"), ("company_id", "=", self.company.id)], limit=1)
            payment = self.env["account.payment"].create({
                "payment_type": "inbound", "partner_type": "customer",
                "partner_id": self.customer.commercial_partner_id.id,
                "amount": vendor_order.amount_total, "currency_id": vendor_order.currency_id.id,
                "journal_id": journal.id})
            payment.action_post()
            receivable = vendor_order.collection_move_id.line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
            (receivable + payment.move_id.line_ids.filtered(
                lambda l: l.account_id in receivable.account_id and not l.reconciled)).reconcile()
        return vendor_order

    def _line(self, vendor_order):
        return vendor_order.line_ids.filtered(lambda l: not l.is_delivery)[:1]

    def _partial(self, vendor_order, shipped=1):
        group = vendor_order.fulfillment_group_ids
        group.action_ship(quantities={self._line(vendor_order).id: shipped}, tracking_ref="F2-1")
        self.assertEqual(group.state, "partially_shipped")
        return group

    def _ledger(self):
        return self.Accounting._leaders_check_ledger(self.vendor)

    def _receivable(self):
        lines = self.env["account.move.line"].search([
            ("account_id.account_type", "=", "asset_receivable"), ("parent_state", "=", "posted"),
            ("partner_id", "=", self.customer.commercial_partner_id.id)])
        return self.company.currency_id.round(sum(lines.mapped("balance")))

    def _earning(self, vendor_order):
        return self.Wallet.search([("vendor_order_id", "=", vendor_order.id), ("type", "=", "earning")])

    def _credit_notes(self, vendor_order):
        return vendor_order.credit_note_ids.filtered(lambda d: d.move_type == "out_refund")

    def _assert_settled_ledger(self, kept):
        ledger = self._ledger()
        self.assertEqual(ledger["escrow"], 0.0)
        self.assertEqual(ledger["payable"], kept)
        self.assertEqual(ledger["wallet"]["unsettled"], kept)
        self.assertEqual(ledger["payable"], ledger["wallet"]["unsettled"])
        self.assertEqual(self._receivable(), 0.0)


@tagged("post_install", "-at_install")
class TestCancelRemainingOnline(CancelRemainingCommon):

    def test_paid_online_earning_created_after_the_cancel(self):
        vendor_order = self._place(self.fbm_product, 4, paid=True)
        self.assertEqual(vendor_order.amount_total, 400.0)
        group = self._partial(vendor_order)
        self.assertFalse(self._earning(vendor_order))
        group.action_cancel_remaining()

        line = self._line(vendor_order)
        self.assertEqual(line.qty_cancelled, 3)
        self.assertEqual(line.product_uom_qty, 4)
        self.assertEqual((line.price_subtotal, line.price_total), (100.0, 100.0))
        self.assertEqual(vendor_order.amount_total, 100.0)
        self.assertEqual(vendor_order.vendor_net, 100.0)
        self.assertEqual(vendor_order.state, "fulfilled")
        # the earning (on_ship) uses the reduced vendor_net
        self.assertEqual(self._earning(vendor_order).amount, 100.0)
        # credit note for the cancelled quantity, numbered like a return credit note
        note = self._credit_notes(vendor_order)
        self.assertEqual(len(note), 1)
        self.assertIn("/RINV/", note.name)
        self.assertEqual((note.amount_total, note.line_ids.quantity), (300.0, 3))
        self.assertEqual(note.reversed_invoice_id, vendor_order.invoice_id)
        self.assertEqual(note.payment_state, "not_paid")
        # escrow refund entry + draft refund payment for what the customer paid
        payment = self.env["account.payment"].search([("leaders_cancel_refund_move_id", "!=", False)])
        self.assertEqual(len(payment), 1)
        self.assertEqual((payment.state, payment.payment_type, payment.amount), ("draft", "outbound", 300.0))
        self.assertEqual(self._receivable(), -300.0)
        payment.action_post()
        self._assert_settled_ledger(100.0)
        self.assertFalse(self.Wallet.search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "cancelled_qty")]))

    def test_paid_online_earning_already_created(self):
        vendor_order = self._place(self.fbm_product, 4, paid=True)
        group = self._partial(vendor_order)
        self.Wallet.create_vendor_earning(vendor_order)     # released before the cancel
        self.assertEqual(self._earning(vendor_order).amount, 400.0)
        self.assertEqual(self._ledger()["payable"], 400.0)

        group.action_cancel_remaining()

        adjustment = self.Wallet.search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "cancelled_qty")])
        self.assertEqual(len(adjustment), 1)
        self.assertEqual(adjustment.amount, -300.0)
        self.assertTrue(adjustment.move_id, "the adjustment carries its journal entry")
        self.assertEqual(adjustment.move_id.state, "posted")
        self.assertEqual(self._earning(vendor_order).amount, 400.0, "the earning itself is immutable")
        payment = self.env["account.payment"].search([("leaders_cancel_refund_move_id", "=", adjustment.move_id.id)])
        self.assertEqual((payment.state, payment.amount), ("draft", 300.0))
        payment.action_post()
        self._assert_settled_ledger(100.0)

    def test_unpaid_online_order_only_lowers_the_receivable(self):
        vendor_order = self._place(self.fbm_product, 4, paid=False)
        group = self._partial(vendor_order)
        group.action_cancel_remaining()
        self.assertEqual(vendor_order.amount_total, 100.0)
        self.assertFalse(self.env["account.payment"].search([("leaders_cancel_refund_move_id", "!=", False)]))
        self.assertEqual(self._receivable(), 100.0, "the customer now owes only the kept part")
        note = self._credit_notes(vendor_order)
        self.assertEqual(note.amount_total, 300.0)
        self.assertEqual(note.payment_state, "paid")
        ledger = self._ledger()
        self.assertEqual(ledger["escrow"], 0.0)
        self.assertEqual(ledger["payable"], ledger["wallet"]["unsettled"])

    def test_unpaid_online_order_with_earning_already_created(self):
        vendor_order = self._place(self.fbm_product, 4, paid=False)
        group = self._partial(vendor_order)
        self.Wallet.create_vendor_earning(vendor_order)
        group.action_cancel_remaining()
        self.assertEqual(self._receivable(), 100.0)
        ledger = self._ledger()
        self.assertEqual((ledger["escrow"], ledger["payable"], ledger["wallet"]["unsettled"]),
                         (0.0, 100.0, 100.0))

    def test_return_after_a_cancelled_remainder_uses_the_kept_price(self):
        vendor_order = self._place(self.fbm_product, 4, paid=True)
        group = self._partial(vendor_order)
        group.action_cancel_remaining()
        group.action_report_delivery()
        line = self._line(vendor_order)
        self.assertEqual(line._qty_returnable(), 1)
        ret = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": line.id, "quantity": 1})]})
        self.assertEqual(ret.refund_amount, 100.0, "not a quarter of the price of the kept unit")

    def test_idempotent_and_validated(self):
        vendor_order = self._place(self.fbm_product, 4, paid=True)
        group = self._partial(vendor_order)
        line = self._line(vendor_order)
        with self.assertRaises(UserError):      # more than is left (3)
            vendor_order._leaders_cancel_quantities({line: 4}, by="vendor")
        with self.assertRaises(UserError):
            vendor_order._leaders_cancel_quantities({line: -1}, by="vendor")
        self.assertEqual(line.qty_cancelled, 0)
        group.action_cancel_remaining()
        with self.assertRaises(UserError):
            group.action_cancel_remaining()
        with self.assertRaises(UserError):
            vendor_order._leaders_cancel_quantities({line: 3}, by="vendor")
        self.assertEqual(line.qty_cancelled, 3)
        self.assertEqual(len(self._credit_notes(vendor_order)), 1)
        self.assertEqual(vendor_order.amount_total, 100.0)
        self.assertFalse(vendor_order._leaders_cancel_quantities({}, by="vendor"))

    def test_cancelling_everything_is_a_normal_cancel(self):
        vendor_order = self._place(self.fbm_product, 2, paid=True)
        with self.assertRaises(UserError):
            vendor_order._leaders_cancel_quantities({self._line(vendor_order): 2}, by="platform")

    def test_back_office_method_needs_the_manager_group(self):
        vendor_order = self._place(self.fbm_product, 4, paid=True)
        self._partial(vendor_order)
        line = self._line(vendor_order)
        user = self.env["res.users"].create({
            "name": "Plain", "login": "f2-plain", "groups_id": [Command.set([self.env.ref("base.group_user").id])]})
        with self.assertRaises(Exception):
            vendor_order.with_user(user).action_cancel_quantities({line.id: 1})
        vendor_order.action_cancel_quantities({line.id: 1}, reason="support")
        self.assertEqual(line.qty_cancelled, 1)
        self.assertEqual(vendor_order.amount_total, 300.0)

    def test_warehouse_cancelled_backorder_resizes_the_money(self):
        vendor_order = self._place(self.fbm_product, 4, paid=True)
        group = self._partial(vendor_order)
        backorder = group.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        backorder.action_cancel()
        self.assertEqual(self._line(vendor_order).qty_cancelled, 3)
        self.assertEqual(vendor_order.amount_total, 100.0)
        self.assertEqual(group.state, "shipped")
        self.assertEqual(vendor_order.state, "fulfilled")
        self.assertEqual(len(self._credit_notes(vendor_order)), 1)
        self.assertTrue(any("warehouse" in (b or "") for b in vendor_order.message_ids.mapped("body")))
        # cancelling the (already cancelled) picking again changes nothing
        backorder.action_cancel()
        self.assertEqual(self._line(vendor_order).qty_cancelled, 3)

    def test_warehouse_cancelling_a_never_shipped_picking_is_not_this_path(self):
        vendor_order = self._place(self.fbm_product, 2, paid=True)
        group = vendor_order.fulfillment_group_ids
        group.picking_ids.action_cancel()
        self.assertEqual(self._line(vendor_order).qty_cancelled, 0)


@tagged("post_install", "-at_install")
class TestCancelRemainingCod(CancelRemainingCommon):

    def _cash_journal(self):
        journal = self.env["account.journal"].create({"name": "COD Cash", "code": "LMF2C", "type": "cash"})
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.cod_cash_journal_id", journal.id)

    def test_cod_fbm_only_the_expected_cash_shrinks(self):
        vendor_order = self._place(self.fbm_product, 4, cod=True)
        moves = self.env["account.move"].search_count([])
        group = self._partial(vendor_order)
        group.action_cancel_remaining()
        self.assertEqual(vendor_order.amount_total, 100.0)
        self.assertEqual(vendor_order.vendor_net, 100.0)
        self.assertEqual(self.env["account.move"].search_count([]), moves, "no accounting entry for COD")
        self.assertFalse(vendor_order.collection_move_id)
        self.assertEqual(self._credit_notes(vendor_order).amount_total, 300.0)
        self.assertEqual(self._credit_notes(vendor_order).payment_state, "paid")
        self.assertFalse(self.env["account.payment"].search([("leaders_cancel_refund_move_id", "!=", False)]))
        group.action_report_delivery()
        self.assertEqual(vendor_order.state, "completed")
        vendor_order.action_confirm_cod_payment()
        due = self.Wallet.search([("vendor_order_id", "=", vendor_order.id), ("type", "=", "cod_collected")])
        self.assertEqual(sum(due.mapped("amount")), 100.0)
        ledger = self._ledger()
        self.assertEqual((ledger["escrow"], ledger["payable"], ledger["wallet"]["unsettled"]), (0.0, 0.0, 0.0))

    def test_cod_fbm_cash_confirmed_before_the_cancel(self):
        vendor_order = self._place(self.fbm_product, 4, cod=True)
        group = self._partial(vendor_order)
        self.Wallet.create_vendor_cod_due(vendor_order)      # 400 already recorded as held cash
        group.action_cancel_remaining()
        due = self.Wallet.search([("vendor_order_id", "=", vendor_order.id), ("type", "=", "cod_collected")])
        self.assertEqual(sorted(due.mapped("amount")), [-300.0, 400.0])
        self.assertEqual(sum(due.mapped("amount")), 100.0)
        ledger = self._ledger()
        self.assertEqual((ledger["escrow"], ledger["payable"], ledger["wallet"]["unsettled"]), (0.0, 0.0, 0.0))
        self.assertEqual(self._receivable(), 0.0)

    def test_cod_fbl_books_the_kept_amount_at_delivery(self):
        self._cash_journal()
        vendor_order = self._place(self.fbl_product, 3, cod=True)
        self.assertEqual(vendor_order.amount_total, 120.0)
        group = vendor_order.fulfillment_group_ids
        group.action_ship(quantities={self._line(vendor_order).id: 1})
        self.assertEqual(group.state, "partially_shipped")
        self.assertFalse(vendor_order.collection_move_id)
        group.action_cancel_remaining()
        self.assertEqual(vendor_order.amount_total, 40.0)
        self.assertFalse(vendor_order.collection_move_id)
        group.action_delivered()
        self.assertEqual(vendor_order.state, "completed")
        self.assertEqual(vendor_order.collection_move_id.amount_total, 40.0)
        self.assertEqual(vendor_order.cod_payment_id.amount, 40.0)
        self.assertEqual(self._earning(vendor_order).amount, 40.0)
        self._assert_settled_ledger(40.0)

    def test_cod_with_a_booked_collection_is_refused(self):
        self._cash_journal()
        vendor_order = self._place(self.fbl_product, 3, cod=True)
        group = vendor_order.fulfillment_group_ids
        group.action_ship(quantities={self._line(vendor_order).id: 1})
        self.Accounting.post_collection_entry(vendor_order)
        with self.assertRaises(UserError):
            group.action_cancel_remaining()
        self.assertEqual(self._line(vendor_order).qty_cancelled, 0)

    def test_fbl_online_warehouse_cancel(self):
        vendor_order = self._place(self.fbl_product, 3, paid=True)
        group = vendor_order.fulfillment_group_ids
        picking = group.picking_ids
        picking.move_ids.quantity = 1
        picking.move_ids.picked = True
        picking.with_context(skip_sms=True)._action_done()
        backorder = group.picking_ids - picking
        backorder.action_cancel()
        self.assertEqual(vendor_order.amount_total, 40.0)
        self.assertEqual(group.state, "shipped")
        self.env["account.payment"].search([("leaders_cancel_refund_move_id", "!=", False)]).action_post()
        self._assert_settled_ledger(40.0)


@tagged("post_install", "-at_install")
class TestCancelRemainingCommission(CancelRemainingCommon):

    def _plan(self):
        plan = self.env["leaders.commission.rule"].create({
            "name": "F2 amount plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "amount_sold", "achieve_rate": 0.10,
        })
        plan.action_approve()

    def _settle(self, vendor_order):
        today = fields.Date.today()
        self.Wallet.search([("vendor_order_id", "=", vendor_order.id)]).write(
            {"state": "available", "available_date": today})
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": today, "period_to": today,
            "currency_id": self.company.currency_id.id})
        settlement.action_collect_transactions()
        return settlement

    def test_sales_base_uses_the_reduced_amount_earning_after(self):
        self._plan()
        vendor_order = self._place(self.fbm_product, 4, paid=True)
        self._partial(vendor_order).action_cancel_remaining()
        settlement = self._settle(vendor_order)
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 10.0,
                         "10% of the kept 100, not of the ordered 400")

    def test_sales_base_uses_the_reduced_amount_earning_before(self):
        self._plan()
        vendor_order = self._place(self.fbm_product, 4, paid=True)
        group = self._partial(vendor_order)
        self.Wallet.create_vendor_earning(vendor_order)
        group.action_cancel_remaining()
        settlement = self._settle(vendor_order)
        self.assertEqual(settlement.commission_achievement_ids.commission_amount, 10.0)
        self.assertIn("cancelled_qty", settlement.transaction_ids.mapped("type"))
