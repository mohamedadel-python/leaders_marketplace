from odoo import fields, models


class AccountPayment(models.Model):
    _inherit = "account.payment"

    leaders_return_id = fields.Many2one("leaders.vendor.return", index=True, copy=False, readonly=True)
    leaders_cancelled_order_id = fields.Many2one(
        "leaders.vendor.order", index=True, copy=False, readonly=True,
        string="Cancelled Marketplace Order",
        help="Vendor order this payment refunds after its cancellation.")

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
