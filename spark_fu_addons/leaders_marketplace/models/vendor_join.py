"""The two ways to sell on the platform, compared side by side on the "Join as a vendor" page (project guide,
feature 3): what the vendor pays, whether the buyer sees the vendor's name, who talks to the buyer and the free
period. The admin edits them (Arabic and English); the storefront reads them from GET /vendor-join."""
from odoo import fields, models


class LeadersVendorJoinModel(models.Model):
    _name = "leaders.vendor.join.model"
    _description = "Vendor Join Model"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    code = fields.Char(required=True, help="Stable key sent to the storefront: subscription, commission.")
    name = fields.Char(required=True, translate=True)
    summary = fields.Char(translate=True, help="One sentence under the name.")
    what_you_pay = fields.Char("What the vendor pays", required=True, translate=True)
    name_shown = fields.Boolean("The buyer sees the vendor's name")
    buyer_contact = fields.Char("Who talks to the buyer", required=True, translate=True)
    free_months = fields.Integer("Free period (months)", default=3)
    active = fields.Boolean(default=True)

    _sql_constraints = [("code_unique", "unique(code)", "This code already exists.")]
