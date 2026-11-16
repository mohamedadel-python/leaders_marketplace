"""Storefront (Next.js) state: guest/user carts and the customer's quote and service requests."""
import hashlib
import json
import secrets

from odoo import api, fields, models

REQUEST_STATES = [("pending", "Pending"), ("inProgress", "In progress"), ("reviewing", "Reviewing"),
                  ("contacting", "Contacting"), ("offered", "Offered"), ("executing", "Being carried out"),
                  ("completed", "Completed"), ("cancelled", "Cancelled")]
QUOTE_STAGE = {"pending": "sent", "inProgress": "reviewing"}
RENT_PERIODS = [("day", "Day"), ("week", "Week"), ("month", "Month"), ("custom", "Custom")]
SERVICES = ("inspection", "operator", "insurance", "delivery")


class LeadersCart(models.Model):
    _name = "leaders.cart"
    _description = "Storefront Cart"
    _order = "write_date desc"

    token = fields.Char(index=True, copy=False, default=lambda self: secrets.token_urlsafe(24))
    user_id = fields.Many2one("res.users", index=True, ondelete="cascade")
    line_ids = fields.One2many("leaders.cart.line", "cart_id")

    _sql_constraints = [("user_unique", "unique(user_id)", "A user has one cart."),
                        ("token_unique", "unique(token)", "Cart token must be unique.")]

    def _touch(self):
        """Bump ``write_date`` (an empty write does not)."""
        self.write({"token": self.token})

    @api.autovacuum
    def _gc_guest_carts(self):
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=30)
        self.search([("user_id", "=", False), ("write_date", "<", limit)]).unlink()


class LeadersCartLine(models.Model):
    _name = "leaders.cart.line"
    _description = "Storefront Cart Line"
    _order = "id"

    cart_id = fields.Many2one("leaders.cart", required=True, ondelete="cascade", index=True)
    product_tmpl_id = fields.Many2one("product.template", required=True, ondelete="cascade")
    quantity = fields.Integer(default=1)
    listing = fields.Selection([("buy", "Buy"), ("rent", "Rent")], required=True, default="buy")
    rent_period = fields.Selection(RENT_PERIODS)
    source = fields.Selection([("turkish", "Turkish"), ("chinese", "Chinese"), ("egyptian", "Egyptian")])
    services = fields.Char(help="Comma separated: inspection, operator, insurance.")
    offer_id = fields.Many2one("leaders.marketplace.offer", ondelete="set null",
                               help="The seller the customer picked; empty: the Buy Box winner at checkout.")
    line_key = fields.Char(compute="_compute_line_key", store=True, index=True)
    platform_seller = fields.Boolean(help="The customer picked the platform's own stock (offerId \"platform\").")

    @api.depends("product_tmpl_id", "listing", "rent_period", "source", "services", "offer_id", "platform_seller")
    def _compute_line_key(self):
        for line in self:
            line.line_key = self.make_key(line.product_tmpl_id.id, line.listing, line.rent_period,
                                          line.source, (line.services or "").split(","),
                                          "platform" if line.platform_seller else line.offer_id.id)

    @api.model
    def make_key(self, tmpl_id, listing, period, source, services, offer_id=None):
        raw = "|".join([str(tmpl_id), listing or "", period or "", source or "", ",".join(sorted(s for s in services if s))])
        if offer_id:  # a picked seller makes its own line; lines without one keep their old key
            raw += "|o%s" % offer_id
        return hashlib.sha1(raw.encode()).hexdigest()[:12]


