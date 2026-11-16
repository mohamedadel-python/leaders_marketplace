"""Equipment is not added to the cart (project guide, feature 4). It is ordered with a request:

* priced, no service: a purchase request, nothing to pay; the platform follows it up;
* priced, with services (installation, delivery, inspection): the platform prices the services and the customer
  confirms the request by paying them (the payment is the confirmation, there is no separate deposit);
* no price: a quote request (the existing quote flow);
* rent: a booking in the platform's name, paid at booking only for the delivery when the renter asks for it.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

EQUIPMENT_SERVICES = ("installation", "delivery", "inspection")  # kept for older callers; see leaders_service_ids


class ProductTemplateServices(models.Model):
    _inherit = "product.template"

    leaders_service_ids = fields.Many2many(
        "product.template", "leaders_product_service_rel", "product_tmpl_id", "service_tmpl_id",
        string="Available services", domain=[("leaders_service_page", "=", True)],
        help="The services a customer can ask with this product (inspection, installation, delivery...). "
             "They are shown on the product page and accepted with the purchase or rent request.")

    def _leaders_available_services(self):
        """Published service pages chosen on the product."""
        self.ensure_one()
        return self.sudo().leaders_service_ids.filtered(lambda s: s.is_published and s.leaders_slug)


class LeadersCustomerRequestEquipment(models.Model):
    _inherit = "leaders.customer.request"

    kind = fields.Selection(selection_add=[("purchase", "Equipment purchase"), ("rent", "Equipment rent")],
                            ondelete={"purchase": "cascade", "rent": "cascade"})
    offer_id = fields.Many2one("leaders.marketplace.offer", "Seller offer", ondelete="set null")
    unit_price = fields.Monetary("Unit price", currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)
    equipment_services = fields.Char("Services asked", help="Comma separated: installation, delivery, inspection.")
    services_amount = fields.Monetary("Price of the services", currency_field="currency_id",
                                      help="Set by the platform; the customer confirms the request by paying it.")
    payment_order_id = fields.Many2one("sale.order", "Payment", readonly=True, copy=False, ondelete="set null")
    rental_id = fields.Many2one("leaders.rental", "Rental", readonly=True, copy=False, ondelete="set null")
    shipping_state_id = fields.Many2one("res.country.state", "Governorate")
    shipping_address = fields.Char("Address")
    rent_period = fields.Selection([("day", "Day"), ("week", "Week"), ("month", "Month")], "Rent period",
                                   readonly=True, copy=False, help="How the customer booked the rental.")
    rent_units = fields.Integer("Rent periods", readonly=True, copy=False,
                                help="How many rent periods were booked (3 with a period of week: 3 weeks).")
    return_url = fields.Char("Return page after paying", readonly=True, copy=False,
                             help="Storefront page the customer lands on after paying (?payment=success|failed is added).")
    included_rental_id = fields.Many2one(
        "leaders.rental", "Included in rental", readonly=True, copy=False, ondelete="set null",
        help="A maintenance request of a renter whose contract includes maintenance: free, a contractual duty.")

    def _leaders_link_included_maintenance(self):
        """Maintenance asked by a renter whose running rental includes maintenance is free (part of the contract)."""
        for rec in self.filtered(lambda r: r.kind == "service" and r.service == "maintenance" and r.partner_id):
            rental = self.env["leaders.rental"].sudo().search([
                ("partner_id", "=", rec.partner_id.commercial_partner_id.id), ("state", "=", "with_customer"),
                ("maintenance_included", "=", True)], limit=1)
            if rental:
                rec.included_rental_id = rental
                rec.message_post(body=_("Maintenance included in the rental %s: no charge.", rental.name))

    def action_send_service_prices(self):
        """Price the services of a purchase request: the customer gets an order to pay, which confirms the request."""
        for rec in self:
            if rec.kind != "purchase" or not rec.equipment_services:
                raise UserError(_("Only a purchase request with services is priced here."))
            if rec.services_amount <= 0:
                raise UserError(_("Enter the price of the services first."))
            rec.payment_order_id = rec._leaders_payment_order(_("Services for %s: %s", rec.title, rec.equipment_services),
                                                             rec.services_amount)
            rec.state = "offered"

    # ---- stages of an equipment request, run by the platform (the customer confirms receipt from the website)
    def _leaders_equipment_only(self):
        if self.filtered(lambda r: r.kind not in ("purchase", "rent")):
            raise UserError(_("Only equipment purchase and rent requests have these steps."))

    def action_equipment_confirm(self):
        """Confirm a request that needs no payment (a purchase without services)."""
        self._leaders_equipment_only()
        for rec in self.filtered(lambda r: r.state in ("pending", "reviewing", "contacting")):
            rec.state = "inProgress"

    def action_equipment_execute(self):
        """The platform starts carrying out a confirmed request (delivery, installation...)."""
        self._leaders_equipment_only()
        for rec in self.filtered(lambda r: r.state == "inProgress"):
            rec.state = "executing"

    def action_equipment_complete(self):
        self._leaders_equipment_only()
        for rec in self.filtered(lambda r: r.state in ("inProgress", "executing")):
            rec.state = "completed"

    def _leaders_payment_order(self, label, amount):
        self.ensure_one()
        product = self.env.ref("leaders_marketplace.product_product_marketplace_delivery")
        order = self.env["sale.order"].sudo().create({
            "partner_id": self.partner_id.id, "require_payment": True, "prepayment_percent": 1.0,
            "leaders_equipment_request_id": self.id,
            "order_line": [(0, 0, {"product_id": product.id, "name": label, "product_uom_qty": 1,
                                   "price_unit": amount, "tax_id": [(5, 0, 0)]})]})
        order.leaders_public_ref_get()
        return order


class SaleOrderEquipmentRequest(models.Model):
    _inherit = "sale.order"

    leaders_equipment_request_id = fields.Many2one("leaders.customer.request", "Equipment request", copy=False,
                                                   index=True, ondelete="set null")

    def action_confirm(self):
        res = super().action_confirm()
        # paying the services (or the rental delivery) confirms the equipment request
        for order in self.filtered("leaders_equipment_request_id"):
            request = order.leaders_equipment_request_id.sudo()
            if request.state in ("pending", "offered"):
                request.state = "inProgress"
        return res
