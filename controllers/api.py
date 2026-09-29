import re

from odoo import http
from odoo.http import request

def _json_response(data, status=200):
    return request.make_json_response(data, status=status)

def _terms_pending(vendor):
    """403 payload while the vendor still has to accept the active terms, else None."""
    if not vendor.is_marketplace_vendor:
        return None
    terms = request.env["leaders.vendor.terms"]._pending_for(vendor)
    if not terms:
        return None
    return _json_response({"error": "terms_not_accepted", "terms_version": terms.version,
                           "accept_url": "/my/vendor/terms"}, 403)

class MarketplaceAPI(http.Controller):
    @http.route("/api/v1/vendor/dashboard", type="http", auth="bearer", methods=["GET"], csrf=False)
    def vendor_dashboard(self):
        vendor = request.env.user.partner_id.commercial_partner_id
        if not vendor.is_marketplace_vendor:
            return _json_response({"error":"not_vendor"}, 403)
        blocked = _terms_pending(vendor)
        if blocked:
            return blocked
        return _json_response({
            "vendor_id":vendor.id,
            "vendor_name":vendor.display_name,
            "orders":request.env["leaders.vendor.order"].search_count([("vendor_id","=",vendor.id)]),
            "offers":request.env["leaders.marketplace.offer"].search_count([("vendor_id","=",vendor.id)]),
            "wallet_available":vendor.vendor_wallet_balance,
            "wallet_hold":vendor.vendor_wallet_hold
        })

    @http.route("/api/v1/vendor/orders", type="http", auth="leaders_vendor_bearer", methods=["GET"],
                csrf=False)
    def vendor_orders(self):
        # A marketplace key (B4) gets the paginated, filterable listing; any other
        # bearer token was already checked by the stock "bearer" method and keeps
        # the original answer.
        from .vendor_api import VendorAPI
        if re.match(r"^bearer\s+lmk_", request.httprequest.headers.get("Authorization") or "", re.IGNORECASE):
            return VendorAPI.orders_list_response()
        vendor = request.env.user.partner_id.commercial_partner_id
        blocked = _terms_pending(vendor)
        if blocked:
            return blocked
        orders = request.env["leaders.vendor.order"].search([("vendor_id","=",vendor.id)], order="id desc", limit=100)
        return _json_response({"items":[{
            "id":o.id,"name":o.name,"state":o.state,"amount_total":o.amount_total,
            "vendor_net":o.vendor_net,"currency":o.currency_id.name
        } for o in orders]})

    @http.route("/api/v1/catalog/offers/<int:product_id>", type="http", auth="public", methods=["GET"], csrf=False)
    def product_offers(self, product_id):
        offers = request.env["leaders.marketplace.offer"].sudo().search([
            ("product_id","=",product_id),("state","=","active"),("active","=",True)
        ], order="buybox_score desc, price asc")
        return _json_response({"items":[{
            "id":o.id,"vendor":o.vendor_id.display_name,"price":o.price,
            "currency":o.currency_id.name,"fulfillment_type":o.fulfillment_type,
            "buybox_score":o.buybox_score,"is_buybox_winner":o.is_buybox_winner
        } for o in offers]})
