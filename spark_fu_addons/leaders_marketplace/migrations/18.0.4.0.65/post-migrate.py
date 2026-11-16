"""The storefront search text now holds Arabic and English names, descriptions, category names and
specs values: recompute it for every existing product."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Template = env["product.template"].with_context(active_test=False)
    templates = Template.search([("leaders_kind", "!=", False)])
    env.add_to_compute(Template._fields["leaders_search_text"], templates)
    templates.flush_recordset(["leaders_search_text"])
