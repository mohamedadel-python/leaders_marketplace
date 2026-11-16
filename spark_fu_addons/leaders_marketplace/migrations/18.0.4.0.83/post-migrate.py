"""Rentals: every machine gets an asset state (available) and the rentable ones an asset number."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    cr.execute("UPDATE product_template SET leaders_asset_state = 'available' WHERE leaders_asset_state IS NULL")
    env = api.Environment(cr, SUPERUSER_ID, {})
    for tmpl in env["product.template"].search([("leaders_rentable", "=", True), ("leaders_asset_code", "=", False)]):
        tmpl.leaders_asset_code = tmpl.leaders_serial or "AST-%05d" % tmpl.id
