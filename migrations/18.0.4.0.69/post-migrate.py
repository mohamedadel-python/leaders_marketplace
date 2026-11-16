"""Inspection before publishing becomes a subcategory setting (on for the equipment and power subcategories).

The used products of those subcategories that are not inspected yet leave the storefront until inspected.
"""
from odoo import SUPERUSER_ID, api

SLUGS = ("generators", "lighting-tower", "load-bank", "loader", "forklift", "welding-machine")


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["product.public.category"].with_context(active_test=False).search(
        [("leaders_slug", "in", SLUGS)]).write({"leaders_inspection_required": True})
    templates = env["product.template"].with_context(active_test=False).search([("leaders_kind", "!=", False)])
    env.add_to_compute(templates._fields["leaders_requires_inspection"], templates)
    templates.flush_recordset()
