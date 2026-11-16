"""Vendor products approved before 18.0.4.0.128 whose own offer stayed "Pending Review" (BUG-00871): the offer
is approved too, so the product reaches the website and the vendor sees it active. Through the ORM, so the Buy Box
of the product is computed again."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    templates = env["product.template"].with_context(active_test=False).search(
        [("marketplace_state", "=", "published"), ("marketplace_owner_vendor_id", "!=", False)])
    templates._leaders_activate_owner_offers()
