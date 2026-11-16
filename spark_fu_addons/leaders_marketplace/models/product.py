import logging
import operator as py_operator

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)

class ProductTemplate(models.Model):
    # _name is explicit: with a list _inherit Odoo would otherwise build a
    # new model instead of extending product.template.
    _name = "product.template"
    _inherit = ["product.template", "leaders.backoffice.mixin"]

    marketplace_state = fields.Selection([
        ("draft","Draft"),("pending","Pending Review"),("published","Published"),
        ("rejected","Rejected"),("suspended","Suspended")
    ], default="draft", tracking=True, index=True)
    marketplace_reject_reason = fields.Text()
    marketplace_publish_date = fields.Datetime()
    marketplace_offer_ids = fields.One2many("leaders.marketplace.offer","product_tmpl_id")
    marketplace_owner_vendor_id = fields.Many2one(
        "res.partner", string="Marketplace Content Owner", readonly=True, index=True,
        help="Vendor who created this product through the vendor portal. Only that "
             "vendor may edit the product content, and only while no other vendor "
             "sells on the same template.",
    )

    marketplace_offer_count = fields.Integer(
        compute="_compute_marketplace_offer_count", string="Offers")
    marketplace_vendor_id = fields.Many2one(
        "res.partner", compute="_compute_marketplace_vendor_id", string="Marketplace Vendor",
        help="Who to talk to about this product: its portal owner, or the "
             "vendor of its first offer for a catalogue product.")

    @api.depends("marketplace_offer_ids")
    def _compute_marketplace_offer_count(self):
        for template in self:
            template.marketplace_offer_count = len(template.marketplace_offer_ids)

    @api.depends("marketplace_owner_vendor_id", "marketplace_offer_ids.vendor_id")
    def _compute_marketplace_vendor_id(self):
        for template in self:
            template.marketplace_vendor_id = (
                template.marketplace_owner_vendor_id
                or template.marketplace_offer_ids[:1].vendor_id)

    def _leaders_vendor_owns_content(self, vendor):
        """May ``vendor`` edit this template's own content (name, image, ...)?

        True only when the vendor created the template from the portal *and*
        every offer on it -- archived ones included -- is still theirs. As soon
        as a second seller lists on the template, or the marketplace attaches
        the offer to a catalogue product, the content becomes shared and the
        vendor is limited to their own offer.
        """
        self.ensure_one()
        vendor = vendor.commercial_partner_id
        if not vendor or self.marketplace_owner_vendor_id.commercial_partner_id != vendor:
            return False
        offers = self.with_context(active_test=False).marketplace_offer_ids
        return bool(offers) and all(
            offer.vendor_id.commercial_partner_id == vendor for offer in offers)

    def action_marketplace_submit(self):
        self.write({"marketplace_state":"pending"})

    def action_marketplace_approve(self):
        self._leaders_check_backoffice("reviewer")
        # ``is_published`` is write-protected by the website publisher group,
        # which a marketplace reviewer has no reason to hold; the guard above is
        # what authorises this, so the write itself goes through sudo.
        self.sudo().write({
            "marketplace_state":"published","is_published":True,
            "marketplace_publish_date":fields.Datetime.now(),"marketplace_reject_reason":False
        })
        for rec in self.filtered("marketplace_owner_vendor_id"):
            self.env["leaders.notification"].notify_vendor("vendor_product_approved", rec, rec.marketplace_owner_vendor_id)
        self._leaders_activate_owner_offers()

    def _leaders_activate_owner_offers(self):
        """Approving a vendor's own product also approves the offer it was submitted with (its price and stock):
        without an active offer the product is published but nothing sells it, so it stays off the website and
        the vendor keeps seeing "Pending Review". Offers of other sellers on the same product keep their own review.
        """
        Offer = self.env["leaders.marketplace.offer"].sudo()
        for rec in self.filtered("marketplace_owner_vendor_id"):
            offers = Offer.search([("product_tmpl_id", "=", rec.id), ("state", "=", "pending"),
                                   ("vendor_id", "=", rec.marketplace_owner_vendor_id.id)])
            if offers:
                offers.write({"state": "active", "reject_reason": False})

    def action_marketplace_reject(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self:
            if not rec.marketplace_reject_reason:
                raise UserError(_("Please add a rejection reason."))
        self.sudo().write({"marketplace_state":"rejected","is_published":False})
        for rec in self.filtered("marketplace_owner_vendor_id"):
            self.env["leaders.notification"].notify_vendor("vendor_product_rejected", rec, rec.marketplace_owner_vendor_id)

    def action_marketplace_suspend(self):
        """Pull a published product off the website without rejecting it."""
        self._leaders_check_backoffice("reviewer")
        self.sudo().write({"marketplace_state": "suspended", "is_published": False})

    def action_marketplace_reset(self):
        """Send a decided product back to the vendor as a draft."""
        self._leaders_check_backoffice("reviewer")
        self.sudo().write({"marketplace_state": "draft", "is_published": False})

class MarketplaceOffer(models.Model):
    _name = "leaders.marketplace.offer"
    _description = "Marketplace Offer"
    _inherit = ["mail.thread", "leaders.backoffice.mixin"]
    _order = "buybox_score desc, price asc, id"

    name = fields.Char(compute="_compute_name", store=True)
    product_tmpl_id = fields.Many2one("product.template", required=True, ondelete="cascade", index=True)
    product_id = fields.Many2one("product.product", required=True, ondelete="cascade", index=True)
    vendor_id = fields.Many2one("res.partner", required=True, domain=[("is_marketplace_vendor","=",True)], index=True)
    seller_sku = fields.Char(required=True, index=True)
    price = fields.Monetary(required=True, currency_field="currency_id", tracking=True)
    currency_id = fields.Many2one("res.currency", required=True, default=lambda self: self.env.company.currency_id)
    fulfillment_type = fields.Selection([("fbm","FBM"),("fbl","FBL")], required=True, default="fbm", tracking=True)
    stock_location_id = fields.Many2one("stock.location", required=True)
    market_ids = fields.Many2many("website", string="Markets")
    active = fields.Boolean(default=True)
    state = fields.Selection([
        ("draft","Draft"),("pending","Pending Review"),("active","Active"),
        ("rejected","Rejected"),("suspended","Suspended")
    ], default="draft", tracking=True)
    reject_reason = fields.Text()
    rating = fields.Float(default=0.0, help="Offer/vendor rating (0..5), used by Buy Box.")
    available_qty_cache = fields.Float(
        compute="_compute_available_qty_cache", search="_search_available_qty_cache")
    product_marketplace_state = fields.Selection(
        related="product_tmpl_id.marketplace_state", store=True, index=True,
        string="Product Status", readonly=True,
        help="Review status of the product itself. An active offer on an "
             "unpublished product never reaches the website.")
    buybox_score = fields.Float(compute="_compute_buybox_score", store=True)
    is_buybox_winner = fields.Boolean(
        string="Buy Box Winner", default=False, copy=False, index=True,
        help="Stored winner flag, recomputed on any offer change for the same product."
    )

    _sql_constraints = [
        ("seller_sku_vendor_uniq","unique(vendor_id, seller_sku)","Seller SKU must be unique per vendor.")
    ]

    _LEADERS_OFFER_UNIQ_INDEX = "leaders_offer_vendor_product_fulfillment_uniq"

    def init(self):
        """One live offer per vendor, variant and fulfilment type (partial unique index).

        Rejected and archived offers do not count. The migration refuses to upgrade
        a database that already holds duplicates, so they are fixed by hand; if
        some slip through anyway the index is skipped, not the whole registry.
        """
        super().init()
        cr = self.env.cr
        cr.execute("""
            SELECT vendor_id, product_id, fulfillment_type, array_agg(id ORDER BY id)
              FROM leaders_marketplace_offer
             WHERE active AND state != 'rejected'
          GROUP BY vendor_id, product_id, fulfillment_type
            HAVING count(*) > 1""")
        duplicates = cr.fetchall()
        if duplicates:
            _logger.error(
                "Duplicate live offers (vendor, product, fulfilment, offer ids): %s. "
                "The unique index %s was not created; archive or reject the extras.",
                duplicates, self._LEADERS_OFFER_UNIQ_INDEX)
            return
        cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS %s
                ON leaders_marketplace_offer (vendor_id, product_id, fulfillment_type)
             WHERE active AND state != 'rejected'""" % self._LEADERS_OFFER_UNIQ_INDEX)

    @api.depends("vendor_id","seller_sku","fulfillment_type")
    def _compute_name(self):
        for rec in self:
            rec.name = f"{rec.vendor_id.display_name or ''} / {rec.seller_sku or ''} / {(rec.fulfillment_type or '').upper()}"

    def _compute_available_qty_cache(self):
        for rec in self:
            rec.available_qty_cache = rec.available_qty() if rec.product_id and rec.stock_location_id else 0.0

    def _search_available_qty_cache(self, operator, value):
        """Make the back-office 'Out of Stock' filter work on a live quantity.

        The quantity is read from the vendor-owned quants at display time, so it
        cannot be a stored column; this walks the live offers instead, which is
        acceptable for a back-office filter.
        """
        compare = {
            "<": py_operator.lt, "<=": py_operator.le, ">": py_operator.gt,
            ">=": py_operator.ge, "=": py_operator.eq, "!=": py_operator.ne,
        }.get(operator)
        if compare is None:
            raise UserError(_("Unsupported search on the offer stock: %s", operator))
        offers = self.with_context(active_test=False).search([])
        # Read the live quantity, not the per-transaction cached compute.
        matching = offers.filtered(lambda o: compare(o.available_qty(), value))
        return [("id", "in", matching.ids)]

    @api.model
    def _tier_score_scale(self):
        """(min_sequence, max_sequence) over the vendor tiers that exist.

        The shipped tiers run Bronze 10 -> Platinum 40, so a HIGHER sequence is
        the better tier. Scores are normalized across whatever tiers the
        database actually holds instead of against a hard-coded 100.
        """
        sequences = self.env["leaders.vendor.tier"].sudo().search([]).mapped("sequence")
        if not sequences:
            return 0, 0
        return min(sequences), max(sequences)

    def _tier_score(self, scale=None):
        """Tier signal in 0..1: 0.0 without a tier, 1.0 when a single tier exists."""
        self.ensure_one()
        tier = self.vendor_id.vendor_tier_id
        if not tier:
            return 0.0
        min_seq, max_seq = scale if scale is not None else self._tier_score_scale()
        if max_seq == min_seq:
            return 1.0
        return (tier.sequence - min_seq) / (max_seq - min_seq)

    @api.depends("price","effective_price","fulfillment_type","rating","vendor_id.vendor_tier_id")
    def _compute_buybox_score(self):
        """Weighted, admin-configurable Buy Box score.

        Combines normalized signals (price, rating, availability, shipping speed,
        vendor tier) using the weights from leaders.buybox.config.

        The price signal is relative to the other live offers on the same
        product, so the siblings of every product in ``self`` are fetched in one
        search rather than one per record.
        """
        cfg = self.env["leaders.buybox.config"].get_active()
        tier_scale = self._tier_score_scale()
        # Without a platform warehouse FBL does not exist, so it earns no bonus.
        fbl_on = self.env["res.partner"]._leaders_fbl_enabled()
        product_ids = [pid for pid in self.mapped("product_id").ids if pid]
        max_price_by_product = {}
        if product_ids:
            siblings = self.sudo().search([
                ("product_id", "in", product_ids), ("state", "=", "active"), ("active", "=", True),
            ])
            for sibling in siblings:
                # The price actually charged (a running vendor discount included).
                if sibling.effective_price:
                    pid = sibling.product_id.id
                    max_price_by_product[pid] = max(max_price_by_product.get(pid, 0.0), sibling.effective_price)
        for rec in self:
            if not rec.product_id:
                rec.buybox_score = 0.0
                continue
            max_price = max_price_by_product.get(rec.product_id.id) or rec.effective_price or 1.0
            # price_score in 0..1 (cheapest -> 1).
            price_score = 1.0 - (rec.effective_price / max_price) if max_price else 0.0
            rating_score = (rec.rating or 0.0) / 5.0
            avail_score = 1.0 if rec.available_qty() > 0 else 0.0
            shipping_score = cfg.fbl_shipping_bonus if (fbl_on and rec.fulfillment_type == "fbl") else cfg.fbm_shipping_bonus
            tier_score = rec._tier_score(scale=tier_scale)
            rec.buybox_score = (
                cfg.price_weight * price_score
                + cfg.rating_weight * rating_score
                + cfg.availability_weight * avail_score
                + cfg.shipping_speed_weight * shipping_score
                + cfg.vendor_tier_weight * tier_score
            )

    def _recompute_buybox_winner(self, product_ids=None):
        """Recompute the stored winner flag for the given products (or self's).

        Out-of-stock offers are excluded when the config requires stock.
        Tie-break: highest rating, then lowest price, then id.
        """
        cfg = self.env["leaders.buybox.config"].get_active()
        if product_ids is None:
            product_ids = self.mapped("product_id").ids
        product_ids = [pid for pid in product_ids if pid]
        if not product_ids:
            return
        # A score is relative (price against the siblings, availability against
        # live stock), so the whole product's offers are re-scored before the
        # winner is picked -- otherwise a sibling's price drop or a sold-out
        # winner would never refresh the stored score.
        all_offers = self.sudo().search([
            ("product_id", "in", product_ids), ("state", "=", "active"), ("active", "=", True),
        ])
        if all_offers:
            all_offers._compute_buybox_score()
            all_offers.flush_recordset(["buybox_score"])
        offers_by_product = all_offers.grouped("product_id")
        for product_id in product_ids:
            offers = offers_by_product.get(self.env["product.product"].browse(product_id),
                                           self.browse())
            eligible = offers
            if cfg.require_stock:
                eligible = offers.filtered(lambda o: o.available_qty() > 0) or offers
            winner = eligible.sorted(
                key=lambda o: (-o.buybox_score, -(o.rating or 0.0), o.effective_price, o.id)
            )[:1]
            to_win = winner
            to_clear = offers - to_win
            to_win.filtered(lambda o: not o.is_buybox_winner).write({"is_buybox_winner": True})
            to_clear.filtered(lambda o: o.is_buybox_winner).write({"is_buybox_winner": False})
            # The website sells at the winning offer's price. On a multi-variant
            # template every variant's winner would fight over the same
            # list_price, so the sync is kept only where it is unambiguous; the
            # variant price comes from the offer itself (see
            # ``_price_compute`` and ``_get_additionnal_combination_info``).
            if winner and winner.price:
                template = winner.product_id.product_tmpl_id
                if len(template.product_variant_ids) == 1:
                    price = winner._leaders_price_in_company_currency()
                    if winner.product_id.list_price != price:
                        template.sudo().list_price = price

    fbl_enabled = fields.Boolean(compute="_compute_fbl_enabled")

    def _compute_fbl_enabled(self):
        enabled = self.env["res.partner"]._leaders_fbl_enabled()
        for rec in self:
            rec.fbl_enabled = enabled

    @api.model
    def _leaders_fbl_allowed(self):
        """True when FBL may be used (the platform has its own warehouse).

        Single entry point for every place that accepts a fulfilment type: the
        portal forms, the bulk import and the vendor API must call this (or
        ``_leaders_check_fulfillment_type``) instead of reading the setting.
        """
        return self.env["res.partner"]._leaders_fbl_enabled()

    @api.model
    def _leaders_check_fulfillment_type(self, fulfillment_type):
        """Raise a UserError when ``fulfillment_type`` is FBL but FBL is off."""
        if fulfillment_type == "fbl" and not self._leaders_fbl_allowed():
            raise UserError(self.env["res.partner"]._leaders_fbl_disabled_message())
        return fulfillment_type

    @api.constrains("fulfillment_type")
    def _check_fbl_allowed(self):
        if self._leaders_fbl_allowed():
            return
        if self.filtered(lambda o: o.fulfillment_type == "fbl"):
            raise ValidationError(self.env["res.partner"]._leaders_fbl_disabled_message())

    def _apply_fulfillment_policy(self):
        """Force the fulfilment type a policy dictates, if any applies.

        With no policy configured -- the default -- nothing is touched and the
        vendor keeps the fulfilment they chose.
        """
        Policy = self.env["leaders.fulfillment.policy"]
        for offer in self:
            policy = Policy._policy_for(
                offer.vendor_id, offer.product_tmpl_id.public_categ_ids)
            if not policy or offer.fulfillment_type == policy.forced_fulfillment_type:
                continue
            forced = policy.forced_fulfillment_type
            location = offer.vendor_id.commercial_partner_id.sudo()._marketplace_stock_location(forced)
            super(MarketplaceOffer, offer).write({
                "fulfillment_type": forced, "stock_location_id": location.id,
            })
        return True

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._apply_fulfillment_policy()
        records._recompute_buybox_winner()
        return records

    def write(self, vals):
        res = super().write(vals)
        if {"fulfillment_type", "vendor_id", "product_tmpl_id"}.intersection(vals):
            self._apply_fulfillment_policy()
        # Any change that can affect ranking triggers a winner recompute.
        trigger = {"price","rating","state","active","fulfillment_type","stock_location_id","vendor_id","product_id",
                   "discount_state"}
        if trigger.intersection(vals):
            self._recompute_buybox_winner()
        return res

    def action_submit(self):
        self.write({"state":"pending"})

    def action_approve(self):
        self._leaders_check_backoffice("reviewer")
        self.write({"state":"active","reject_reason":False})
        for rec in self:
            self.env["leaders.notification"].notify_vendor("vendor_offer_approved", rec, rec.vendor_id)

    def action_reject(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self:
            if not rec.reject_reason:
                raise UserError(_("Please add a rejection reason."))
        self.write({"state":"rejected"})
        for rec in self:
            self.env["leaders.notification"].notify_vendor("vendor_offer_rejected", rec, rec.vendor_id)

    def action_approve_with_product(self):
        """Approve the offer and publish its product in one step.

        Approving an offer alone leaves its product unpublished, so the offer
        is live but the product never reaches the website. This does both; the
        plain Approve is untouched for reviewers who only mean the offer.
        """
        self._leaders_check_backoffice("reviewer")
        self.action_approve()
        templates = self.product_tmpl_id.filtered(
            lambda t: t.marketplace_state != "published")
        if templates:
            templates.action_marketplace_approve()
        return True

    def _leaders_price_in_company_currency(self, company=None, date=None, effective=False):
        """This offer's price expressed in the company currency.

        By default the REGULAR price (what a cart line is priced from; the
        vendor discount is carried by the line's discount). ``effective=True``
        gives the price actually charged while a vendor discount is running
        (Buy Box, website price display).
        """
        self.ensure_one()
        return self._leaders_to_company_currency(
            self.effective_price if effective else self.price, company=company, date=date)

    def _leaders_to_company_currency(self, amount, company=None, date=None):
        self.ensure_one()
        company = company or self.env.company
        date = date or fields.Date.context_today(self)
        if self.currency_id == company.currency_id:
            return amount
        return self.currency_id._convert(amount, company.currency_id, company, date)

    def available_qty(self):
        """Free quantity of the vendor-owned stock backing this offer."""
        self.ensure_one()
        if not self.product_id or not self.stock_location_id:
            return 0.0
        qty = self.env["stock.quant"].sudo()._get_available_quantity(
            self.product_id, self.stock_location_id, owner_id=self.vendor_id.commercial_partner_id
        )
        return max(qty,0.0)

    @api.model
    def _marketplace_migrate_vendor_stock(self):
        """Move existing vendor stock out of company stock into vendor-owned locations.

        Only free (unreserved) quantity is moved, through inventory adjustment moves, and only
        for products sold exclusively by that vendor, so company-owned stock is never touched.
        """
        Quant = self.env["stock.quant"].sudo()
        Offer = self.sudo().with_context(active_test=False)
        vendors = self.env["res.partner"].sudo().search([
            ("is_marketplace_vendor", "=", True), ("vendor_state", "=", "approved")])
        old_locations = vendors.mapped("vendor_location_id")
        vendors._marketplace_ensure_vendor_stock()
        moved = []
        for offer in Offer.search([("vendor_id", "in", vendors.ids)]):
            vendor = offer.vendor_id.commercial_partner_id
            target = vendor._marketplace_stock_location(offer.fulfillment_type)
            source = offer.stock_location_id
            if source and source != target:
                sellers = Offer.search([("product_id", "=", offer.product_id.id)]).mapped("vendor_id.commercial_partner_id")
                if sellers == vendor and offer.product_id.is_storable:
                    quants = Quant.search([
                        ("product_id", "=", offer.product_id.id),
                        ("location_id", "=", source.id),
                        ("owner_id", "in", [False, vendor.id]),
                        ("lot_id", "=", False), ("package_id", "=", False),
                    ])
                    for quant in quants:
                        free = quant.quantity - quant.reserved_quantity
                        if free <= 0:
                            continue
                        Quant._leaders_adjust_vendor_stock(
                            quant.product_id, source, quant.owner_id, -free,
                            reason=_("Marketplace migration: move to vendor stock"))
                        Quant._leaders_adjust_vendor_stock(
                            quant.product_id, target, vendor, free,
                            reason=_("Marketplace migration: vendor-owned stock"))
                        moved.append((offer.product_id.display_name, free, source.complete_name, target.complete_name))
                offer.stock_location_id = target
        # Archive the legacy per-vendor locations that lived inside WH/Stock once empty.
        for location in old_locations - vendors.mapped("vendor_location_id") - vendors.mapped("vendor_fbl_location_id"):
            if not Quant.search_count([("location_id", "=", location.id), ("quantity", "!=", 0)]):
                location.active = False
        return moved

    @api.model
    def cron_refresh_buybox(self):
        """Safety net: re-pick the winner of every contested product.

        The stock hooks cover the normal paths; this catches whatever moved
        vendor stock without going through them (manual quant edits, imports,
        other modules). Only products with more than one live offer can change
        winner, so single-offer products are skipped.
        """
        groups = self.sudo()._read_group(
            [("state", "=", "active"), ("active", "=", True)],
            groupby=["product_id"], aggregates=["__count"])
        product_ids = [product.id for product, count in groups if count > 1]
        if product_ids:
            self.sudo()._recompute_buybox_winner(product_ids=product_ids)
        return True

    @api.model
    def _leaders_current_website(self):
        """The website being browsed, or an empty recordset outside a request.

        Outside a web request (crons, back office, tests) there is no market to
        filter on, and returning nothing keeps every offer eligible -- which is
        exactly how the marketplace behaved before markets were honoured.
        """
        if not request:
            return self.env["website"].browse()
        return getattr(request, "website", None) or self.env["website"].browse()

    def _leaders_filter_by_market(self, website=None):
        """Offers sellable on ``website``.

        An offer with no market is sold everywhere, which is the default, so a
        database that never filled market_ids sees no change at all.
        """
        website = website if website is not None else self._leaders_current_website()
        if not website:
            return self
        return self.filtered(lambda o: not o.market_ids or website in o.market_ids)

    @api.constrains("market_ids", "vendor_id")
    def _check_market_ids_allowed(self):
        """An offer cannot be listed on a market the vendor is not allowed on."""
        for offer in self:
            allowed = offer.vendor_id.commercial_partner_id.vendor_market_ids
            if not allowed or not offer.market_ids:
                continue
            extra = offer.market_ids - allowed
            if extra:
                raise ValidationError(_(
                    "%(vendor)s is not allowed to sell on %(markets)s.",
                    vendor=offer.vendor_id.display_name,
                    markets=", ".join(extra.mapped("name"))))

    @api.model
    def _get_marketplace_winner(self, product):
        """Offer that sells ``product`` on the website (Buy Box winner, else best active).

        A flagged winner that has since sold out is stepped over when the policy
        requires stock and another offer can actually ship: otherwise the website
        would report the product unavailable while a second vendor holds stock.
        """
        offers = self.sudo().search([
            ("product_id", "=", product.id), ("state", "=", "active"), ("active", "=", True),
        ])._leaders_filter_by_market()
        winner = offers.filtered("is_buybox_winner")[:1] or offers[:1]
        cfg = self.env["leaders.buybox.config"].sudo().get_active()
        if winner and cfg.require_stock and winner.available_qty() <= 0:
            in_stock = offers.filtered(lambda o: o.available_qty() > 0).sorted(
                key=lambda o: (-o.buybox_score, -(o.rating or 0.0), o.effective_price, o.id))
            if in_stock:
                return in_stock[:1]
        return winner


class ProductProduct(models.Model):
    _inherit = "product.product"

    def _price_compute(self, price_type, uom=None, currency=None, company=None, date=False):
        """Let a marketplace offer stand in for the variant's sales price.

        Callers that price a marketplace offer put ``leaders_offer_unit_price``
        (already in company currency) in the context. Only the ``list_price``
        base is substituted, so every pricelist rule -- percentage, surcharge,
        another pricelist -- still applies on top of it exactly as it would on
        the catalogue price.
        """
        offer_price = self.env.context.get("leaders_offer_unit_price")
        if price_type != "list_price" or offer_price is None or len(self) != 1:
            return super()._price_compute(
                price_type, uom=uom, currency=currency, company=company, date=date)
        company = company or self.env.company
        date = date or fields.Date.context_today(self)
        price = offer_price
        # Mirrors the core list_price branch, minus the attribute extra: an
        # offer price is already the final price of that exact variant.
        if uom:
            price = self.uom_id._compute_price(price, uom)
        if currency and currency != self.currency_id:
            price = self.currency_id._convert(price, currency, company, date)
        return {self.id: price}


class ProductTemplateWebsite(models.Model):
    _inherit = "product.template"

    def _leaders_seller_display(self):
        """Who sells this product on its page: the Buy Box winner and the other in-stock offers."""
        self.ensure_one()
        if not self.env["res.partner"]._marketplace_catalog_enabled():
            return {}
        Offer = self.env["leaders.marketplace.offer"].sudo()
        template = self.sudo()
        winner = Offer._get_marketplace_winner(template.product_variant_id)
        offers = Offer.search([
            ("product_tmpl_id", "=", template.id), ("state", "=", "active"), ("active", "=", True),
        ])._leaders_filter_by_market()
        others = (offers - winner).filtered(lambda o: o.available_qty() > 0).sorted(
            key=lambda o: (o.effective_price, o.id))
        if not winner and not others:
            return {}
        return {"winner": winner, "others": others, "multi_variant": len(template.product_variant_ids) > 1}

    def _get_additionnal_combination_info(self, product_or_template, quantity, date, website):
        """Show the winning offer's price on the product page and the shop grid.

        Without this a multi-variant marketplace product would be priced from
        the shared template list_price, which no longer follows any single
        vendor's offer. The price is the EFFECTIVE one (vendor discount
        included); the struck-through price and the "-X%" badge come from the
        offer's reference price (lowest price of the last 30 days), see
        ``_leaders_display_discount``.
        """
        offer = None
        effective_price = 0.0
        if product_or_template._name == "product.product":
            offer = self.env["leaders.marketplace.offer"]._get_marketplace_winner(
                product_or_template)
            if offer and offer.price:
                effective_price = offer._leaders_price_in_company_currency(
                    date=date, effective=True)
                product_or_template = product_or_template.with_context(
                    leaders_offer_unit_price=effective_price)
        info = super()._get_additionnal_combination_info(
            product_or_template, quantity, date, website)
        display = offer._leaders_display_discount() if offer and offer.price else {}
        if display and effective_price and info.get("price"):
            # ``price`` already carries pricelist, taxes and currency: scale the
            # reference price by the same factor so both figures stay comparable.
            ratio = info["price"] / effective_price
            reference = offer._leaders_to_company_currency(display["reference"], date=date)
            info["list_price"] = max(info.get("list_price") or 0.0, reference * ratio)
            info["has_discounted_price"] = True
            # A variant switch does not refresh a server-rendered badge, so it is
            # only offered where the product has a single variant.
            if len(offer.product_tmpl_id.product_variant_ids) == 1:
                info["leaders_discount_percent"] = display["percent"]
        return info

    def _get_sales_prices(self, website):
        """Shop grid: add the struck reference price and the "-X%" badge."""
        res = super()._get_sales_prices(website)
        Offer = self.env["leaders.marketplace.offer"]
        for template in self:
            vals = res.get(template.id)
            if not vals or len(template.product_variant_ids) != 1 or not vals.get("price_reduce"):
                continue
            offer = Offer._get_marketplace_winner(template.product_variant_id)
            display = offer._leaders_display_discount() if offer and offer.price else {}
            if not display:
                continue
            company_currency = self.env.company.currency_id
            regular = offer._leaders_price_in_company_currency()
            # The grid prices the synchronised template list_price, which is the
            # REGULAR price of the winner (cart and back-office lines are priced from
            # it, the discount travels as the line discount). Scale it to the
            # effective and reference prices; skip when it is not this offer's price.
            if company_currency.compare_amounts(template.list_price, regular) != 0 or not offer.price:
                continue
            regular_price = vals["price_reduce"]
            vals["price_reduce"] = regular_price * display["effective"] / offer.price
            vals["base_price"] = regular_price * display["reference"] / offer.price
            vals["leaders_discount_percent"] = display["percent"]
        return res


class MarketplaceOfferPublishLog(models.Model):
    _name = "leaders.marketplace.offer.publish.log"
    _description = "Marketplace Offer Publish Day"
    _order = "date desc, id desc"

    offer_id = fields.Many2one("leaders.marketplace.offer", required=True, ondelete="cascade", index=True)
    product_id = fields.Many2one(related="offer_id.product_id", store=True, index=True)
    vendor_id = fields.Many2one(related="offer_id.vendor_id", store=True, index=True)
    date = fields.Date(required=True, index=True, default=fields.Date.context_today)

    _sql_constraints = [
        ("offer_date_uniq", "unique(offer_id, date)", "The offer already has a publish log for this day."),
    ]

    @api.model
    def cron_track_published_days(self):
        """Log one row per offer that is active and in stock today.

        Runs once a day; a vendor's listing-days commission only accrues for
        offers that were actually published and available on that day, so an
        out-of-stock offer stops earning until it is restocked.
        """
        today = fields.Date.context_today(self)
        offers = self.env["leaders.marketplace.offer"].search([
            ("state", "=", "active"), ("active", "=", True),
        ])
        already_logged = self.search([
            ("date", "=", today), ("offer_id", "in", offers.ids),
        ]).offer_id
        to_log = offers - already_logged
        self.create([
            {"offer_id": offer.id, "date": today}
            for offer in to_log if offer.available_qty() > 0
        ])
