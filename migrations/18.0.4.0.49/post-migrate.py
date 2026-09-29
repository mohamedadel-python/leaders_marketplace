"""Storefront slugs for the vendors that already exist.

``vendor_store_slug`` was never filled. New vendors get one when they are
approved; this gives the same to those approved before.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    vendors = env["res.partner"].with_context(active_test=False).search([
        ("is_marketplace_vendor", "=", True),
        ("vendor_state", "in", ("approved", "frozen")),
        ("vendor_store_slug", "in", (False, "")),
    ], order="id")
    vendors._marketplace_ensure_store_slug()
