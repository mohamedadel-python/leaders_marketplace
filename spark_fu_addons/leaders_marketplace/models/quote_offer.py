"""Quote offer & payment (frontend contract of 2026-11-12): the offer of a quote or service request is an
Odoo quotation (sale.order) linked to the request.

Each line says who sells it: a seller offer (marketplace_offer_id) makes it a vendor line, moved to a vendor
order when the quotation is confirmed; no offer keeps it on the platform. What is due now is paid online on
Odoo's payment page (the quotation's prepayment); the rest is paid to the platform later (cash on delivery),
so commission and settlement follow the storefront orders.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

OFFER_KINDS = ("quote", "service")


class LeadersCustomerRequestOffer(models.Model):
    _inherit = "leaders.customer.request"

    state = fields.Selection(selection_add=[("confirming", "Accepted, awaiting payment"), ("executing",)],
                             ondelete={"confirming": "set default"})
    quote_order_id = fields.Many2one("sale.order", "Offer (quotation)", copy=False, ondelete="set null", index=True)
    offer_accepted_at = fields.Datetime("Offer accepted on", readonly=True, copy=False)
    offer_paid_at = fields.Datetime("Paid on", readonly=True, copy=False,
                                    help="Set when the payment gateway confirms what was due now.")
    needed_by = fields.Date("Needed by")
    sf_payment_method = fields.Selection(
        [("cash", "Cash on delivery"), ("card", "Card"), ("paypal", "PayPal"), ("applepay", "Apple Pay"),
         ("online", "Online")], "Payment method", readonly=True, copy=False,
        help="How the customer chose to pay the offer or the equipment request. Cash is collected on delivery, "
             "outside the payment gateway.")

    def action_create_offer(self):
        """Open the request's quotation, creating it from the requested products the first time."""
        self.ensure_one()
        if self.kind not in OFFER_KINDS:
            raise UserError(_("Only quotes and service requests get an offer."))
        order = self.quote_order_id
        if not order:
            lines = [(0, 0, {"product_id": line.product_tmpl_id.product_variant_id.id, "product_uom_qty": line.quantity})
                     for line in self.line_ids if line.product_tmpl_id.product_variant_id]
            if not lines and self.service_id.product_variant_id:
                lines = [(0, 0, {"product_id": self.service_id.product_variant_id.id, "product_uom_qty": 1})]
            order = self.env["sale.order"].sudo().create({
                "partner_id": self.partner_id.id, "leaders_quote_request_id": self.id, "order_line": lines})
            order.leaders_public_ref_get()
            self.quote_order_id = order
        return {"type": "ir.actions.act_window", "res_model": "sale.order", "res_id": order.id, "view_mode": "form",
                "target": "current"}

    def leaders_offer_open(self):
        """The offer is shown from the moment it is sent (stage offered) on."""
        self.ensure_one()
        return bool(self.quote_order_id) and self.state in ("offered", "confirming", "executing", "completed")


class SaleOrderQuoteOffer(models.Model):
    _inherit = "sale.order"

    leaders_quote_request_id = fields.Many2one("leaders.customer.request", "Quote request", copy=False,
                                               index=True, ondelete="set null")
    leaders_due_now_note = fields.Char("Note on what is due now", translate=True,
                                       help='Shown under the amount due now, e.g. "The delivery fee is not refunded '
                                            'once the machine has shipped."')

    def _leaders_offer_lines(self):
        return self.order_line.filtered(lambda l: not l.display_type)

    def action_leaders_send_offer(self):
        """The customer sees the offer on the storefront's track page and can accept it."""
        for order in self:
            request = order.leaders_quote_request_id.sudo()
            if not request:
                raise UserError(_("This quotation is not linked to a customer request."))
            if not order._leaders_offer_lines():
                raise UserError(_("Add at least one line to the offer."))
            if order.state == "draft":
                order.state = "sent"
            order.leaders_public_ref_get()
            if request.state not in ("offered", "confirming", "executing", "completed", "cancelled"):
                request.state = "offered"
        return True

    def _marketplace_vendor_lines(self):
        """On an offer the seller of each line is chosen by the platform's staff: an empty seller is the
        platform itself, never the Buy Box winner picked behind their back."""
        if self.leaders_quote_request_id:
            return self.order_line.filtered(lambda l: not l.display_type and l.marketplace_offer_id and not l.is_delivery)
        return super()._marketplace_vendor_lines()

    def action_confirm(self):
        res = super().action_confirm()
        # paying what was due now (or accepting when nothing was) starts the work
        for order in self.filtered("leaders_quote_request_id"):
            request = order.leaders_quote_request_id.sudo()
            if request.state == "confirming":
                request.state = "executing"
        return res


class SaleOrderLineQuoteOffer(models.Model):
    _inherit = "sale.order.line"

    leaders_optional = fields.Boolean("Optional", help="The customer may remove this line before accepting the offer.")
    leaders_due_now = fields.Boolean("Due now", help="Paid online to confirm the offer (delivery fee, deposit, the service).")
    leaders_offer_note = fields.Char("Customer note", help='Shown under the line on the storefront, e.g. "Rent: 30 days".')


class PaymentTransactionQuoteOffer(models.Model):
    _inherit = "payment.transaction"

    @api.model_create_multi
    def create(self, vals_list):
        txs = super().create(vals_list)
        for tx in txs:  # a quote offer paid on Odoo's page goes back to the storefront track page
            request = tx.sale_order_ids.leaders_quote_request_id[:1]
            if request:
                tx.landing_route = "/quotes/%s/payment/return" % request.name
        return txs

    def _post_process(self):
        res = super()._post_process()
        # paidAt comes only from a verified, done transaction (the gateway's notification), never from the browser
        for tx in self.filtered(lambda t: t.state == "done"):
            for request in tx.sale_order_ids.leaders_quote_request_id.sudo():
                if not request.offer_paid_at:
                    request.offer_paid_at = fields.Datetime.now()
                if request.state == "confirming":
                    request.state = "executing"
        return res
