from collections import defaultdict
from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare, float_is_zero

#: Group states that mean "the goods have left the vendor". A reported delivery
#: is still only a claim, so for the vendor order it counts exactly like
#: ``shipped``: fulfilled/partially_fulfilled, returnable, no longer cancellable.
_SHIPPED_GROUP_STATES = frozenset({"shipped", "delivery_reported", "delivered"})


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    marketplace_offer_id = fields.Many2one("leaders.marketplace.offer", index=True, copy=False)
    vendor_id = fields.Many2one("res.partner", related="marketplace_offer_id.vendor_id", store=True, index=True)
    fulfillment_type = fields.Selection(related="marketplace_offer_id.fulfillment_type", store=True)

    @api.depends("order_partner_id", "order_id", "product_id", "linked_line_id", "linked_line_ids")
    def _compute_name(self):
        lines_with_order = self.filtered("order_id")
        if lines_with_order:
            super(SaleOrderLine, lines_with_order)._compute_name()

    def _get_product_price_context(self):
        """Price a marketplace line from its own offer, not the catalogue price.

        Both ``_get_pricelist_price`` and ``_get_pricelist_price_before_discount``
        go through this context, so the pricelist rule is applied on top of the
        offer price rather than on the shared template list_price.
        """
        context = super()._get_product_price_context()
        offer = self.marketplace_offer_id
        if offer and offer.price:
            context["leaders_offer_unit_price"] = offer._leaders_price_in_company_currency(
                company=self.company_id or self.env.company,
                date=self._get_order_date() and fields.Date.to_date(self._get_order_date()),
            )
        return context

    def _marketplace_vendor_line_vals(self):
        self.ensure_one()
        return {
            "offer_id": self.marketplace_offer_id.id,
            "product_id": self.product_id.id,
            "name": self.name,
            "product_uom_qty": self.product_uom_qty,
            "product_uom": self.product_uom.id,
            "price_unit": self.price_unit,
            "discount": self.discount,
            "tax_ids": [(6, 0, self.tax_id.ids)],
        }


