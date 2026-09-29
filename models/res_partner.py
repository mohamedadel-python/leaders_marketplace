from odoo import api, fields, models, _

class ResPartner(models.Model):
    _inherit = "res.partner"

    is_marketplace_vendor = fields.Boolean(string="Marketplace Vendor", index=True)
    vendor_state = fields.Selection([
        ("draft","Draft"),("submitted","Submitted"),("under_review","Under Review"),
        ("approved","Approved"),("rejected","Rejected"),("frozen","Frozen"),("banned","Banned")
    ], default="draft", tracking=True, index=True)
    vendor_tier_id = fields.Many2one("leaders.vendor.tier", tracking=True)
    vendor_kyc_state = fields.Selection([
        ("not_started","Not Started"),("pending","Pending"),("verified","Verified"),("failed","Failed")
    ], default="not_started", tracking=True)
    vendor_store_name = fields.Char()
    vendor_store_slug = fields.Char(index=True)
    vendor_rating_avg = fields.Float(
        readonly=True, copy=False, digits=(3, 2),
        help="Average of the customer ratings of this vendor's completed orders.")
    vendor_rating_count = fields.Integer(readonly=True, copy=False)
    vendor_location_id = fields.Many2one("stock.location", string="Vendor Stock Location (FBM)")
    vendor_warehouse_id = fields.Many2one("stock.warehouse", string="Vendor Warehouse (FBM)", copy=False)
    vendor_fbl_location_id = fields.Many2one("stock.location", string="Vendor FBL Location", copy=False)
    vendor_invoice_seq = fields.Integer(copy=False, default=0)
    vendor_market_ids = fields.Many2many("website", string="Allowed Markets")
    vendor_hold_days = fields.Integer(default=14)
    vendor_approved_date = fields.Date(
        string="Vendor Approved On", readonly=True, copy=False, index=True,
        help="When this vendor was approved. Seniority for tier upgrades is "
             "counted from here.")
    vendor_min_payout = fields.Monetary(currency_field="company_currency_id", default=0.0)
    company_currency_id = fields.Many2one("res.currency", related="company_id.currency_id", readonly=True)
    vendor_wallet_balance = fields.Monetary(compute="_compute_vendor_wallet_balances", currency_field="company_currency_id")
    vendor_wallet_hold = fields.Monetary(compute="_compute_vendor_wallet_balances", currency_field="company_currency_id")

    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)
        partners.filtered(lambda p: p.vendor_state == "approved")._marketplace_ensure_store_slug()
        return partners

    def write(self, vals):
        res = super().write(vals)
        if vals.get("vendor_state") == "approved":
            self._marketplace_ensure_store_slug()
        return res

    def _marketplace_ensure_store_slug(self):
        """Give each vendor without one a unique, stable storefront slug."""
        Partner = self.sudo().with_context(active_test=False)
        for partner in self.filtered(lambda p: p.is_marketplace_vendor and not p.vendor_store_slug):
            base = self.env["ir.http"]._slugify(partner.vendor_store_name or partner.name or "") or "seller"
            slug, n = base, 2
            while Partner.search_count([("vendor_store_slug", "=", slug), ("id", "!=", partner.id)]):
                slug = "%s-%s" % (base, n)
                n += 1
            partner.sudo().vendor_store_slug = slug

    @api.model
    def _marketplace_catalog_enabled(self):
        """Catalog offers, seller display, storefronts and ratings (off by default)."""
        return self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.catalog_offers_enabled") in ("True", "true", "1")

    def _marketplace_refresh_vendor_ratings(self):
        """Store each vendor's rating average/count from their consumed order ratings."""
        Rating = self.env["rating.rating"].sudo()
        vendors = self.commercial_partner_id.filtered("is_marketplace_vendor") if self else \
            self.sudo().search([("is_marketplace_vendor", "=", True)])
        groups = Rating._read_group(
            [("res_model", "=", "leaders.vendor.order"), ("consumed", "=", True),
             ("rated_partner_id", "in", vendors.ids)],
            groupby=["rated_partner_id"], aggregates=["rating:avg", "__count"])
        stats = {partner.id: (avg, count) for partner, avg, count in groups}
        for vendor in vendors.sudo():
            avg, count = stats.get(vendor.id, (0.0, 0))
            if vendor.vendor_rating_count != count or vendor.vendor_rating_avg != avg:
                vendor.write({"vendor_rating_avg": avg, "vendor_rating_count": count})
        return vendors

    @api.model
    def cron_sync_vendor_ratings(self):
        """Feed customer ratings into the Buy Box, for vendors rated often enough.

        Below the minimum number of ratings the manual offer rating is kept.
        """
        if not self._marketplace_catalog_enabled():
            return True
        vendors = self.sudo().browse()._marketplace_refresh_vendor_ratings()
        try:
            minimum = int(self.env["ir.config_parameter"].sudo().get_param(
                "leaders_marketplace.vendor_rating_min_count", 5))
        except (TypeError, ValueError):
            minimum = 5
        for vendor in vendors.filtered(lambda v: v.vendor_rating_count >= minimum):
            offers = self.env["leaders.marketplace.offer"].sudo().with_context(active_test=False).search([
                ("vendor_id", "=", vendor.id), ("rating", "!=", vendor.vendor_rating_avg)])
            if offers:
                offers.write({"rating": vendor.vendor_rating_avg})
        return True

    @api.depends("is_marketplace_vendor")
    def _compute_vendor_wallet_balances(self):
        Tx = self.env["leaders.wallet.transaction"].sudo()
        for partner in self:
            if not partner.is_marketplace_vendor:
                partner.vendor_wallet_balance = 0
                partner.vendor_wallet_hold = 0
                continue
            txs = Tx.search([("vendor_id","=",partner.commercial_partner_id.id)])
            partner.vendor_wallet_balance = sum(txs.filtered(lambda t: t.state == "available").mapped("amount"))
            partner.vendor_wallet_hold = sum(txs.filtered(lambda t: t.state == "hold").mapped("amount"))

    # -------------------------------------------------------------------------
    # Vendor stock setup
    # -------------------------------------------------------------------------
    @api.model
    def _marketplace_main_warehouse(self):
        warehouse = self.env.ref("stock.warehouse0", raise_if_not_found=False)
        if not warehouse:
            warehouse = self.env["stock.warehouse"].sudo().search([
                ("company_id", "=", self.env.company.id), ("marketplace_vendor_id", "=", False),
            ], limit=1)
        return warehouse

    @api.model
    def _marketplace_fbl_root_location(self):
        """View location inside the main warehouse, outside its stock location.

        Company deliveries pick from WH/Stock, so vendor FBL stock kept here is
        never reserved for the company's own orders.
        """
        warehouse = self._marketplace_main_warehouse()
        Location = self.env["stock.location"].sudo().with_context(active_test=False)
        root = Location.search([
            ("location_id", "=", warehouse.view_location_id.id),
            ("name", "=", "Marketplace FBL"),
            ("usage", "=", "view"),
        ], limit=1)
        if root and not root.active:
            root.active = True
        if not root:
            root = Location.create({
                "name": "Marketplace FBL",
                "location_id": warehouse.view_location_id.id,
                "usage": "view",
                "company_id": warehouse.company_id.id,
            })
        return root

    @staticmethod
    def _marketplace_warehouse_code(partner_id):
        digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        n, code = partner_id, ""
        while n:
            n, r = divmod(n, 36)
            code = digits[r] + code
        return "V" + (code or "0")

    def _marketplace_ensure_vendor_stock(self):
        """Create the vendor's own warehouse (FBM) and FBL location if missing."""
        Warehouse = self.env["stock.warehouse"].sudo()
        Location = self.env["stock.location"].sudo()
        for partner in self.mapped("commercial_partner_id"):
            name = partner.vendor_store_name or partner.name
            if not partner.vendor_warehouse_id:
                warehouse = Warehouse.search([("marketplace_vendor_id", "=", partner.id)], limit=1)
                if not warehouse:
                    warehouse = Warehouse.create({
                        "name": _("Vendor - %s", name),
                        "code": self._marketplace_warehouse_code(partner.id),
                        "partner_id": partner.id,
                        "company_id": self.env.company.id,
                        "sequence": 1000,
                        "marketplace_vendor_id": partner.id,
                    })
                partner.vendor_warehouse_id = warehouse
            partner.vendor_location_id = partner.vendor_warehouse_id.lot_stock_id
            if not partner.vendor_fbl_location_id:
                partner.vendor_fbl_location_id = Location.create({
                    "name": name,
                    "location_id": self._marketplace_fbl_root_location().id,
                    "usage": "internal",
                    "company_id": self.env.company.id,
                })
        return True

    def _marketplace_stock_location(self, fulfillment_type):
        self.ensure_one()
        vendor = self.commercial_partner_id
        vendor._marketplace_ensure_vendor_stock()
        return vendor.vendor_fbl_location_id if fulfillment_type == "fbl" else vendor.vendor_location_id

    def _marketplace_next_invoice_number(self):
        """Per-vendor invoice numbering (row-locked to stay gap-free under concurrency)."""
        self.ensure_one()
        vendor = self.commercial_partner_id
        self.env.cr.execute(
            "SELECT vendor_invoice_seq FROM res_partner WHERE id = %s FOR UPDATE", [vendor.id])
        seq = (self.env.cr.fetchone()[0] or 0) + 1
        self.env.cr.execute("UPDATE res_partner SET vendor_invoice_seq = %s WHERE id = %s", [seq, vendor.id])
        vendor.invalidate_recordset(["vendor_invoice_seq"])
        return seq
