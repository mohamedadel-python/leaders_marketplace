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


# What the storefront shows the customer for each stage (controllers/storefront_equipment.py STATUS): the admin list
# shows the same word, so both sides talk about the same step. A test keeps the two tables equal.
CUSTOMER_STATUS = {"pending": "requested", "offered": "awaitingPayment", "inProgress": "confirmed",
                   "executing": "inProgress", "reviewing": "requested", "contacting": "requested",
                   "completed": "completed", "cancelled": "cancelled"}
CUSTOMER_STATUS_LABELS = [("requested", "Requested"), ("awaitingPayment", "Awaiting payment"),
                          ("confirmed", "Confirmed"), ("inProgress", "In progress"), ("completed", "Completed"),
                          ("cancelled", "Cancelled"), ("confirming", "Accepted, awaiting payment")]


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
    rent_delivery_fee = fields.Monetary(
        "Delivery fee", currency_field="currency_id", copy=False,
        help="The delivery fee of a rental, asked at booking and collected in cash: no quotation is made for it.")
    rent_price_day = fields.Monetary("Rent per day", related="rental_id.customer_price_day", readonly=False,
                                     currency_field="currency_id")
    rent_date_start = fields.Date("From", related="rental_id.date_start")
    rent_date_end = fields.Date("To", related="rental_id.date_end")
    rent_days = fields.Integer("Days", compute="_compute_rent_total")
    rent_total = fields.Monetary("Rent total", compute="_compute_rent_total", currency_field="currency_id",
                                 help="The days of the rental at the rent per day, with the delivery fee.")
    vendor_rent_total = fields.Monetary("Vendor's rent price", currency_field="currency_id", copy=False,
                                        help="The price of the whole rental period, written by the vendor.")
    rent_invoice_id = fields.Many2one("account.move", "Rent invoice", readonly=True, copy=False, ondelete="set null")
    included_rental_id = fields.Many2one(
        "leaders.rental", "Included in rental", readonly=True, copy=False, ondelete="set null",
        help="A maintenance request of a renter whose contract includes maintenance: free, a contractual duty.")

    customer_status = fields.Selection(
        CUSTOMER_STATUS_LABELS, "Customer sees", compute="_compute_customer_status",
        help="The stage the customer sees on the website for this request.")

    @api.depends("state")
    def _compute_customer_status(self):
        for rec in self:
            rec.customer_status = CUSTOMER_STATUS.get(rec.state, rec.state if rec.state == "confirming" else False)

    equipment_vendor_services = fields.Char(
        "Services by the vendor", compute="_compute_equipment_service_split",
        help="The asked services the vendor does himself: not priced by the platform.")
    equipment_platform_services = fields.Char(
        "Services by the platform", compute="_compute_equipment_service_split",
        help="The asked services the platform prices and does.")

    @api.depends("equipment_services", "product_tmpl_id.leaders_own_service_ids.slug")
    def _compute_equipment_service_split(self):
        for rec in self:
            asked = [s for s in (rec.equipment_services or "").split(",") if s]
            tmpl = rec.sudo().product_tmpl_id
            own = set(tmpl.leaders_own_service_ids.mapped("slug"))
            rec.equipment_vendor_services = ",".join(s for s in asked if s in own) or False
            rec.equipment_platform_services = ",".join(s for s in asked if s not in own) or False

    vendor_services_amount = fields.Monetary(
        "Price of the vendor's services", currency_field="currency_id", copy=False,
        help="Written by the vendor for the services he does himself. The buyer pays it to the vendor directly; the "
             "platform does not collect it.")
    equipment_platform_service_names = fields.Char(
        "Services by the platform", compute="_compute_equipment_service_names",
        help="Names of the asked services the platform prices and does.")
    equipment_vendor_service_names = fields.Char(
        "Services by the vendor", compute="_compute_equipment_service_names",
        help="Names of the asked services the vendor does himself; the price is agreed with the buyer.")

    @api.depends("equipment_platform_services", "equipment_vendor_services")
    def _compute_equipment_service_names(self):
        for rec in self:
            for field, slugs in (("equipment_platform_service_names", rec.equipment_platform_services),
                                 ("equipment_vendor_service_names", rec.equipment_vendor_services)):
                rec[field] = ", ".join(rec._leaders_service_names((slugs or "").split(","))) or False

    def _leaders_service_names(self, slugs=None):
        """Names (in the reader's language) of the asked services, platform pages and the vendor's own alike."""
        self.ensure_one()
        slugs = [s for s in (slugs if slugs is not None else (self.equipment_services or "").split(",")) if s]
        pages = self.env["product.template"].sudo().search(
            [("leaders_service_page", "=", True), ("leaders_slug", "in", slugs)])
        names = {p.leaders_slug: p.name for p in pages}
        own = self.env["leaders.vendor.service"].sudo().with_context(active_test=False).search([("slug", "in", slugs)])
        names.update({o.slug: o.name for o in own})
        return [names.get(s, s) for s in slugs]

    @api.depends("rental_id.date_start", "rental_id.date_end", "rental_id.customer_price_day", "rent_delivery_fee")
    def _compute_rent_total(self):
        for rec in self:
            rental = rec.rental_id
            days = max((rental.date_end - rental.date_start).days, 0) if rental else 0
            rec.rent_days = days
            rec.rent_total = (rental.customer_price_day * days if rental else 0.0) + (rec.rent_delivery_fee or 0.0)

    def _leaders_create_rent_invoice(self):
        """The invoice of the platform to the renter, made when the rental request is completed: the days at the rent
        per day, and the delivery fee when there was one."""
        for rec in self.sudo():
            rental = rec.rental_id
            if rec.kind != "rent" or rec.rent_invoice_id or not rental or not rec.rent_total:
                continue
            machine = rec.product_tmpl_id.display_name or rec.title
            lines = []
            if rec.rent_days and rental.customer_price_day:
                lines.append((0, 0, {
                    "product_id": self.env.ref("leaders_marketplace.product_product_equipment_rental").id,
                    "name": _("Rental of %(machine)s from %(start)s to %(end)s", machine=machine,
                              start=rental.date_start, end=rental.date_end),
                    "quantity": rec.rent_days, "price_unit": rental.customer_price_day, "tax_ids": [(5, 0, 0)]}))
            if rec.rent_delivery_fee:
                lines.append((0, 0, {
                    "product_id": self.env.ref("leaders_marketplace.product_product_marketplace_delivery").id,
                    "name": _("Delivery of %s", machine), "quantity": 1, "price_unit": rec.rent_delivery_fee,
                    "tax_ids": [(5, 0, 0)]}))
            if not lines:
                continue
            try:
                with self.env.cr.savepoint():
                    invoice = self.env["account.move"].create({
                        "move_type": "out_invoice", "partner_id": rec.partner_id.commercial_partner_id.id,
                        "invoice_origin": rec.name, "ref": rec.name, "invoice_line_ids": lines})
                    invoice.action_post()
            except Exception as error:  # the request is closed anyway; the invoice is made again by hand
                rec.message_post(body=_("The invoice of the rental could not be made: %s", error))
                continue
            rec.rent_invoice_id = invoice
            rec.message_post(body=_("The invoice %(invoice)s was made for %(amount)s.", invoice=invoice.name,
                                    amount=invoice.amount_total))

    def action_send_rent_price(self):
        """The rent price reaches the renter: the request becomes an offer he accepts (and pays the delivery in cash)."""
        for rec in self:
            if rec.kind != "rent" or not rec.rental_id:
                raise UserError(_("Only a rental request has a rent price to send."))
            if rec.rental_id.customer_price_day <= 0:
                raise UserError(_("Enter the rent per day first."))
            if rec.state in ("pending", "reviewing", "contacting"):
                rec.state = "offered"
                rec.message_post(body=_("The rent price was sent to the renter."))

    def action_rent_invoice(self):
        self.ensure_one()
        self._leaders_create_rent_invoice()
        if not self.rent_invoice_id:
            raise UserError(_("There is nothing to invoice on this request."))
        return {"type": "ir.actions.act_window", "res_model": "account.move", "res_id": self.rent_invoice_id.id,
                "view_mode": "form", "target": "current"}

    def write(self, vals):
        res = super().write(vals)
        if vals.get("state") == "completed":  # a finished rental is invoiced
            self.filtered(lambda r: r.kind == "rent" and not r.rent_invoice_id)._leaders_create_rent_invoice()
        return res

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
            if rec.kind not in ("purchase", "quote") or not rec.equipment_platform_services:
                raise UserError(_("Only a purchase or quote request with services is priced here."))
            if rec.services_amount <= 0:
                raise UserError(_("Enter the price of the services first."))
            if rec.kind == "quote":  # the services join the offer; it goes out with the vendor's price
                if rec.quote_order_id:
                    rec._leaders_sync_service_lines()
                rec._leaders_send_vendor_offer()
                continue
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
        """Also closes an equipment quote whose offer was paid, and a service request once the service was done
        (stage executing): the customer then sees it completed."""
        if self.filtered(lambda r: r.kind not in ("purchase", "rent", "quote", "service")):
            raise UserError(_("Only equipment purchase, rent, quote and service requests have these steps."))
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
