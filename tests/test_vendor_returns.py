from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .test_vendor_split import MarketplaceVendorCommon


@tagged("post_install", "-at_install")
class TestVendorReturns(MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["leaders.commission.rule"].create({
            "name": "10% vendor", "scope": "vendor", "vendor_id": cls.vendor.id,
            "rule_type": "percent", "percent": 10.0,
        })

    def _shipped_order(self, product, qty):
        order = self._order([(product, qty)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        for group in vendor_order.fulfillment_group_ids:
            group.tracking_ref = "TRK"
            group.action_ship()
        return vendor_order

    def _request(self, vendor_order, qty, line=None):
        line = line or vendor_order.line_ids[:1]
        return self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id,
            "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": line.id, "quantity": qty})],
        })

    def _vendor_stock(self, product, location):
        return sum(self.env["stock.quant"].search([
            ("product_id", "=", product.id), ("location_id", "=", location.id),
            ("owner_id", "=", self.vendor.id)]).mapped("quantity"))

    # ------------------------------------------------------------ validation
    def test_cannot_return_before_shipping_or_more_than_delivered(self):
        order = self._order([(self.fbm_product, 2)])
        order.action_confirm()
        with self.assertRaises(ValidationError):
            self._request(order.vendor_order_ids, 1)

        vendor_order = self._shipped_order(self.fbm_product, 2)
        with self.assertRaises(ValidationError):
            self._request(vendor_order, 3)
        self._request(vendor_order, 2)
        with self.assertRaises(ValidationError):
            self._request(vendor_order, 1)  # already fully engaged in an open return

    def test_return_window(self):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.return_window_days", 5)
        vendor_order = self._shipped_order(self.fbm_product, 1)
        vendor_order.date_shipped = fields.Datetime.now() - timedelta(days=6)
        with self.assertRaises(ValidationError):
            self._request(vendor_order, 1)

    # --------------------------------------------------------------- happy path
    def test_vendor_approves_goods_back_and_refund_before_escrow_release(self):
        vendor_order = self._shipped_order(self.fbm_product, 2)
        self.assertEqual(vendor_order.commission_amount, 0.0)
        ret = self._request(vendor_order, 1)
        self.assertEqual(ret.refund_amount, 100.0)
        self.assertEqual(ret.commission_refund, 0.0)
        self.assertEqual(ret.vendor_deduction, 100.0)

        ret.action_vendor_approve(note="OK")
        self.assertEqual(ret.state, "approved")
        receipt = ret.picking_ids
        self.assertEqual(receipt.location_dest_id, self.vendor.vendor_location_id)
        self.assertEqual(receipt.picking_type_id.warehouse_id, self.vendor.vendor_warehouse_id)
        stock_before = self._vendor_stock(self.fbm_product, self.vendor.vendor_location_id)

        ret.action_receive()
        self.assertEqual(ret.state, "refunded")
        self.assertEqual(receipt.state, "done")
        self.assertEqual(self._vendor_stock(self.fbm_product, self.vendor.vendor_location_id), stock_before + 1)
        self.assertEqual(vendor_order.line_ids.qty_returned, 1)

        # Commission goes back to the vendor; net reflects the refund.
        self.assertEqual(vendor_order.refunded_amount, 100.0)
        self.assertEqual(vendor_order.commission_amount, 0.0)
        self.assertEqual(vendor_order.vendor_net, 100.0)

        credit_note = ret.credit_note_id
        self.assertEqual(credit_note.move_type, "out_refund")
        self.assertEqual(credit_note.amount_total, 100.0)
        self.assertEqual(credit_note.reversed_invoice_id, vendor_order.invoice_id)
        self.assertIn("/RINV/", credit_note.name)

        # No earning yet, so no wallet deduction: the escrow simply shrinks.
        self.assertFalse(ret.wallet_transaction_id)
        vendor_order.fulfillment_group_ids.action_delivered()
        earning = self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "earning")])
        # Orders stay gross (commission is only realized at settlement), so the
        # earning is the full net-of-return amount, with nothing withheld.
        self.assertEqual(earning.amount, 100.0)

    def test_return_after_escrow_release_deducts_wallet_and_uses_reserve(self):
        tier = self.env["leaders.vendor.tier"].create({
            "name": "Reserve Tier", "code": "reserve_test", "sequence": 99,
            "escrow_release_policy": "on_deliver", "reserve_percent": 10.0, "return_policy": "reserve_first",
        })
        self.vendor.vendor_tier_id = tier
        vendor_order = self._shipped_order(self.fbm_product, 2)
        vendor_order.fulfillment_group_ids.action_delivered()
        self.assertEqual(vendor_order.state, "completed")
        reserve = self.env["leaders.vendor.reserve"].search([("vendor_order_id", "=", vendor_order.id)])
        # Orders stay gross: 10% of the full 200 net, not of a commission-reduced net.
        self.assertEqual(reserve.amount, 20.0)

        ret = self._request(vendor_order, 1)
        ret.action_vendor_approve()
        ret.action_receive()
        tx = ret.wallet_transaction_id
        self.assertEqual((tx.type, tx.amount, tx.state), ("return", -100.0, "available"))
        # The reserve only holds 20, so it caps the consumption there even
        # though the return's deduction (100) is bigger.
        self.assertEqual(reserve.consumed_amount, 20.0)
        self.assertEqual(reserve.state, "consumed")

    # ------------------------------------------------------------ escalation
    def test_vendor_rejects_customer_escalates_platform_decides(self):
        vendor_order = self._shipped_order(self.fbm_product, 1)
        ret = self._request(vendor_order, 1)
        with self.assertRaises(UserError):
            ret.action_vendor_reject()  # a reason is mandatory
        ret.action_vendor_reject(note="Used product")
        self.assertEqual(ret.state, "rejected")
        ret.action_escalate()
        self.assertEqual(ret.state, "escalated")
        ret.action_platform_approve()
        self.assertEqual((ret.state, ret.approved_by), ("approved", "platform"))

        other = self._request(self._shipped_order(self.fbm_product, 1), 1)
        other.action_escalate()
        with self.assertRaises(UserError):
            other.action_platform_reject()  # decision note required
        other.decision_note = "Outside policy"
        other.action_platform_reject()
        self.assertEqual(other.state, "closed")

    def test_unanswered_request_escalates_automatically(self):
        ret = self._request(self._shipped_order(self.fbm_product, 1), 1)
        ret.vendor_deadline = fields.Datetime.now() - timedelta(hours=1)
        self.env["leaders.vendor.return"].cron_escalate_overdue()
        self.assertEqual(ret.state, "escalated")

    # ------------------------------------------------------------------- FBL
    def test_fbl_return_received_by_warehouse(self):
        vendor_order = self._shipped_order(self.fbl_product, 2)
        ret = self._request(vendor_order, 2)
        ret.action_vendor_approve()
        receipt = ret.picking_ids
        self.assertEqual(receipt.location_dest_id, self.vendor.vendor_fbl_location_id)
        self.assertEqual(receipt.picking_type_id.warehouse_id, self.main_wh)
        # The warehouse validates the receipt from the normal stock screens.
        for move in receipt.move_ids:
            move.quantity = move.product_uom_qty
        receipt.move_ids.picked = True
        receipt._action_done()
        self.assertEqual(ret.state, "refunded")
        self.assertEqual(self._vendor_stock(self.fbl_product, self.vendor.vendor_fbl_location_id), 3)

    # ------------------------------------------------------------ accounting
    def test_refund_accounting_unpaid_customer(self):
        escrow, income, payable, receivable = self._configure_accounts()
        vendor_order = self._shipped_order(self.fbm_product, 2)
        ret = self._request(vendor_order, 1)
        ret.action_vendor_approve()
        ret.action_receive()
        move = ret.refund_move_id
        self.assertEqual(move.line_ids.filtered(lambda l: l.account_id == escrow).debit, 100.0)
        refund_line = move.line_ids.filtered(lambda l: l.account_id == receivable)
        self.assertEqual(refund_line.credit, 100.0)
        # Customer had not paid: the refund lowers what they owe, no payment needed.
        self.assertTrue(refund_line.reconciled)
        self.assertFalse(ret.refund_payment_id)
        self.assertEqual(ret.credit_note_id.payment_state, "paid")

        vendor_order.fulfillment_group_ids.action_delivered()
        earning = vendor_order.earning_move_id
        self.assertEqual(earning.line_ids.filtered(lambda l: l.account_id == escrow).debit, 100.0)
        # Orders stay gross: no per-order commission, so no income line at all.
        self.assertFalse(earning.line_ids.filtered(lambda l: l.account_id == income))
        self.assertEqual(earning.line_ids.filtered(lambda l: l.account_id == payable).credit, 100.0)
        escrow_balance = sum(self.env["account.move.line"].search([
            ("account_id", "=", escrow.id), ("parent_state", "=", "posted"),
            ("move_id", "in", (vendor_order.collection_move_id | move | earning).ids)]).mapped("balance"))
        self.assertAlmostEqual(escrow_balance, 0.0)

    def test_refund_accounting_after_release_with_paid_customer(self):
        escrow, income, payable, receivable = self._configure_accounts()
        vendor_order = self._shipped_order(self.fbm_product, 2)
        # Customer pays the collection receivable.
        bank = self.env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", self.company.id)], limit=1)
        payment = self.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": self.customer.id,
            "amount": 200.0, "journal_id": bank.id,
        })
        payment.action_post()
        (payment.move_id.line_ids.filtered(lambda l: l.account_id == receivable)
         + vendor_order.collection_move_id.line_ids.filtered(lambda l: l.account_id == receivable)).reconcile()
        vendor_order.fulfillment_group_ids.action_delivered()

        ret = self._request(vendor_order, 1)
        ret.action_vendor_approve()
        ret.action_receive()
        move = ret.refund_move_id
        # Orders stay gross: no commission was ever withheld, so nothing is
        # given back through an income line -- the vendor bears the full refund.
        self.assertEqual(move.line_ids.filtered(lambda l: l.account_id == payable).debit, 100.0)
        self.assertFalse(move.line_ids.filtered(lambda l: l.account_id == income))
        self.assertEqual(move.line_ids.filtered(lambda l: l.account_id == receivable).credit, 100.0)

        refund_payment = ret.refund_payment_id
        self.assertEqual((refund_payment.payment_type, refund_payment.amount, refund_payment.state),
                         ("outbound", 100.0, "draft"))
        self.assertEqual(ret.credit_note_id.payment_state, "not_paid")
        refund_payment.action_post()
        self.assertTrue(move.line_ids.filtered(lambda l: l.account_id == receivable).reconciled)
        self.assertEqual(ret.credit_note_id.payment_state, "paid")