class LeadersCustomerRequest(models.Model):
    """A quote request (SQ-) or a service order (SV-) shown on the customer's orders page."""
    _name = "leaders.customer.request"
    _description = "Customer Quote / Service Request"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char("Reference", readonly=True, copy=False, index=True)
    kind = fields.Selection([("quote", "Quote"), ("service", "Service")], required=True, index=True)
    partner_id = fields.Many2one("res.partner", index=True, ondelete="set null")
    phone = fields.Char()
    product_tmpl_id = fields.Many2one("product.template", ondelete="set null")
    title = fields.Char(required=True)
    quantity = fields.Integer(default=1)
    note = fields.Text()
    state = fields.Selection(REQUEST_STATES, default="pending", required=True, tracking=True, index=True)
    service = fields.Char(help="Service slug when the request is a service request.")
    service_details = fields.Text(help="JSON: the service request form.")
    history = fields.Text(help="JSON: stage -> ISO time it was reached.")
    line_ids = fields.One2many("leaders.customer.request.line", "request_id")

    @staticmethod
    def stage_of(state):
        return QUOTE_STAGE.get(state, state)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name"):
                code = "leaders.customer.request.%s" % vals.get("kind", "quote")
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code(code)
            vals.setdefault("history", json.dumps({self.stage_of(vals.get("state", "pending")): fields.Datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")}))
        return super().create(vals_list)

    def write(self, vals):
        if "state" in vals:
            for rec in self:
                history = json.loads(rec.history or "{}")
                stage = self.stage_of(vals["state"])
                if stage != "cancelled":
                    history.setdefault(stage, fields.Datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"))
                super(LeadersCustomerRequest, rec).write(dict(vals, history=json.dumps(history)))
            return True
        return super().write(vals)


class LeadersCustomerRequestLine(models.Model):
    _name = "leaders.customer.request.line"
    _description = "Quote Request Product"

    request_id = fields.Many2one("leaders.customer.request", required=True, ondelete="cascade", index=True)
    product_tmpl_id = fields.Many2one("product.template", required=True, ondelete="cascade")
    quantity = fields.Integer(default=1)
    options = fields.Text(help="JSON: the cart options of the line.")


class LeadersPromoCode(models.Model):
    _name = "leaders.promo.code"
    _description = "Storefront Promo Code"

    name = fields.Char("Code", required=True, index=True)
    percent_off = fields.Integer("Percent off", required=True)
    date_end = fields.Date("Valid until")
    active = fields.Boolean(default=True)

    _sql_constraints = [("code_uniq", "unique(name)", "This promo code already exists."),
                        ("percent_range", "check(percent_off between 1 and 100)", "Percent must be between 1 and 100.")]


class SaleOrderPublicRef(models.Model):
    """The customer-facing reference (SF-1042) of an order, assigned once."""
    _inherit = "sale.order"

    leaders_public_ref = fields.Char("Public reference", copy=False, readonly=True, index=True)
    leaders_sf_payment = fields.Selection([("cod", "Cash on delivery"), ("card", "Card"), ("paypal", "PayPal"),
                                           ("applePay", "Apple Pay")], string="Storefront payment", copy=False)
    leaders_sf_percent_off = fields.Integer("Promo percent", copy=False)
    leaders_sf_delivery_fee = fields.Float("Storefront delivery fee", copy=False)
    leaders_sf_lead_min = fields.Integer(copy=False)
    leaders_sf_lead_max = fields.Integer(copy=False)
    leaders_sf_city_key = fields.Char(copy=False)
    leaders_sf_governorate_key = fields.Char(copy=False)
    leaders_sf_notes = fields.Text(copy=False)
    leaders_sf_locale = fields.Char(copy=False, help="ar or en: the language of the storefront page the shopper came from.")
    leaders_sf_history = fields.Text(copy=False, help="JSON: stage -> ISO time it was reached.")

    def leaders_public_ref_get(self):
        self.ensure_one()
        if not self.leaders_public_ref:
            self.sudo().leaders_public_ref = self.env["ir.sequence"].sudo().next_by_code("leaders.sale.order.ref")
        return self.leaders_public_ref


class SaleOrderLineStorefront(models.Model):
    _inherit = "sale.order.line"

    leaders_sf_options = fields.Text(copy=False, help="JSON: the cart options the shopper chose.")


class LeadersContactRequest(models.Model):
    _name = "leaders.contact.request"
    _description = "Storefront Contact Request"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(readonly=True, copy=False, index=True)
    sender_name = fields.Char(required=True)
    email = fields.Char()
    phone = fields.Char()
    subject = fields.Char(required=True)
    message = fields.Text(required=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals["name"] = vals.get("name") or self.env["ir.sequence"].sudo().next_by_code("leaders.contact.request")
        return super().create(vals_list)
