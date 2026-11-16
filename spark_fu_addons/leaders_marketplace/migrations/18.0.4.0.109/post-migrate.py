"""Category slugs are now made from the English name: remake the slugs typed by hand on the storefront's
categories (the shipped ones keep theirs)."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    categories = env["product.public.category"].with_context(active_test=False).search([])
    categories._leaders_refresh_slugs()
