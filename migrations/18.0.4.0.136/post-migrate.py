"""Every published service gets its own sub-category under Services (load-bank and temporary-power sat on the
Services root, so GET /categories listed 5 services while GET /services listed 7)."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["product.template"].search([("leaders_service_page", "=", True), ("is_published", "=", True)]) \
        ._leaders_ensure_service_tile()
