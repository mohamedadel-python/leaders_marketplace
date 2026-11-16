"""Installation and Delivery join the services: their category tiles get a description and an icon.

Only the two new categories are touched (the other categories keep what the admin set).
"""
import base64

from odoo import SUPERUSER_ID, api
from odoo.addons.leaders_marketplace.models.catalog_seed import DEMO_CATEGORY_TEXT, DEMO_ICON_PATHS

NEW = ("leaders_categ_installation", "leaders_categ_delivery")


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    arabic = [code for code, _n in env["res.lang"].get_installed() if code.split("_")[0] == "ar"]
    for xmlid in NEW:
        categ = env.ref("leaders_marketplace." + xmlid, raise_if_not_found=False)
        if not categ:
            continue
        en, ar = DEMO_CATEGORY_TEXT[xmlid]
        if not categ.leaders_description:
            categ.leaders_description = en
            for lang in arabic:
                categ.with_context(lang=lang).leaders_description = ar
        if not categ.leaders_icon:
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="96" height="96" fill="none" '
                   'stroke="#1f2937" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">%s</svg>'
                   % DEMO_ICON_PATHS[xmlid])
            categ.leaders_icon = base64.b64encode(svg.encode())
