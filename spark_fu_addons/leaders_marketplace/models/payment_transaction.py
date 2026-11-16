from odoo import api, models


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    @api.model_create_multi
    def create(self, vals_list):
        """A storefront order pays on Odoo's page and then lands on our return route, which sends the shopper on to
        the Next.js confirmation (or payment) page."""
        for vals in vals_list:
            ids = []
            for command in vals.get("sale_order_ids") or []:
                if command[0] == 6:
                    ids += list(command[2])
                elif command[0] == 4:
                    ids.append(command[1])
            order = self.env["sale.order"].sudo().browse(ids).filtered("leaders_public_ref")[:1]
            if order and order.leaders_sf_payment and order.leaders_sf_payment != "cod":
                vals["landing_route"] = "/orders/%s/payment/return?locale=%s" % (
                    order.leaders_public_ref, order.leaders_sf_locale or "en")
            equipment = self.env["sale.order"].sudo().browse(ids).leaders_equipment_request_id[:1]
            if equipment.return_url:  # an equipment request paid on Odoo's page goes back to the storefront request page
                vals["landing_route"] = "/equipment-requests/%s/payment/return" % equipment.name
        return super().create(vals_list)

    def _post_process(self):
        res = super()._post_process()
        vendor_orders = self.filtered(lambda tx: tx.state == "done").sale_order_ids.vendor_order_ids
        if vendor_orders:
            self.env["leaders.marketplace.accounting"].sudo().reconcile_collection(vendor_orders)
        return res