class SaleOrder(models.Model):
    _inherit = "sale.order"

    vendor_order_ids = fields.One2many("leaders.vendor.order", "sale_order_id")
    vendor_order_count = fields.Integer(compute="_compute_vendor_order_count")
    marketplace_split_done = fields.Boolean(
        copy=False, readonly=True,
        help="Vendor products of this checkout were moved to vendor orders.")
    leaders_payment_method = fields.Selection([
        ("online", "Online Payment"), ("cod", "Cash on Delivery"),
    ], default="online", copy=False)
    marketplace_vendor_only = fields.Boolean(
        copy=False, readonly=True, index=True,
        help="Every product of this checkout belongs to marketplace vendors, so the "
             "order was cancelled after the split and lives on as vendor orders.")

    def _compute_vendor_order_count(self):
        for order in self:
            order.vendor_order_count = len(order.vendor_order_ids)

    def _marketplace_checkout_total(self):
        """What the customer paid for at checkout: company lines + the vendor orders split out."""
        self.ensure_one()
        amount = 0.0 if self.state == "cancel" else self.amount_total
        return amount + sum(self.sudo().vendor_order_ids.mapped("amount_total"))

    # -------------------------------------------------------------------------
    # Website cart
    # -------------------------------------------------------------------------
    def _prepare_order_line_values(self, product_id, quantity, *args, **kwargs):
        values = super()._prepare_order_line_values(product_id, quantity, *args, **kwargs)
        product = self.env["product.product"].browse(values.get("product_id") or product_id)
        offer = self._leaders_explicit_offer(product, kwargs.get("offer_id")) \
            or self.env["leaders.marketplace.offer"]._get_marketplace_winner(product)
        if offer:
            values["marketplace_offer_id"] = offer.id
        return values

    def _leaders_explicit_offer(self, product, offer_id):
        """The offer the customer picked on the product page, if it is valid.

        Ignored unless catalog offers are enabled, and unless the offer is live,
        on this exact variant and sellable on this website.
        """
        try:
            offer_id = int(offer_id or 0)
        except (TypeError, ValueError):
            return self.env["leaders.marketplace.offer"]
        Offer = self.env["leaders.marketplace.offer"].sudo()
        if not offer_id or not self.env["res.partner"]._marketplace_catalog_enabled():
            return Offer
        offer = Offer.search([
            ("id", "=", offer_id), ("product_id", "=", product.id),
            ("state", "=", "active"), ("active", "=", True)])._leaders_filter_by_market()
        return offer

    def _cart_find_product_line(self, product_id, line_id=None, **kwargs):
        lines = super()._cart_find_product_line(product_id, line_id, **kwargs)
        if line_id is False or line_id is None:
            offer = self._leaders_explicit_offer(self.env["product.product"].browse(product_id), kwargs.get("offer_id"))
            if offer:
                return lines.filtered(lambda l: l.marketplace_offer_id == offer)
        return lines

    def _leaders_revalidate_marketplace_prices(self):
        """Re-point each vendor line at a live offer and refresh its price.

        A Buy Box winner can change between add-to-cart and payment. Without
        this the customer would pay the new winner's price while the line --
        and therefore the vendor order -- still belongs to the old offer, and
        the vendor would be paid a price that is not the one they listed.

        :return: True when anything changed, so the caller can send the
            customer back to the cart instead of confirming silently.
        """
        Offer = self.env["leaders.marketplace.offer"]
        changed = False
        for line in self.order_line.filtered("marketplace_offer_id"):
            offer = line.marketplace_offer_id
            if offer.state != "active" or not offer.active:
                winner = Offer._get_marketplace_winner(line.product_id)
                if not winner:
                    # Nothing sells this product any more; the standard cart
                    # checks deal with an unavailable product.
                    continue
                line.marketplace_offer_id = winner
                changed = True
            before = line.price_unit
            line.with_context(force_price_recomputation=True)._compute_price_unit()
            currency = line.currency_id or self.currency_id or self.env.company.currency_id
            if currency.compare_amounts(line.price_unit, before):
                changed = True
        return changed

    # -------------------------------------------------------------------------
    # Confirmation: vendor products never stay on the company's sales order
    # -------------------------------------------------------------------------
    def action_confirm(self):
        split_orders = self.env["sale.order"]
        for order in self.filtered(lambda o: o.state in ("draft", "sent")):
            if order._marketplace_split_vendor_lines():
                split_orders |= order
        # Checkouts that only contained vendor products have nothing left for the company.
        vendor_only = split_orders.filtered(lambda o: not o._marketplace_company_lines())
        for order in vendor_only:
            order.write({"state": "cancel", "marketplace_vendor_only": True})
            order.message_post(body=_(
                "All products of this checkout belong to marketplace vendors. "
                "They were moved to the vendor orders: %s", ", ".join(order.vendor_order_ids.mapped("name"))))
        to_confirm = self - vendor_only
        res = super(SaleOrder, to_confirm).action_confirm() if to_confirm else True
        # Vendor orders are confirmed once the checkout itself is accepted.
        split_orders.vendor_order_ids.filtered(lambda v: v.state == "pending").action_confirm()
        split_orders._marketplace_send_confirmation_mail()
        return res

    @api.model
    def _marketplace_vendor_only_presentation(self):
        """Customer-facing handling of vendor-only checkouts (off by default)."""
        return self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.vendor_only_presentation") in ("True", "true", "1")

    def _marketplace_send_confirmation_mail(self):
        """One confirmation email listing the vendor orders a checkout was split into."""
        if not self._marketplace_vendor_only_presentation():
            return
        template = self.env.ref(
            "leaders_marketplace.mail_template_marketplace_order_confirmation", raise_if_not_found=False)
        if not template:
            return
        for order in self.filtered(lambda o: o.vendor_order_ids and o.partner_id.email):
            template.sudo().send_mail(order.id, force_send=False)

    def _marketplace_company_lines(self):
        self.ensure_one()
        return self.order_line.filtered(
            lambda l: not l.display_type and not l.is_delivery and not l.is_downpayment and l.product_uom_qty)

    def _marketplace_vendor_lines(self):
        """Lines sold on behalf of a vendor (offer set, or product only sold via offers)."""
        self.ensure_one()
        Offer = self.env["leaders.marketplace.offer"]
        lines = self.env["sale.order.line"]
        for line in self.order_line.filtered(lambda l: not l.display_type and l.product_id and not l.is_delivery):
            if not line.marketplace_offer_id:
                offer = Offer._get_marketplace_winner(line.product_id)
                if offer:
                    line.marketplace_offer_id = offer
            if line.marketplace_offer_id:
                lines |= line
        return lines

    def _marketplace_split_vendor_lines(self):
        """Move vendor lines to one vendor order per vendor. Returns True if anything moved."""
        self.ensure_one()
        vendor_lines = self._marketplace_vendor_lines()
        if not vendor_lines:
            return False
        VendorOrder = self.env["leaders.vendor.order"].sudo()
        by_vendor = defaultdict(lambda: self.env["sale.order.line"])
        for line in vendor_lines:
            by_vendor[line.marketplace_offer_id.vendor_id.commercial_partner_id] |= line

        # Shipping follows the goods: if nothing of the company remains, the delivery
        # charge belongs to the vendor order (single vendor, or the first one).
        delivery_lines = self.order_line.filtered("is_delivery")
        company_remaining = self._marketplace_company_lines() - vendor_lines
        # A per-governorate carrier prices each vendor separately, so it can be
        # allocated even when company products stay behind on the checkout.
        zone_priced = bool(delivery_lines) and self.carrier_id.delivery_type == "marketplace_zone"
        fbl_income = self._leaders_fbl_delivery_income()
        currency = self.currency_id or self.env.company.currency_id
        # Non-zone carriers rate the basket as a whole; the shares are decided
        # once, up front, because a proportional split needs every vendor.
        non_zone_shares = {}
        if delivery_lines and not company_remaining and not zone_priced:
            non_zone_shares = self._leaders_non_zone_delivery_shares(by_vendor, delivery_lines)

        zone_moved = 0.0
        platform_delivery = 0.0
        plan = fbm_prices = None
        fbl_shares = {}
        if zone_priced:
            # FBM vendors are paid their own zone price; what is left of the
            # charge is the platform's part (company products, FBL goods).
            plan = self._leaders_zone_delivery_plan()
            state = self.partner_shipping_id.state_id
            fbm_prices = {}
            for zone_vendor, price in plan["vendors"]:
                if price is None:
                    raise UserError(_(
                        "%(vendor)s does not deliver to %(state)s.",
                        vendor=zone_vendor.display_name, state=state.name or _("this address")))
                fbm_prices[zone_vendor] = price
            platform_part = max(0.0, sum(delivery_lines.mapped("price_unit")) - sum(fbm_prices.values()))
            if plan["fbl_lines"] and not company_remaining and platform_part:
                if fbl_income == "platform":
                    # The marketplace warehouse ships it, so the marketplace keeps it.
                    platform_delivery = platform_part
                else:
                    fbl_shares = self._leaders_fbl_delivery_shares(plan["fbl_lines"], platform_part)
        for index, (vendor, lines) in enumerate(by_vendor.items()):
            line_vals = [(0, 0, line._marketplace_vendor_line_vals()) for line in lines]
            if zone_priced:
                if vendor in fbm_prices:
                    price = fbm_prices[vendor]
                    line_vals += [(0, 0, dict(
                        delivery_lines[:1]._marketplace_vendor_line_vals(),
                        price_unit=price, discount=0.0,
                        name=_("Delivery - %s", self.partner_shipping_id.state_id.name or ""),
                        is_delivery=True))]
                    zone_moved += price
                if vendor in fbl_shares:
                    line_vals += [(0, 0, dict(
                        delivery_lines[:1]._marketplace_vendor_line_vals(),
                        price_unit=fbl_shares[vendor], discount=0.0,
                        name=_("Delivery - %s", self.partner_shipping_id.state_id.name or ""),
                        is_delivery=True))]
            elif non_zone_shares:
                if vendor in non_zone_shares:
                    line_vals += non_zone_shares[vendor]
            elif delivery_lines and not company_remaining and index == 0:
                line_vals += [(0, 0, dict(dl._marketplace_vendor_line_vals(), is_delivery=True))
                              for dl in delivery_lines]
            VendorOrder.create({
                "sale_order_id": self.id,
                "vendor_id": vendor.id,
                "partner_id": self.partner_id.id,
                "partner_shipping_id": self.partner_shipping_id.id,
                "currency_id": self.currency_id.id,
                "date_order": self.date_order,
                "website_id": self.website_id.id if "website_id" in self._fields else False,
                "payment_method": self.leaders_payment_method,
                "line_ids": line_vals,
            })
        moved = vendor_lines
        if not company_remaining:
            moved |= delivery_lines
            if platform_delivery:
                # Vendor-only checkout: nothing carries this charge any more.
                self.env["leaders.marketplace.accounting"].post_platform_delivery_income(
                    self, platform_delivery)
        elif zone_priced and zone_moved:
            # Mixed cart: the customer pays the same total, but the part that
            # went to the vendors comes off the company's delivery line.
            self._leaders_reduce_company_delivery(delivery_lines, zone_moved)
        moved.sudo().unlink()
        self.marketplace_split_done = True
        return True

    def _leaders_fbl_delivery_shares(self, fbl_lines, amount):
        """Split the platform delivery ``amount`` across the FBL vendors of a
        checkout that has no company product ("vendor" income mode): by untaxed
        FBL amount, the rounding difference on the last vendor."""
        currency = self.currency_id or self.env.company.currency_id
        bases = {}
        for line in fbl_lines:
            vendor = line.marketplace_offer_id.vendor_id.commercial_partner_id
            bases[vendor] = bases.get(vendor, 0.0) + line.price_subtotal
        vendors = list(bases)
        grand = sum(bases.values())
        shares, allocated = {}, 0.0
        for vendor in vendors[:-1]:
            share = currency.round(amount * bases[vendor] / grand) if grand else 0.0
            shares[vendor] = share
            allocated += share
        shares[vendors[-1]] = currency.round(amount - allocated)
        return {v: a for v, a in shares.items() if a}

    @staticmethod
    def _leaders_lines_are_all_fbl(lines):
        return bool(lines) and all(
            line.marketplace_offer_id.fulfillment_type == "fbl" for line in lines)

    def _leaders_fbl_delivery_income(self):
        """"vendor" (default: the vendor is paid the delivery) or "platform"."""
        mode = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.fbl_delivery_income", "vendor")
        return mode if mode in ("vendor", "platform") else "vendor"

    def _leaders_split_delivery_proportional(self):
        param = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.split_delivery_proportional")
        return str(param).strip().lower() not in ("", "false", "0", "none")

    def _leaders_non_zone_delivery_shares(self, by_vendor, delivery_lines):
        """How a whole-basket delivery charge is allocated across vendors.

        Default: the whole charge to the first vendor, exactly as before, with a
        chatter note naming who received it. With
        ``leaders_marketplace.split_delivery_proportional`` it is split by
        untaxed amount, the rounding difference landing on the last vendor.

        :return: ``{vendor: [line command, ...]}``, empty to keep the caller on
            its original first-vendor path.
        """
        self.ensure_one()
        vendors = list(by_vendor)
        if not self._leaders_split_delivery_proportional():
            if len(vendors) > 1:
                self.message_post(body=_(
                    "The delivery charge of this checkout was assigned to "
                    "%(vendor)s; the other vendors of the order received none.",
                    vendor=vendors[0].display_name))
            return {}
        currency = self.currency_id or self.env.company.currency_id
        total = sum(delivery_lines.mapped("price_unit"))
        bases = {v: sum(by_vendor[v].mapped("price_subtotal")) for v in vendors}
        grand = sum(bases.values())
        if not total or not grand:
            return {}
        template = delivery_lines[:1]._marketplace_vendor_line_vals()
        shares, allocated = {}, 0.0
        for vendor in vendors[:-1]:
            amount = currency.round(total * bases[vendor] / grand)
            allocated += amount
            shares[vendor] = amount
        shares[vendors[-1]] = currency.round(total - allocated)
        self.message_post(body=_(
            "The delivery charge of %(total)s was split across the vendors of this "
            "checkout in proportion to their amount.", total=total))
        return {
            vendor: [(0, 0, dict(template, price_unit=amount, discount=0.0, is_delivery=True))]
            for vendor, amount in shares.items()
        }

    def _leaders_reduce_company_delivery(self, delivery_lines, amount):
        """Take ``amount`` off the checkout's delivery lines; drop empty ones."""
        self.ensure_one()
        currency = self.currency_id or self.env.company.currency_id
        total = sum(delivery_lines.mapped("price_unit"))
        if currency.compare_amounts(amount, total) > 0:
            raise UserError(_(
                "The vendor delivery prices for this address add up to %(moved)s, "
                "more than the %(charged)s charged at checkout. Please reload the "
                "cart so the delivery is priced again.",
                moved=amount, charged=total))
        left = amount
        for line in delivery_lines:
            if currency.is_zero(left):
                break
            take = min(line.price_unit, left)
            line.price_unit -= take
            left -= take
        delivery_lines.filtered(
            lambda l: currency.is_zero(l.price_unit)).sudo().unlink()


