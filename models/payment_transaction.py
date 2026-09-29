from odoo import models


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    def _post_process(self):
        res = super()._post_process()
        vendor_orders = self.filtered(lambda tx: tx.state == "done").sale_order_ids.vendor_order_ids
        if vendor_orders:
            self.env["leaders.marketplace.accounting"].sudo().reconcile_collection(vendor_orders)
        return res
