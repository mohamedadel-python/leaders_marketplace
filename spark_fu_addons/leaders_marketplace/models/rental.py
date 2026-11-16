"""Rentals of the project guide (feature 4): the platform rents the vendor's machine and re-rents it in its
own name. The customer sees the platform's price (vendor price + margin), the vendor never sees the renter,
every machine has an asset state and a movement log, and a rented machine leaves the website until returned."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

ASSET_STATES = [("available", "Available"), ("reserved", "Reserved"), ("rented", "With a customer"),
                ("inspection", "In inspection"), ("maintenance", "In maintenance"), ("out_of_service", "Out of service")]
HIDDEN_ASSET_STATES = ("rented", "out_of_service")  # not shown on the storefront
LONG_RENT_MONTHS = 24  # over two years: new equipment only


class ProductPublicCategoryRent(models.Model):
    _inherit = "product.public.category"

    leaders_rent_margin_percent = fields.Float(
        "Rent margin (%)", help="Margin added to the vendor's daily rent price for this subcategory. 0: the platform margin.")


class ProductTemplateRent(models.Model):
    _inherit = "product.template"

    leaders_rent_margin_percent = fields.Float(
        "Rent margin (%)", groups="leaders_marketplace.group_marketplace_manager",
        help="Margin of this machine only. 0: the subcategory's margin, else the platform margin.")
    leaders_rent_agreed_by = fields.Many2one("res.users", "Rent price agreed by", copy=False,
                                             groups="leaders_marketplace.group_marketplace_reviewer")
    leaders_rent_agreed_on = fields.Date("Rent price agreed on", copy=False,
                                         groups="leaders_marketplace.group_marketplace_reviewer")
    leaders_rent_valid_until = fields.Date("Vendor price valid until", copy=False,
                                           groups="leaders_marketplace.group_marketplace_reviewer",
                                           help="The vendor's daily price (Rent per day) agreed with the vendor, valid until this date.")
    leaders_asset_code = fields.Char("Asset number", copy=False, index=True)
    leaders_asset_state = fields.Selection(ASSET_STATES, string="Asset state", default="available", copy=False,
                                           index=True, tracking=True)
    leaders_rental_ids = fields.One2many("leaders.rental", "product_tmpl_id", string="Rentals")
    leaders_asset_move_ids = fields.One2many("leaders.asset.move", "product_tmpl_id", string="Asset log")

    def _leaders_rent_margin(self):
        """The margin percent of this machine: its own, else its subcategory's, else the platform's."""
        self.ensure_one()
        own = self.sudo().leaders_rent_margin_percent
        if own:
            return own
        categ = self.public_categ_ids[:1]
        node = categ
        while node:
            if node.leaders_rent_margin_percent:
                return node.leaders_rent_margin_percent
            node = node.parent_id
        try:
            return float(self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.rent_margin_percent") or 0)
        except ValueError:
            return 0.0

    def _leaders_rent_customer_price(self):
        """Daily rent price the customer pays: the vendor's price plus the margin (never below the minimum margin)."""
        self.ensure_one()
        vendor_price = self.leaders_rent_price_day or 0.0
        if not vendor_price:
            return 0.0
        margin = vendor_price * self._leaders_rent_margin() / 100.0
        try:
            minimum = float(self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.rent_margin_min") or 0)
        except ValueError:
            minimum = 0.0
        if self._leaders_rent_margin() or minimum:
            margin = max(margin, minimum)
        return round(vendor_price + margin, 2)

    def _leaders_set_asset_state(self, state, rental=False, note=False):
        for tmpl in self:
            tmpl.sudo().leaders_asset_state = state
            self.env["leaders.asset.move"].sudo().create({
                "product_tmpl_id": tmpl.id, "state": state, "rental_id": rental.id if rental else False,
                "partner_id": rental.partner_id.id if rental else False, "note": note or False})

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for tmpl in records.filtered(lambda t: t.leaders_rentable and not t.leaders_asset_code):
            tmpl.leaders_asset_code = tmpl.leaders_serial or "AST-%05d" % tmpl.id
        return records


class LeadersAssetMove(models.Model):
    _name = "leaders.asset.move"
    _description = "Rental Asset Movement"
    _order = "id desc"

    product_tmpl_id = fields.Many2one("product.template", required=True, ondelete="cascade", index=True, string="Machine")
    state = fields.Selection(ASSET_STATES, required=True)
    rental_id = fields.Many2one("leaders.rental", ondelete="set null")
    partner_id = fields.Many2one("res.partner", "With", ondelete="set null")
    note = fields.Char()
    date = fields.Datetime(default=fields.Datetime.now, required=True)


class LeadersRental(models.Model):
    _name = "leaders.rental"
    _description = "Equipment Rental"
    _inherit = ["mail.thread", "leaders.backoffice.mixin"]
    _order = "id desc"

    name = fields.Char("Reference", readonly=True, copy=False, default="New")
    product_tmpl_id = fields.Many2one("product.template", "Machine", required=True, index=True)
    vendor_id = fields.Many2one(related="product_tmpl_id.marketplace_owner_vendor_id", store=True, string="Vendor")
    partner_id = fields.Many2one("res.partner", "Renter", required=True, index=True)
    sale_order_id = fields.Many2one("sale.order", "Order", index=True, ondelete="set null")
    state_id = fields.Many2one("res.country.state", "Area (governorate)")
    date_start = fields.Date("From", required=True)
    date_end = fields.Date("To", required=True)
    state = fields.Selection([("reserved", "Reserved"), ("with_customer", "With the customer"), ("returned", "Returned"),
                              ("cancelled", "Cancelled")], default="reserved", required=True, tracking=True, index=True)
    supplier_price_day = fields.Monetary("Vendor price per day", currency_field="currency_id")
    customer_price_day = fields.Monetary("Customer price per day", currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)
    delivery_requested = fields.Boolean("Delivery requested")
    maintenance_included = fields.Boolean(
        "Maintenance included", help="Maintenance during the rental is part of the contract: a maintenance request "
                                     "of the renter is free. Otherwise it is a paid service.")
    contract_accepted_on = fields.Datetime("Contract accepted on", readonly=True)
    contract_version = fields.Char("Contract version", readonly=True)
    handover_meter = fields.Float("Hour meter at hand-over")
    return_meter = fields.Float("Hour meter at return")
    handover_photo_ids = fields.Many2many("ir.attachment", "leaders_rental_handover_rel", "rental_id", "attachment_id",
                                          string="Photos at hand-over")
    return_photo_ids = fields.Many2many("ir.attachment", "leaders_rental_return_rel", "rental_id", "attachment_id",
                                        string="Photos at return")
    payment_line_ids = fields.One2many("leaders.rental.payment", "rental_id", string="Agreed payments")

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec.name = "RNT/%05d" % rec.id
            rec.product_tmpl_id._leaders_set_asset_state("reserved", rec)
        return records

    @api.constrains("date_start", "date_end", "product_tmpl_id")
    def _check_rental(self):
        for rec in self:
            if rec.date_end < rec.date_start:
                raise ValidationError(_("The rental cannot end before it starts."))
            months = (rec.date_end - rec.date_start).days / 30.0
            if months > LONG_RENT_MONTHS and rec.product_tmpl_id.leaders_condition != "new":
                raise ValidationError(_("Rentals over two years are for new equipment only."))
            clash = self.search_count([("id", "!=", rec.id), ("product_tmpl_id", "=", rec.product_tmpl_id.id),
                                       ("state", "in", ("reserved", "with_customer")),
                                       ("date_start", "<=", rec.date_end), ("date_end", ">=", rec.date_start)])
            if clash and rec.state in ("reserved", "with_customer"):
                raise ValidationError(_("This machine is already rented over these dates."))

    def action_hand_over(self):
        self._leaders_check_backoffice("manager")
        for rec in self:
            if not rec.handover_photo_ids:
                raise UserError(_("Add the photos of the machine at hand-over first."))
            rec.state = "with_customer"
            rec.product_tmpl_id._leaders_set_asset_state("rented", rec, _("Handed over (meter %s)", rec.handover_meter))

    def action_return(self):
        self._leaders_check_backoffice("manager")
        for rec in self:
            if not rec.return_photo_ids:
                raise UserError(_("Add the photos of the machine at return first."))
            rec.state = "returned"
            rec.product_tmpl_id._leaders_set_asset_state("available", rec, _("Returned (meter %s)", rec.return_meter))

    def action_cancel(self):
        self._leaders_check_backoffice("manager")
        for rec in self.filtered(lambda r: r.state == "reserved"):
            rec.state = "cancelled"
            rec.product_tmpl_id._leaders_set_asset_state("available", rec, _("Reservation cancelled"))

    def _leaders_vendor_view(self):
        """What the vendor may see of a rental: the machine, the period, the area and the asset state only."""
        return [{"machine": r.product_tmpl_id.display_name, "from": r.date_start, "to": r.date_end,
                 "area": r.state_id.name or "", "asset_state": dict(ASSET_STATES).get(r.product_tmpl_id.leaders_asset_state)}
                for r in self]


class LeadersRentalPayment(models.Model):
    _name = "leaders.rental.payment"
    _description = "Agreed Rental Payment"
    _order = "due_date, id"

    rental_id = fields.Many2one("leaders.rental", required=True, ondelete="cascade", index=True)
    amount = fields.Monetary(required=True, currency_field="currency_id")
    currency_id = fields.Many2one(related="rental_id.currency_id")
    due_date = fields.Date(required=True)
    paid = fields.Boolean()
    agreed_by = fields.Many2one("res.users", "Agreed by", default=lambda self: self.env.user, required=True)
    note = fields.Char()
