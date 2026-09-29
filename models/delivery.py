from odoo import api, fields, models, _


class VendorDeliveryZone(models.Model):
    _name = "leaders.vendor.delivery.zone"
    _description = "Vendor Delivery Price per Governorate"
    _order = "vendor_id, state_id"

    vendor_id = fields.Many2one(
        "res.partner", required=True, index=True,
        domain=[("is_marketplace_vendor", "=", True)])
    country_id = fields.Many2one(related="state_id.country_id", store=True, index=True)
    state_id = fields.Many2one("res.country.state", required=True, index=True)
    price = fields.Monetary(currency_field="currency_id", required=True)
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("vendor_state_uniq", "unique(vendor_id, state_id)",
         "Only one delivery price per governorate per vendor."),
    ]

    @api.model
    def _price_for(self, vendor, state):
        if not state:
            return None
        zone = self.sudo().search(
            [("vendor_id", "=", vendor.commercial_partner_id.id), ("state_id", "=", state.id)], limit=1)
        return zone.price if zone else None


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    delivery_type = fields.Selection(
        selection_add=[("marketplace_zone", "Marketplace Delivery (Per Governorate)")],
        ondelete={"marketplace_zone": "set default"})

    def marketplace_zone_rate_shipment(self, order):
        Zone = self.env["leaders.vendor.delivery.zone"]
        state = order.partner_shipping_id.state_id
        total = 0.0
        for vendor in order._marketplace_vendor_lines().mapped("marketplace_offer_id.vendor_id"):
            price = Zone._price_for(vendor, state)
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