class VendorOrder(models.Model):
    _name = "leaders.vendor.order"
    _description = "Vendor Order"
    _inherit = ["rating.mixin", "mail.thread", "mail.activity.mixin", "portal.mixin", "leaders.backoffice.mixin"]
    _order = "id desc"

    name = fields.Char(default="New", copy=False, readonly=True, index=True)
    sale_order_id = fields.Many2one(
        "sale.order", string="Checkout", ondelete="set null", index=True, readonly=True,
        help="Website checkout this vendor order was split from. Not a company sale.")
    vendor_id = fields.Many2one("res.partner", required=True, index=True, tracking=True)
    partner_id = fields.Many2one("res.partner", string="Customer", index=True, tracking=True)
    partner_shipping_id = fields.Many2one("res.partner", string="Delivery Address")
    date_order = fields.Datetime(default=fields.Datetime.now)
    website_id = fields.Many2one("website")
    line_ids = fields.One2many("leaders.vendor.order.line", "order_id", copy=True)
    fulfillment_group_ids = fields.One2many("leaders.fulfillment.group", "vendor_order_id")
    picking_ids = fields.One2many("stock.picking", "leaders_vendor_order_id", string="Deliveries")
    picking_count = fields.Integer(compute="_compute_picking_count")
    invoice_id = fields.Many2one("leaders.vendor.invoice", string="Vendor Invoice", copy=False, readonly=True)
    cod_payment_id = fields.Many2one(
        "account.payment", string="COD Cash Receipt", copy=False, readonly=True,
        help="Payment recording the cash collected on delivery for a "
             "platform-delivered (FBL) order.")
    refund_payment_id = fields.Many2one(
        "account.payment", string="Cancellation Refund", copy=False, readonly=True,
        help="Draft refund prepared when a paid order was cancelled. Finance "
             "posts it once the money has actually been sent back.")
    currency_id = fields.Many2one("res.currency", required=True, default=lambda self: self.env.company.currency_id)
    state = fields.Selection([
        ("pending","Pending"),("confirmed","Confirmed"),("processing","Processing"),
        ("partially_fulfilled","Partially Fulfilled"),("fulfilled","Fulfilled"),
        ("completed","Completed"),("cancelled","Cancelled")
    ], default="pending", tracking=True, index=True)
    cancel_reason = fields.Text(readonly=True, copy=False)
    cancelled_by = fields.Many2one("res.users", readonly=True, copy=False)
    amount_untaxed = fields.Monetary(currency_field="currency_id")
    amount_tax = fields.Monetary(currency_field="currency_id")
    amount_total = fields.Monetary(currency_field="currency_id")
    commission_amount = fields.Monetary(currency_field="currency_id")
    fulfillment_fee_amount = fields.Monetary(currency_field="currency_id")
    refunded_amount = fields.Monetary(currency_field="currency_id", readonly=True, copy=False,
                                      help="Amount refunded to the customer through returns.")
    vendor_net = fields.Monetary(currency_field="currency_id")
    date_shipped = fields.Datetime(readonly=True, copy=False, help="First shipment date; starts the return window.")
    return_ids = fields.One2many("leaders.vendor.return", "vendor_order_id", string="Returns")
    return_count = fields.Integer(compute="_compute_return_count")
    credit_note_ids = fields.One2many("leaders.vendor.invoice", "vendor_order_id", string="Vendor Documents")
    collection_move_id = fields.Many2one("account.move", string="Escrow Collection Entry", copy=False, readonly=True)
    collection_reversal_move_id = fields.Many2one("account.move", string="Escrow Reversal Entry", copy=False, readonly=True)
    earning_move_id = fields.Many2one("account.move", string="Earning Journal Entry", copy=False)
    payment_method = fields.Selection([
        ("online", "Online Payment"), ("cod", "Cash on Delivery"),
    ], default="online", copy=False, index=True)
    cod_due_move_id = fields.Many2one("account.move", string="COD Commission Due Entry", copy=False, readonly=True)
    cod_cash_confirmed = fields.Boolean(
        string="Cash Collected (Vendor Confirmed)", copy=False, tracking=True,
        help="For FBM + Cash on Delivery: the vendor has confirmed they collected "
             "the cash from the customer. Their commission debt is only recorded once "
             "this is confirmed, not automatically at delivery.")
    cod_cash_confirmed_date = fields.Datetime(copy=False, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name","New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("leaders.vendor.order") or "New"
        records = super().create(vals_list)
        records._recompute_amounts()
        if self.env["leaders.notification"]._enabled("vendor_order_confirmed"):
            # Chatter messages on the order reach the vendor owner.
            for rec in records:
                rec.message_subscribe(partner_ids=self.env["leaders.notification"]._vendor_owner_partners(rec.vendor_id).ids)
        return records

    def _compute_picking_count(self):
        for rec in self:
            rec.picking_count = len(rec.picking_ids)

    def _compute_return_count(self):
        for rec in self:
            rec.return_count = len(rec.return_ids)

    def _net_qty_sold(self):
        """Quantity still kept by the customer, across all non-delivery lines."""
        self.ensure_one()
        return sum(
            max(line.product_uom_qty - line.qty_returned, 0.0)
            for line in self.line_ids if not line.is_delivery
        )

    def _compute_access_url(self):
        super()._compute_access_url()
        for rec in self:
            rec.access_url = f"/my/marketplace/vendor-orders/{rec.id}"

    def _recompute_amounts(self):
        for rec in self:
            rec.amount_untaxed = sum(rec.line_ids.mapped("price_subtotal"))
            rec.amount_tax = sum(rec.line_ids.mapped("price_tax"))
            rec.amount_total = sum(rec.line_ids.mapped("price_total"))
            rec.commission_amount = sum(self.env["leaders.commission.line"].search([
                ("vendor_order_id","=",rec.id),("state","!=","cancelled")
            ]).mapped("commission_amount"))
            rec.fulfillment_fee_amount = sum(rec.fulfillment_group_ids.mapped("fee_amount"))
            # Commission lines already include the negative lines given back on returns.
            rec.vendor_net = (rec.amount_total - rec.refunded_amount
                              - rec.commission_amount - rec.fulfillment_fee_amount)

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------
    def action_confirm(self):
        Accounting = self.env["leaders.marketplace.accounting"]
        for rec in self:
            if rec.state != "pending":
                continue
            if not rec.line_ids:
                raise UserError(_("Vendor order %s has no lines.", rec.name))
            rec.state = "confirmed"
            rec._create_fulfillment_groups()
            rec._apply_fulfillment_fees()
            rec._recompute_amounts()
            rec.fulfillment_group_ids._create_pickings()
            self.env["leaders.vendor.invoice"]._create_for_vendor_order(rec)
            self.env["leaders.notification"].notify_vendor("vendor_order_confirmed", rec, rec.vendor_id)
            if rec.payment_method == "cod":
                # No escrow leg for COD: FBM never had the money; FBL/warehouse
                # collection is booked once delivery confirms the cash exists.
                rec.message_post(body=_("Cash on Delivery: collection deferred until delivery."))
                continue
            # The customer owes the vendor's amount to the platform (held in escrow).
            Accounting.post_collection_entry(rec)
            Accounting.reconcile_collection(rec)
        return True

    def _create_fulfillment_groups(self):
        Fulfillment = self.env["leaders.fulfillment.group"].sudo()
        for rec in self:
            by_source = defaultdict(lambda: self.env["leaders.vendor.order.line"])
            for line in rec.line_ids.filtered(lambda l: not l.is_delivery and l.product_id.is_storable):
                location = line.offer_id.stock_location_id or rec.vendor_id._marketplace_stock_location(line.fulfillment_type)
                by_source[(line.fulfillment_type, location)] |= line
            for (fulfillment_type, location), lines in by_source.items():
                group = rec.fulfillment_group_ids.filtered(
                    lambda g: g.fulfillment_type == fulfillment_type and g.source_location_id == location)[:1]
                if not group:
                    group = Fulfillment.create({
                        "vendor_order_id": rec.id,
                        "fulfillment_type": fulfillment_type,
                        "source_location_id": location.id,
                    })
                lines.write({"fulfillment_group_id": group.id})

    def _apply_fulfillment_fees(self):
        """Apply configured fulfilment fees to FBL groups of this vendor order."""
        Fee = self.env["leaders.fulfillment.fee"]
        for rec in self:
            for group in rec.fulfillment_group_ids:
                if group.fulfillment_type != "fbl":
                    group.fee_amount = 0.0
                    continue
                group.fee_amount = Fee._compute_fulfillment_fee(group)

    def _sync_state_from_groups(self):
        for rec in self:
            if rec.state in ("pending", "cancelled", "completed"):
                continue
            groups = rec.fulfillment_group_ids.filtered(lambda g: g.state != "cancelled")
            if not groups:
                continue
            states = set(groups.mapped("state"))
            if states == {"delivered"}:
                # Platform-only action reached from stock hooks / the vendor portal.
                rec.sudo().action_mark_completed()
            elif states <= _SHIPPED_GROUP_STATES:
                rec.state = "fulfilled"
            elif states & _SHIPPED_GROUP_STATES:
                rec.state = "partially_fulfilled"
            elif states & {"ready", "picking", "packing"}:
                rec.state = "processing"
            if rec.state in ("fulfilled", "partially_fulfilled", "completed") and not rec.date_shipped:
                dates = rec.picking_ids.filtered(lambda p: p.state == "done").mapped("date_done")
                rec.date_shipped = min(dates) if dates else fields.Datetime.now()
            if rec.state in ("fulfilled", "partially_fulfilled"):
                tier = rec.vendor_id.vendor_tier_id
                if (tier.escrow_release_policy == "on_ship" and rec.state == "fulfilled"
                        and rec.payment_method != "cod"):
                    # COD only resolves at full delivery -- there is no escrow to release early.
                    self.env["leaders.wallet.transaction"].create_vendor_earning(rec)

    def action_mark_completed(self):
        self._leaders_check_backoffice("manager")
        Wallet = self.env["leaders.wallet.transaction"]
        Accounting = self.env["leaders.marketplace.accounting"]
        for rec in self:
            rec.state = "completed"
            fulfillment_types = set(rec.fulfillment_group_ids.mapped("fulfillment_type"))
            if rec.payment_method == "cod" and fulfillment_types == {"fbm"}:
                # The vendor collected the cash directly, outside our systems --
                # delivery alone doesn't prove they actually got paid (the
                # customer could refuse at the door). Wait for the vendor to
                # explicitly confirm collection (action_confirm_cod_payment)
                # before recording the debt they owe us for it.
                continue
            if rec.payment_method == "cod":
                # COD + FBL (or mixed): the warehouse/driver held the cash on our
                # behalf -- book it as collected now, then earn like any online order.
                Accounting.post_collection_entry(rec)
                # ...and record the cash actually received, so the receivable the
                # line above opened does not stay against the customer forever.
                Accounting.post_cod_cash_receipt(rec)
            Wallet.create_vendor_earning(rec)

    def action_confirm_cod_payment(self):
        """The vendor declares they collected the cash for an FBM + COD order.

        Only now is the commission/fee debt they owe the marketplace recorded
        (delivery alone doesn't prove the customer actually paid at the door)."""
        Wallet = self.env["leaders.wallet.transaction"]
        for rec in self:
            if rec.payment_method != "cod":
                raise UserError(_("This is not a Cash on Delivery order."))
            if set(rec.fulfillment_group_ids.mapped("fulfillment_type")) != {"fbm"}:
                raise UserError(_("Only vendor-fulfilled (FBM) orders need this confirmation."))
            if rec.state != "completed":
                raise UserError(_("The order must be fully delivered before confirming cash collection."))
            if rec.cod_cash_confirmed:
                continue
            rec.cod_cash_confirmed = True
            rec.cod_cash_confirmed_date = fields.Datetime.now()
            Wallet.create_vendor_cod_due(rec)

    def action_cancel(self):
        self._leaders_check_backoffice("manager")
        Accounting = self.env["leaders.marketplace.accounting"]
        for rec in self:
            if rec.state == "cancelled":
                continue
            if (not self.env.context.get("leaders_cod_refusal")
                    and rec.fulfillment_group_ids.filtered(lambda g: g.state in _SHIPPED_GROUP_STATES)):
                raise UserError(_("Vendor order %s is already shipped; use a return instead.", rec.name))
            if not rec.cancelled_by_type:
                # Nobody said who cancelled: the back-office did (a vendor or a
                # customer cancellation stores its own type before getting here).
                rec.cancelled_by_type = "platform"
            if not rec.cancel_date:
                rec.cancel_date = fields.Datetime.now()
            rec.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")).action_cancel()
            rec.fulfillment_group_ids.write({"state": "cancelled"})
            self.env["leaders.commission.line"].search([("vendor_order_id", "=", rec.id)]).write({"state": "cancelled"})
            if rec.invoice_id:
                rec.invoice_id.state = "cancelled"
            Accounting.reverse_collection_entry(rec)
            if rec.payment_method != "cod":
                # An online order the customer already paid leaves their money
                # sitting as an unapplied credit; prepare the way back.
                Accounting.create_cancellation_refund_payment(rec)
            rec.state = "cancelled"
            rec._recompute_amounts()
            if not (self.env.context.get("leaders_customer_cancel")
                    or self.env.context.get("leaders_cod_refusal")):
                self.env["leaders.notification"].notify_customer("customer_order_cancelled", rec, rec.partner_id)
        return True

    def _marketplace_submit_rating(self, rating, feedback=None):
        """The customer rates a completed order (1-5 plus an optional comment)."""
        self.ensure_one()
        if self.state != "completed":
            raise UserError(_("You can rate an order once it is completed."))
        Rating = self.env["rating.rating"].sudo()
        if Rating.search_count([
                ("res_model", "=", self._name), ("res_id", "=", self.id), ("consumed", "=", True)]):
            raise UserError(_("You already rated this order."))
        try:
            value = int(rating)
        except (TypeError, ValueError):
            value = 0
        if not 1 <= value <= 5:
            raise UserError(_("Please choose a rating between 1 and 5."))
        record = Rating.create({
            "res_model_id": self.env["ir.model"]._get(self._name).id,
            "res_id": self.id,
            "rated_partner_id": self.vendor_id.id,
            "partner_id": self.partner_id.id,
            "rating": value,
            "feedback": (feedback or "").strip() or False,
            "consumed": True,
        })
        self.vendor_id.sudo()._marketplace_refresh_vendor_ratings()
        return record

    def action_vendor_cancel(self, reason, user=None):
        """The vendor turns down an order they cannot fulfil.

        Off unless the marketplace enabled it; the cancellation itself is the
        back-office ``action_cancel`` (escrow reversal, refund draft), run as sudo.
        """
        self.ensure_one()
        enabled = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.vendor_can_cancel_orders")
        if enabled not in ("True", "true", "1"):
            raise UserError(_("Vendors cannot cancel orders on this marketplace."))
        reason = (reason or "").strip()
        if not reason:
            raise UserError(_("Please give a reason for cancelling this order."))
        if self.state == "cancelled":
            raise UserError(_("Vendor order %s is already cancelled.", self.name))
        if self.state in ("fulfilled", "completed") or self.fulfillment_group_ids.filtered(
                lambda g: g.state in _SHIPPED_GROUP_STATES):
            raise UserError(_("Vendor order %s is already shipped and can no longer be cancelled.", self.name))
        # The reason is stored first so the customer's email can quote it.
        self.sudo().write({"cancel_reason": reason, "cancelled_by": (user or self.env.user).id,
                           "cancelled_by_type": "vendor", "cancel_date": fields.Datetime.now()})
        self.sudo().action_cancel()
        self.sudo().message_post(body=_(
            "Order cancelled by the vendor. Reason: %(reason)s", reason=reason))
        return True

    def _update_invoice_payment_state(self):
        for rec in self.filtered("invoice_id"):
            if rec.payment_method == "cod":
                # No receivable/transaction to check: the vendor (FBM) or the
                # warehouse (FBL) already holds the cash once delivery completes.
                paid = rec.state == "completed"
            elif rec.collection_move_id:
                receivable = rec.collection_move_id.line_ids.filtered(
                    lambda l: l.account_id.account_type == "asset_receivable")
                paid = bool(receivable) and all(receivable.mapped("reconciled"))
            else:
                paid = any(t.state == "done" for t in rec.sale_order_id.transaction_ids)
            rec.invoice_id.payment_state = "paid" if paid else "not_paid"

    def action_view_returns(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Returns"),
            "res_model": "leaders.vendor.return",
            "view_mode": "list,form",
            "domain": [("vendor_order_id", "=", self.id)],
            "context": {"create": False},
        }

    def action_view_pickings(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("stock.action_picking_tree_all")
        action["domain"] = [("id", "in", self.picking_ids.ids)]
        action["context"] = {"create": False}
        return action


class VendorOrderLine(models.Model):
    _name = "leaders.vendor.order.line"
    _description = "Vendor Order Line"
    _order = "order_id, sequence, id"

    order_id = fields.Many2one("leaders.vendor.order", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    vendor_id = fields.Many2one(related="order_id.vendor_id", store=True, index=True)
    currency_id = fields.Many2one(related="order_id.currency_id", store=True)
    offer_id = fields.Many2one("leaders.marketplace.offer", index=True)
    fulfillment_type = fields.Selection(related="offer_id.fulfillment_type", store=True)
    fulfillment_group_id = fields.Many2one("leaders.fulfillment.group", index=True)
    product_id = fields.Many2one("product.product", required=True)
    name = fields.Text(required=True)
    product_uom_qty = fields.Float("Quantity", digits="Product Unit of Measure", default=1.0)
    product_uom = fields.Many2one("uom.uom", string="Unit")
    price_unit = fields.Float(digits="Product Price")
    discount = fields.Float(digits="Discount")
    tax_ids = fields.Many2many("account.tax", string="Taxes")
    price_subtotal = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    price_tax = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    price_total = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    qty_delivered = fields.Float(compute="_compute_qty_delivered", digits="Product Unit of Measure")
    is_delivery = fields.Boolean(default=False)
    commission_amount = fields.Monetary(currency_field="currency_id")
    move_ids = fields.One2many("stock.move", "leaders_vendor_line_id")

    @api.depends("product_uom_qty", "price_unit", "discount", "tax_ids", "currency_id")
    def _compute_amounts(self):
        for line in self:
            price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
            taxes = line.tax_ids.compute_all(
                price, line.currency_id, line.product_uom_qty,
                product=line.product_id, partner=line.order_id.partner_id)
            line.price_subtotal = taxes["total_excluded"]
            line.price_total = taxes["total_included"]
            line.price_tax = taxes["total_included"] - taxes["total_excluded"]

    return_line_ids = fields.One2many("leaders.vendor.return.line", "vendor_line_id")
    qty_returned = fields.Float(compute="_compute_qty_returned", digits="Product Unit of Measure")

    @api.depends("return_line_ids.quantity", "return_line_ids.return_id.state")
    def _compute_qty_returned(self):
        for line in self:
            line.qty_returned = sum(line.return_line_ids.filtered(
                lambda r: r.return_id.state in ("received", "refunded")).mapped("quantity"))

    def _qty_returnable(self, exclude=None):
        """Delivered quantity not yet covered by an open or completed return."""
        self.ensure_one()
        engaged = self.return_line_ids.filtered(
            lambda r: r.return_id != exclude and r.return_id.state not in ("rejected", "closed", "cancelled"))
        return self.qty_delivered - sum(engaged.mapped("quantity"))

    @api.depends("move_ids.state", "move_ids.quantity")
    def _compute_qty_delivered(self):
        for line in self:
            line.qty_delivered = sum(
                m.product_uom._compute_quantity(m.quantity, line.product_uom or m.product_id.uom_id)
                for m in line.move_ids.filtered(lambda m: m.state == "done"))


class FulfillmentGroup(models.Model):
    _name = "leaders.fulfillment.group"
    _description = "Marketplace Fulfillment Group"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(default="New", copy=False, readonly=True)
    vendor_order_id = fields.Many2one("leaders.vendor.order", required=True, ondelete="cascade", index=True)
    vendor_id = fields.Many2one(related="vendor_order_id.vendor_id", store=True, index=True)
    line_ids = fields.One2many("leaders.vendor.order.line", "fulfillment_group_id")
    fulfillment_type = fields.Selection([("fbm","FBM"),("fbl","FBL")], required=True, index=True)
    source_location_id = fields.Many2one("stock.location", required=True)
    picking_ids = fields.One2many("stock.picking", "leaders_fulfillment_group_id", string="Pickings")
    carrier_id = fields.Many2one("delivery.carrier")
    tracking_ref = fields.Char()
    fee_amount = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one(related="vendor_order_id.currency_id", store=True)
    state = fields.Selection([
        ("allocated","Allocated"),("picking","Picking"),("packing","Packing"),
        ("ready","Ready to Ship"),("shipped","Shipped"),
        ("delivery_reported","Delivery Reported"),("delivered","Delivered"),
        ("cancelled","Cancelled")
    ], default="allocated", tracking=True)
    delivered_reported_by = fields.Many2one(
        "res.users", string="Delivery Reported By", readonly=True, copy=False,
        help="Who said the parcel arrived (the vendor, for FBM).")
    delivered_reported_date = fields.Datetime(
        string="Delivery Reported On", readonly=True, copy=False)
    delivered_confirmed_by = fields.Many2one(
        "res.users", string="Delivery Confirmed By", readonly=True, copy=False,
        help="Who turned the report into an actual delivery: the customer, the "
             "marketplace, or the auto-confirm cron.")
    delivered_date = fields.Datetime(string="Delivered On", readonly=True, copy=False)
    delivery_proof = fields.Binary(string="Delivery Proof", attachment=True, copy=False)
    delivery_proof_name = fields.Char(string="Delivery Proof Filename", copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name","New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("leaders.fulfillment.group") or "New"
        return super().create(vals_list)

    def _picking_type(self):
        """FBM ships from the vendor's warehouse; FBL from the main warehouse."""
        self.ensure_one()
        if self.fulfillment_type == "fbm":
            warehouse = self.vendor_id.commercial_partner_id.vendor_warehouse_id
        else:
            warehouse = self.env["res.partner"]._marketplace_main_warehouse()
            if not warehouse:
                raise UserError(_("The marketplace has no main warehouse to ship FBL orders from."))
        if not warehouse:
            raise UserError(_("No warehouse configured for vendor %s.", self.vendor_id.display_name))
        return warehouse.out_type_id

    def _create_pickings(self):
        Picking = self.env["stock.picking"].sudo()
        customer_location = self.env.ref("stock.stock_location_customers")
        for group in self:
            if group.picking_ids or not group.line_ids:
                continue
            order = group.vendor_order_id
            vendor = group.vendor_id.commercial_partner_id
            picking = Picking.create({
                "picking_type_id": group._picking_type().id,
                "location_id": group.source_location_id.id,
                "location_dest_id": customer_location.id,
                "partner_id": (order.partner_shipping_id or order.partner_id).id,
                "origin": order.name,
                "owner_id": vendor.id,
                "leaders_fulfillment_group_id": group.id,
                "move_ids": [(0, 0, {
                    "name": line.name,
                    "product_id": line.product_id.id,
                    "product_uom_qty": line.product_uom_qty,
                    "product_uom": (line.product_uom or line.product_id.uom_id).id,
                    "location_id": group.source_location_id.id,
                    "location_dest_id": customer_location.id,
                    # Only the vendor's own stock can be reserved for this delivery.
                    "restrict_partner_id": vendor.id,
                    "leaders_vendor_line_id": line.id,
                    "origin": order.name,
                }) for line in group.line_ids],
            })
            picking.action_confirm()
            picking.action_assign()
        self._sync_from_pickings()

    def _sync_from_pickings(self):
        for group in self:
            pickings = group.picking_ids.filtered(lambda p: p.state != "cancel")
            if not pickings or group.state in ("delivery_reported", "delivered", "cancelled"):
                continue
            if all(p.state == "done" for p in pickings):
                was_shipped = group.state == "shipped"
                group.state = "shipped"
                refs = [p.carrier_tracking_ref for p in pickings
                        if "carrier_tracking_ref" in p._fields and p.carrier_tracking_ref]
                if refs and not group.tracking_ref:
                    group.tracking_ref = ", ".join(refs)
                if not was_shipped:
                    self.env["leaders.notification"].notify_customer(
                        "customer_order_shipped", group, group.vendor_order_id.partner_id)
            elif all(p.state == "assigned" for p in pickings):
                group.state = "ready"
            else:
                group.state = "allocated"
        self.vendor_order_id._sync_state_from_groups()

    def action_ship(self):
        """Validate the vendor delivery (FBM requires a tracking reference)."""
        for rec in self:
            if rec.state in ("shipped", "delivery_reported", "delivered", "cancelled"):
                continue
            if rec.fulfillment_type == "fbm" and not rec.tracking_ref:
                raise UserError(_("Tracking reference is required for FBM shipments."))
            for picking in rec.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")):
                if picking.state != "assigned":
                    picking.action_assign()
                for move in picking.move_ids:
                    if float_compare(move.quantity, move.product_uom_qty,
                                     precision_rounding=move.product_uom.rounding) < 0:
                        raise UserError(_(
                            "Not enough vendor stock to ship %(product)s (%(done)s of %(needed)s available).",
                            product=move.product_id.display_name, done=move.quantity, needed=move.product_uom_qty))
                if "carrier_tracking_ref" in picking._fields and rec.tracking_ref:
                    picking.carrier_tracking_ref = rec.tracking_ref
                picking.move_ids.picked = True
                picking.with_context(skip_backorder=True, skip_sms=True, skip_immediate=True)._action_done()
            rec._sync_from_pickings()

    @api.model
    def _leaders_fbm_delivery_mode(self):
        """"vendor" (default, the vendor's word is final) or "customer_confirm"."""
        mode = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.fbm_delivery_mode", "vendor")
        return mode if mode in ("vendor", "customer_confirm") else "vendor"

    def action_report_delivery(self, reported_by=None, proof=None, proof_name=None):
        """The vendor reports that their own (FBM) parcel arrived.

        Under the default ``vendor`` mode this *is* the delivery, exactly as
        before. Under ``customer_confirm`` it is only a claim: the group waits
        in ``delivery_reported`` until the customer confirms it, disputes it, or
        the auto-confirm cron closes it. Who reported it and when is recorded in
        both modes, and an optional proof file is stored with it.
        """
        user = reported_by or self.env.user
        for rec in self:
            if rec.state != "shipped":
                raise UserError(_("Only shipped groups can be reported as delivered."))
        vals = {
            "delivered_reported_by": user.id,
            "delivered_reported_date": fields.Datetime.now(),
        }
        if proof:
            vals["delivery_proof"] = proof
            vals["delivery_proof_name"] = proof_name or _("Delivery proof")
        self.write(vals)

        awaiting = self.filtered(
            lambda g: g.fulfillment_type == "fbm"
            and g._leaders_fbm_delivery_mode() == "customer_confirm")
        if awaiting:
            awaiting.write({"state": "delivery_reported"})
            for rec in awaiting:
                rec.vendor_order_id.message_post(body=_(
                    "%(vendor)s reported %(group)s as delivered. Waiting for the "
                    "customer to confirm they received it.",
                    vendor=user.display_name, group=rec.name))
        rest = self - awaiting
        if rest:
            rest.action_delivered(confirmed_by=user)
        return True

    def action_delivered(self, confirmed_by=None):
        """Mark the group actually delivered; releases the vendor order.

        ``confirmed_by`` names the real person when the call is made through a
        sudo record (the customer portal); it defaults to the acting user, which
        is what the back-office Delivered button wants.
        """
        for rec in self:
            if rec.state not in ("shipped", "delivery_reported"):
                raise UserError(_("Only shipped groups can be marked as delivered."))
        self.write({
            "state": "delivered",
            "delivered_confirmed_by": (confirmed_by or self.env.user).id,
            "delivered_date": fields.Datetime.now(),
        })
        for rec in self:
            self.env["leaders.notification"].notify_customer("customer_order_delivered", rec, rec.vendor_order_id.partner_id)
        self.vendor_order_id._sync_state_from_groups()

    def action_delivery_disputed(self, reason=None, raised_by=None):
        """The customer says the parcel never arrived: open a dispute."""
        Dispute = self.env["leaders.vendor.dispute"]
        disputes = Dispute.browse()
        user = raised_by or self.env.user
        for rec in self:
            if rec.state != "delivery_reported":
                raise UserError(_(
                    "Only a reported delivery can be disputed as not received."))
            text = (reason or "").strip() or _("The customer did not receive this shipment.")
            disputes |= Dispute.create({
                "vendor_order_id": rec.vendor_order_id.id,
                "reason": _(
                    "Shipment %(group)s reported as delivered but not received. %(reason)s",
                    group=rec.name, reason=text),
            })
            rec.vendor_order_id.message_post(body=_(
                "%(customer)s reports that %(group)s was never received. A dispute "
                "was opened for the marketplace to review.",
                customer=user.display_name, group=rec.name))
        return disputes

    @api.model
    def cron_auto_confirm_delivery(self):
        """Close reported deliveries the customer never answered.

        Only runs in ``customer_confirm`` mode; a delay of 0 disables it so an
        administrator can require an explicit customer answer forever.
        """
        if self._leaders_fbm_delivery_mode() != "customer_confirm":
            return False
        param = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.fbm_auto_confirm_days", 3)
        try:
            days = int(param)
        except (TypeError, ValueError):
            days = 3
        if days <= 0:
            return False
        deadline = fields.Datetime.now() - timedelta(days=days)
        groups = self.search([
            ("state", "=", "delivery_reported"),
            ("delivered_reported_date", "<=", deadline),
        ])
        for group in groups:
            group.vendor_order_id.message_post(body=_(
                "%(group)s was automatically confirmed as delivered: the customer "
                "did not answer within %(days)s days.", group=group.name, days=days))
            group.action_delivered()
        return True
