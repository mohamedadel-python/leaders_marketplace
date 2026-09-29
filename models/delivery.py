from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.tools.misc import format_amount


class VendorDeliveryZone(models.Model):
    _name = "leaders.vendor.delivery.zone"
    _description = "Vendor Delivery Price per Governorate"
    _order = "is_platform desc, vendor_id, state_id"

    # A zone is either a vendor's (vendor_id set) or the platform's
    # (``is_platform`` set, vendor_id empty). The dedicated flag keeps platform
    # rows out of every vendor record rule / portal query (they filter on
    # vendor_id) without inventing a fake vendor partner for the company.
    is_platform = fields.Boolean(
        string="Platform", index=True,
        help="Delivery price of the marketplace itself, charged once for the company's "
             "products and for goods shipped from the marketplace warehouse (FBL).")
    vendor_id = fields.Many2one(
        "res.partner", index=True,
        domain=[("is_marketplace_vendor", "=", True)])
    country_id = fields.Many2one(related="state_id.country_id", store=True, index=True)
    state_id = fields.Many2one("res.country.state", required=True, index=True)
    price = fields.Monetary(currency_field="currency_id", required=True)
    free_shipping_threshold = fields.Monetary(
        currency_field="currency_id",
        help="Optional. When the untaxed amount of the goods this zone ships reaches this "
             "value, the delivery price is waived. Empty or 0 = no free shipping.")
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("vendor_state_uniq", "unique(vendor_id, state_id)",
         "Only one delivery price per governorate per vendor."),
    ]

    @api.constrains("is_platform", "vendor_id", "state_id")
    def _check_scope(self):
        for zone in self:
            if zone.is_platform and zone.vendor_id:
                raise ValidationError(_("A platform delivery price cannot belong to a vendor."))
            if not zone.is_platform and not zone.vendor_id:
                raise ValidationError(_("Please choose the vendor of this delivery price."))
            if zone.is_platform:
                clash = self.with_context(active_test=False).sudo().search_count([
                    ("is_platform", "=", True), ("state_id", "=", zone.state_id.id),
                    ("id", "!=", zone.id)])
                if clash:
                    raise ValidationError(_("Only one platform delivery price per governorate."))

    @api.constrains("price", "free_shipping_threshold")
    def _check_amounts(self):
        for zone in self:
            if zone.price < 0 or zone.free_shipping_threshold < 0:
                raise ValidationError(_("Delivery prices and thresholds cannot be negative."))

    @api.model
    def _zone_for(self, vendor, state):
        """The zone record of ``vendor`` (a res.partner) for ``state``, or empty."""
        if not state:
            return self.browse()
        return self.sudo().search(
            [("vendor_id", "=", vendor.commercial_partner_id.id), ("state_id", "=", state.id)], limit=1)

    @api.model
    def _platform_zone_for(self, state):
        if not state:
            return self.browse()
        return self.sudo().search(
            [("is_platform", "=", True), ("state_id", "=", state.id)], limit=1)

    @api.model
    def _price_for(self, vendor, state):
        if not state:
            return None
        zone = self._zone_for(vendor, state)
        return zone.price if zone else None

    def _price_for_amount(self, amount):
        """Delivery price for goods worth ``amount`` (untaxed): 0 once the free-shipping threshold is met."""
        self.ensure_one()
        threshold = self.free_shipping_threshold
        if threshold and self.currency_id.compare_amounts(amount, threshold) >= 0:
            return 0.0
        return self.price


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _leaders_zone_delivery_plan(self):
        """Who ships what for a per-governorate delivery, and at which price.

        * ``platform``: the marketplace ships the company's own products and every
          FBL line. ONE platform price per checkout (None = no price for the address).
        * ``vendors``: one entry per FBM vendor, ``(vendor, price or None)``.

        :return: dict(state, platform_needed, platform, platform_lines, fbl_lines,
                      vendors, vendor_lines)
        """
        self.ensure_one()
        Zone = self.env["leaders.vendor.delivery.zone"]
        state = self.partner_shipping_id.state_id
        vendor_lines = self._marketplace_vendor_lines()
        company_lines = (self._marketplace_company_lines() - vendor_lines).filtered(
            lambda l: l.product_id.type != "service")
        fbl_lines = vendor_lines.filtered(
            lambda l: l.marketplace_offer_id.fulfillment_type == "fbl")
        fbm_lines = vendor_lines - fbl_lines
        plan = {
            "state": state, "platform_needed": bool(company_lines or fbl_lines),
            "platform": 0.0, "platform_lines": company_lines | fbl_lines,
            "fbl_lines": fbl_lines, "vendors": [], "vendor_lines": {},
        }
        if plan["platform_needed"]:
            zone = Zone._platform_zone_for(state)
            plan["platform"] = zone._price_for_amount(
                sum(plan["platform_lines"].mapped("price_subtotal"))) if zone else None
        by_vendor = {}
        for line in fbm_lines:
            vendor = line.marketplace_offer_id.vendor_id.commercial_partner_id
            by_vendor[vendor] = by_vendor.get(vendor, self.env["sale.order.line"]) | line
        for vendor, lines in by_vendor.items():
            zone = Zone._zone_for(vendor, state)
            price = zone._price_for_amount(sum(lines.mapped("price_subtotal"))) if zone else None
            plan["vendors"].append((vendor, price))
            plan["vendor_lines"][vendor] = lines
        return plan

    def _leaders_delivery_breakdown(self):
        """Delivery charge per shipper, as a list of ``(label, amount)``.

        Before the split it is computed from the zone prices; once the checkout
        was split it is read from what each order actually carries, so the
        confirmation page and emails always match the money.
        """
        self.ensure_one()
        order = self.sudo()
        result = []
        if order.marketplace_split_done or order.vendor_order_ids:
            platform = sum(order.order_line.filtered("is_delivery").mapped("price_subtotal"))
            if platform:
                result.append((_("Leaders delivery"), platform))
            for vendor_order in order.vendor_order_ids:
                amount = sum(vendor_order.line_ids.filtered("is_delivery").mapped("price_subtotal"))
                if amount:
                    vendor = vendor_order.vendor_id
                    result.append((_("%s delivery", vendor.vendor_store_name or vendor.name), amount))
            return result
        if order.carrier_id.delivery_type != "marketplace_zone":
            return result
        plan = order._leaders_zone_delivery_plan()
        if plan["platform_needed"] and plan["platform"] is not None:
            result.append((_("Leaders delivery"), plan["platform"]))
        for vendor, price in plan["vendors"]:
            if price is not None:
                result.append((_("%s delivery", vendor.vendor_store_name or vendor.name), price))
        return result

    def _leaders_delivery_breakdown_text(self):
        """e.g. ``Leaders delivery: 40.00 · Al Nour Store delivery: 30.00``."""
        self.ensure_one()
        currency = self.currency_id or self.env.company.currency_id
        return " · ".join(
            "%s: %s" % (label, format_amount(self.env, amount, currency) if amount else _("Free"))
            for label, amount in self._leaders_delivery_breakdown())


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    delivery_type = fields.Selection(
        selection_add=[("marketplace_zone", "Marketplace Delivery (Per Governorate)")],
        ondelete={"marketplace_zone": "set default"})

    def marketplace_zone_rate_shipment(self, order):
        plan = order._leaders_zone_delivery_plan()
        state = plan["state"]
        total = 0.0
        if plan["platform_needed"]:
            if plan["platform"] is None:
                return {
                    "success": False, "price": 0.0,
                    "error_message": _(
                        "We do not deliver to %(state)s.",
                        state=state.name or _("this address")),
                    "warning_message": False,
                }
            total += plan["platform"]
        for vendor, price in plan["vendors"]:
            if price is None:
                return {
                    "success": False, "price": 0.0,
                    "error_message": _(
                        "%(vendor)s does not deliver to %(state)s.",
                        vendor=vendor.display_name, state=state.name or _("this address")),
                    "warning_message": False,
                }
            total += price
        return {"success": True, "price": total, "error_message": False, "warning_message": False}
