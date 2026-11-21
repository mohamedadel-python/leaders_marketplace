"""Vendor deliveries that were waiting for stock before the vendor added it never took that stock. Reserve it now."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["stock.picking"].search([
        ("leaders_fulfillment_group_id", "!=", False), ("state", "in", ("confirmed", "waiting"))]).action_assign()
