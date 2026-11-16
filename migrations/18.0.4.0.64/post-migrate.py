"""Services leave the Service Pages screen: a service is a product in a category of the services group.

The landing tile link (``leaders_service_categ_id``) becomes the product's category, a service without a
tile goes into the services root category, and the services' own publish flag becomes ``is_published``.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def _columns(cr, table, names):
    cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s AND column_name IN %s",
               (table, tuple(names)))
    return {row[0] for row in cr.fetchall()}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    old = _columns(cr, "product_template", ("leaders_service_published", "leaders_service_categ_id"))
    if not old:
        return
    cr.execute("SELECT id, %s, %s FROM product_template WHERE leaders_service_page" % (
        "leaders_service_published" if "leaders_service_published" in old else "TRUE",
        "leaders_service_categ_id" if "leaders_service_categ_id" in old else "NULL"))
    rows = cr.fetchall()
    root = env.ref("leaders_marketplace.leaders_categ_services", raise_if_not_found=False)
    Template = env["product.template"].with_context(active_test=False)
    for tmpl_id, published, categ_id in rows:
        tmpl = Template.browse(tmpl_id)
        categ = env["product.public.category"].browse(categ_id) if categ_id else root
        taken = categ != root and Template.search_count([("public_categ_ids", "in", categ.ids)])
        if categ != root and (categ.leaders_slug != tmpl.leaders_slug or taken):
            # a tile opens /services/<its slug>: a service with another slug, or a second one, stays without a tile
            _logger.warning("Service %s moved to the services root: tile %s has slug %s or another service",
                            tmpl.leaders_slug, categ.display_name, categ.leaders_slug)
            categ = root
        tmpl.write({"public_categ_ids": [(6, 0, categ.ids)], "is_published": bool(published),
                    "type": "service"})
    env.add_to_compute(Template._fields["leaders_service_page"], Template.search([]))
    env.flush_all()
    for column in old:
        cr.execute('ALTER TABLE product_template DROP COLUMN IF EXISTS "%s"' % column)
