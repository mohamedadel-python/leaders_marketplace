from odoo import api, fields, models, _
from odoo.exceptions import UserError

class FulfillmentFee(models.Model):
    _name = "leaders.fulfillment.fee"
    _description = "Fulfillment Fee Rule"
    _order = "sequence, id"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    fee_type = fields.Selection([("fulfillment","Fulfillment"),("storage","Storage")], required=True)
    calc_method = fields.Selection([
        ("fixed","Fixed"),("per_kg","Per Kg"),("per_cbm_day","Per CBM / Day"),("percent","Percentage")
    ], required=True, default="fixed")
    amount = fields.Monetary(currency_field="currency_id")
    percent = fields.Float()
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)

    @api.model
    def _compute_fulfillment_fee(self, group):
        """Sum active 'fulfillment' fee rules for one fulfilment group."""
        total = 0.0
        rules = self.search([("active", "=", True), ("fee_type", "=", "fulfillment")])
        weight = sum((l.product_id.weight or 0.0) * l.product_uom_qty for l in group.line_ids)
        base_value = sum(l.price_subtotal for l in group.line_ids)
        for rule in rules:
            if rule.calc_method == "fixed":
                total += rule.amount
            elif rule.calc_method == "per_kg":
                total += rule.amount * weight
            elif rule.calc_method == "percent":
                total += base_value * rule.percent / 100.0
        return total

    @staticmethod
    def _storage_reference(period):
        """Reference that marks a vendor as charged for one month: STORAGE/YYYY-MM."""
        return "STORAGE/%s" % period.strftime("%Y-%m")

    @api.model
    def cron_apply_storage_fees(self):
        """Monthly storage fee per vendor FBL location = area/volume x duration.

        Storage is what the *platform warehouse* holds for the vendor, so the
        volume is measured in ``vendor_fbl_location_id``. A vendor's own FBM
        warehouse costs the marketplace nothing and is never charged.

        The month charged is stamped on the transaction reference, and a vendor
        already carrying that reference is skipped, so re-running the cron in
        the same month cannot charge twice.
        """
        if not self.env["res.partner"]._leaders_fbl_enabled():
            return  # no platform warehouse, nothing is stored for vendors
        Quant = self.env["stock.quant"]
        Tx = self.env["leaders.wallet.transaction"]
        storage_rules = self.search([("active", "=", True), ("fee_type", "=", "storage")])
        if not storage_rules:
            return
        period = fields.Date.context_today(self)
        reference = self._storage_reference(period)
        already = set(Tx.search([
            ("type", "=", "storage_fee"), ("reference", "=", reference),
        ]).mapped("vendor_id").ids)
        vendors = self.env["res.partner"].search([("is_marketplace_vendor", "=", True)])
        for vendor in vendors:
            if vendor.id in already:
                continue
            loc = vendor.vendor_fbl_location_id
            if not loc:
                continue
            quants = Quant.search([("location_id", "child_of", loc.id), ("quantity", ">", 0)])
            volume = sum((q.product_id.volume or 0.0) * q.quantity for q in quants)
            if not volume:
                continue
            fee = 0.0
            for rule in storage_rules:
                if rule.calc_method == "per_cbm_day":
                    # approx one month of storage
                    fee += rule.amount * volume * 30
                elif rule.calc_method == "fixed":
                    fee += rule.amount
            if fee:
                Tx.create({
                    "vendor_id": vendor.id,
                    "type": "storage_fee",
                    "amount": -fee,
                    "currency_id": self.env.company.currency_id.id,
                    "state": "available",
                    "available_date": period,
                    "reference": reference,
                })

class FulfillmentPolicy(models.Model):
    _name = "leaders.fulfillment.policy"
    _description = "Fulfillment Policy"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    category_id = fields.Many2one("product.public.category")
    vendor_id = fields.Many2one("res.partner")
    forced_fulfillment_type = fields.Selection([("fbm","FBM"),("fbl","FBL")], required=True)

    @api.constrains("forced_fulfillment_type", "active")
    def _check_fbl_allowed(self):
        if self.env["res.partner"]._leaders_fbl_enabled():
            return
        if self.filtered(lambda p: p.active and p.forced_fulfillment_type == "fbl"):
            raise UserError(self.env["res.partner"]._leaders_fbl_disabled_message())

    @api.model
    def _policy_for(self, vendor, categories):
        """Most specific active policy: vendor+category, then vendor, then category.

        Returns an empty recordset when no policy applies, which leaves the
        vendor's own choice of fulfilment untouched.
        """
        vendor = vendor.commercial_partner_id
        category_ids = categories.ids
        candidates = self.sudo().search([
            ("active", "=", True),
            "|", ("vendor_id", "=", vendor.id), ("vendor_id", "=", False),
            "|", ("category_id", "in", category_ids), ("category_id", "=", False),
        ])
        def rank(policy):
            # vendor+category (2) beats vendor (1) beats category (1) beats nothing.
            return (bool(policy.vendor_id) + bool(policy.category_id),
                    bool(policy.vendor_id), -policy.id)
        if not self.env["res.partner"]._leaders_fbl_enabled():
            # A stored FBL policy is ignored while the platform has no warehouse.
            candidates = candidates.filtered(lambda p: p.forced_fulfillment_type != "fbl")
        applicable = candidates.filtered(lambda p: p.vendor_id or p.category_id)
        return applicable.sorted(rank, reverse=True)[:1]

