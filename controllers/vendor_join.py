"""The "Join as a vendor" page: the two ways to sell compared side by side (project guide, feature 3).

``/vendor/join`` on the Odoo website, and ``GET /vendor-join`` for the storefront (same data, its own design).
"""
from odoo import http
from odoo.http import request

from ..tools.auth_errors import json_response
from .storefront_common import rate_limit, storefront_api


def _join_models():
    return request.env["leaders.vendor.join.model"].sudo().search([])


class VendorJoin(http.Controller):

    @http.route("/vendor/join", type="http", auth="public", website=True, sitemap=True)
    def vendor_join_page(self, **kw):
        return request.render("leaders_marketplace.vendor_join_page", {"join_models": _join_models()})

    @http.route("/vendor-join", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def vendor_join(self, cors, **kw):
        rate_limit("sf_catalog")
        models = _join_models()
        return json_response({
            "freePeriodMonths": max(models.mapped("free_months") or [0]),
            "models": [{"code": m.code, "name": m.name, "summary": m.summary or "", "whatYouPay": m.what_you_pay,
                        "nameShown": m.name_shown, "buyerContact": m.buyer_contact, "freeMonths": m.free_months}
                       for m in models],
        }, 200, cors)
