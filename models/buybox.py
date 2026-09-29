from odoo import api, fields, models


class BuyBoxConfig(models.Model):
    """Admin-configurable weights for the Buy Box winner formula.

    A singleton-style config (the active record with the lowest sequence is used).
    All weights are editable from the UI so the business can tune the winner
    selection without a code change.
    """
    _name = "leaders.buybox.config"
    _description = "Buy Box Configuration"
    _order = "sequence, id"

    name = fields.Char(required=True, default="Default Buy Box Policy")
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    # Weights (relative importance of each signal). Editable from the UI.
    price_weight = fields.Float(default=50.0, help="Higher = cheaper offers win more.")
    rating_weight = fields.Float(default=20.0, help="Weight of vendor/offer rating.")
    availability_weight = fields.Float(default=15.0, help="Weight of being in stock.")
    shipping_speed_weight = fields.Float(default=10.0, help="Weight of faster fulfilment.")
    vendor_tier_weight = fields.Float(default=5.0, help="Weight of vendor tier rank.")

    # FBL is generally faster/safer: give its shipping-speed signal a boost.
    fbl_shipping_bonus = fields.Float(default=1.0, help="Shipping score for FBL (0..1).")
    fbm_shipping_bonus = fields.Float(default=0.5, help="Shipping score for FBM (0..1).")

    # Offers with zero available stock must never win the Buy Box.
    require_stock = fields.Boolean(default=True)

    @api.model
    def get_active(self):
        cfg = self.search([("active", "=", True)], order="sequence, id", limit=1)
        if not cfg:
            cfg = self.create({"name": "Default Buy Box Policy"})
        return cfg