class FulfillmentInbound(models.Model):
    _name = "leaders.fulfillment.inbound"
    _description = "FBL Vendor Inbound"
    _inherit = ["mail.thread"]

    name = fields.Char(default="New", copy=False, readonly=True)
    vendor_id = fields.Many2one("res.partner", required=True, index=True)
    location_id = fields.Many2one(
        "stock.location", compute="_compute_location_id", store=True, readonly=True,
        help="Vendor-owned FBL location inside the Leaders warehouse (outside company stock).",
    )
    line_ids = fields.One2many("leaders.fulfillment.inbound.line","inbound_id")
    state = fields.Selection([
        ("draft","Draft"),("submitted","Submitted"),("received","Received"),("cancelled","Cancelled")
    ], default="draft", tracking=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env["res.partner"]._leaders_fbl_enabled():
            raise UserError(self.env["res.partner"]._leaders_fbl_disabled_message())
        for vals in vals_list:
            if vals.get("name","New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("leaders.fulfillment.inbound") or "New"
        return super().create(vals_list)

    @api.depends("vendor_id")
    def _compute_location_id(self):
        for inbound in self:
            vendor = inbound.vendor_id.commercial_partner_id
            inbound.location_id = vendor.vendor_fbl_location_id if vendor else False

    def action_receive(self):
        # Warehouse staff receive FBL inbounds, so everything this touches
        # outside their own ACLs -- vendor stock quants, the vendor's FBL
        # location, and the offers pointed at it -- runs as sudo.
        Quant = self.env["stock.quant"].sudo()
        Offer = self.env["leaders.marketplace.offer"].sudo()
        for inbound in self:
            if inbound.state == "received":
                continue
            vendor = inbound.vendor_id.commercial_partner_id
            location = vendor.sudo()._marketplace_stock_location("fbl")
            inbound.location_id = location
            if not inbound.line_ids:
                raise UserError(_("Add at least one product before receiving this inbound."))
            for line in inbound.line_ids:
                if not line.product_id.is_storable:
                    raise UserError(_("Only storable products can be received into FBL stock."))
                if line.quantity <= 0:
                    raise UserError(_("Inbound quantities must be greater than zero."))
                offers = Offer.search([
                    ("vendor_id", "=", vendor.id),
                    ("product_id", "=", line.product_id.id),
                    ("fulfillment_type", "=", "fbl"),
                ])
                if not offers:
                    raise UserError(_("This product does not have an FBL offer for the selected vendor."))
                Quant._leaders_adjust_vendor_stock(
                    line.product_id, location, vendor, line.quantity,
                    reason=_("FBL inbound %s", inbound.name))
                offers.filtered(lambda o: o.stock_location_id != location).sudo().write(
                    {"stock_location_id": location.id})
            inbound.state = "received"
        return True

    def action_cancel(self):
        self.filtered(lambda inbound: inbound.state != "received").write({"state": "cancelled"})

class FulfillmentInboundLine(models.Model):
    _name = "leaders.fulfillment.inbound.line"
    _description = "FBL Vendor Inbound Line"

    inbound_id = fields.Many2one("leaders.fulfillment.inbound", required=True, ondelete="cascade")
    vendor_id = fields.Many2one(related="inbound_id.vendor_id", store=True, readonly=True)
    product_id = fields.Many2one("product.product", required=True)
    quantity = fields.Float(required=True, default=1.0)
    uom_id = fields.Many2one("uom.uom", related="product_id.uom_id", store=True)

    @api.onchange("inbound_id")
    def _onchange_inbound_id(self):
        if not self.inbound_id.vendor_id:
            return {"domain": {"product_id": [("id", "=", False)]}}
        offers = self.env["leaders.marketplace.offer"].search([
            ("vendor_id", "=", self.inbound_id.vendor_id.id),
            ("fulfillment_type", "=", "fbl"),
            ("active", "=", True),
        ])
        return {"domain": {"product_id": [
            ("id", "in", offers.mapped("product_id").ids),
            ("is_storable", "=", True),
        ]}}
