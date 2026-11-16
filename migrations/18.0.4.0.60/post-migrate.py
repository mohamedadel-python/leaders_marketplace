"""Services cycle: the storefront sends E.164 phones, the API is uncached while in development,
and the two services without a landing tile get an icon."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    params = env["ir.config_parameter"].sudo()
    params.set_param("leaders_marketplace.storefront_service_phone_e164", "1")
    if not params.get_param("leaders_marketplace.api_no_cache"):
        params.set_param("leaders_marketplace.api_no_cache", "1")
    env["product.template"].load_service_icons()
