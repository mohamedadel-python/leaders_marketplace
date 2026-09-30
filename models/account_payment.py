from odoo import fields, models


class AccountPayment(models.Model):
    _inherit = "account.payment"

    leaders_return_id = fields.Many2one("leaders.vendor.return", index=True, copy=False, readonly=True)
    leaders_cancelled_order_id = fields.Many2one(
        "leaders.vendor.order", index=True, copy=False, readonly=True,
        string="Cancelled Marketplace Order",
        help="Vendor order this payment refunds after its cancellation.")

    leaders_cancel_refund_move_id = fields.Many2one(
        "account.move", index=True, copy=False, readonly=True,
        string="Cancelled Quantity Refund Entry",
        help="Refund entry of a cancelled remainder (part of a vendor order) this payment pays out.")

    leaders_cancelled_sale_id = fields.Many2one(
        "sale.order", index=True, copy=False, readonly=True,
        string="Cancelled Company Order",
        help="Company sales order this payment refunds after the customer cancelled it.")

    def action_post(self):
        res = super().action_post()
        for payment in self.filtered("leaders_cancelled_order_id"):
            # Match the refund with the credit the cancellation reversal left
            # on the customer's receivable, so nothing stays open.
            reversal = payment.leaders_cancelled_order_id.collection_reversal_move_id
            credit = reversal.line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
            pay_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id in credit.account_id and not l.reconciled)
            if credit and pay_lines:
                (credit + pay_lines).reconcile()
        for payment in self.filtered("leaders_cancelled_sale_id"):
            # Match the refund with the customer's credit: the posted credit note
            # of the cancelled invoice, else what the online payment left open.
            sale = payment.leaders_cancelled_sale_id
            credit_notes = self.env["account.move"].search([
                ("reversed_entry_id", "in", sale.invoice_ids.ids),
                ("move_type", "=", "out_refund"), ("state", "=", "posted")])
            payments = sale.transaction_ids.filtered(lambda t: t.state == "done").payment_id
            credit = (credit_notes | payments.move_id).line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable"
                and not l.reconciled and l.credit)
            pay_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id in credit.account_id and not l.reconciled)
            credit = credit.filtered(lambda l: l.account_id in pay_lines.account_id)
            if credit and pay_lines:
                (credit + pay_lines).reconcile()
        for payment in self.filtered("leaders_cancel_refund_move_id"):
            receivable = payment.leaders_cancel_refund_move_id.line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
            pay_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id in receivable.account_id and not l.reconciled)
            if receivable and pay_lines:
                (receivable + pay_lines).reconcile()
        for payment in self.filtered("leaders_return_id"):
            ret = payment.leaders_return_id
            receivable = ret.refund_move_id.line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
            pay_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id in receivable.account_id and not l.reconciled)
            if receivable and pay_lines:
                (receivable + pay_lines).reconcile()
            if ret.credit_note_id:
                ret.credit_note_id.payment_state = "paid"
        return res
