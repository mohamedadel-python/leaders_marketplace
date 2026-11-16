"""Business types a vendor picks on the public application form (manufacturer, importer...)."""
from odoo import fields, models


class VendorBusinessType(models.Model):
    _name = "leaders.vendor.business.type"
    _description = "Vendor Business Type"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
