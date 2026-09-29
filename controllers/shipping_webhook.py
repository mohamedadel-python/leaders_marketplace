"""B6: carrier webhook (POST /api/v1/shipping/webhook/<provider_code>)."""
import logging

from odoo import http
from odoo.http import request

from ..models.shipping_provider import WEBHOOK_MAX_BYTES

_logger = logging.getLogger(__name__)


def _json(data, status=200):
    return request.make_json_response(data, status=status)


class ShippingWebhook(http.Controller):

    @http.route("/api/v1/shipping/webhook/<string:provider_code>", type="http", auth="public",
                methods=["POST"], csrf=False, save_session=False)
    def shipping_webhook(self, provider_code, **kwargs):
        env = request.env(su=True)
        Provider = env["leaders.shipping.provider"]
        if not Provider._leaders_enabled():
            return _json({"error": "not_found"}, 404)
        provider = Provider.search([("code", "=", provider_code), ("active", "=", True)], limit=1)
        if not provider or provider.provider_type == "manual":
            return _json({"error": "not_found"}, 404)
        httpreq = request.httprequest
        if (httpreq.content_length or 0) > WEBHOOK_MAX_BYTES:
            return _json({"error": "payload_too_large"}, 413)
        body = httpreq.stream.read(WEBHOOK_MAX_BYTES + 1) if httpreq.content_length is None \
            else httpreq.get_data()
        if len(body) > WEBHOOK_MAX_BYTES:
            return _json({"error": "payload_too_large"}, 413)
        if not provider._verify_signature(body, httpreq.headers):
            return _json({"error": "invalid_signature"}, 401)
        try:
            counts, status = provider._process_webhook(body, httpreq.headers)
        except Exception:
            _logger.exception("Shipping webhook %s failed", provider_code)
            return _json({"error": "server_error"}, 500)
        return _json(counts, status)
