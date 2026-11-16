"""Signed-in account endpoints of the storefront contract."""
import base64

from odoo import http
from odoo.http import request
from odoo.tools.mimetypes import guess_mimetype

from ..services.storefront import Storefront
from ..services.storefront_account import AccountService
from ..tools.auth_errors import AuthApiError, json_response, validation_error
from .auth_api import _session_cookies
from .storefront_common import bearer_user, json_body, rate_limit, storefront_api


AVATAR_TYPES = ("image/jpeg", "image/png", "image/webp")
AVATAR_MAX_BYTES = 5 * 1024 * 1024


def _service():
    return AccountService(request.env, bearer_user(required=True))


class StorefrontAccount(http.Controller):

    @http.route("/account/profile", type="http", auth="public", methods=["GET", "PATCH", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET", "PATCH"])
    def profile(self, cors, **kw):
        rate_limit("sf_account")
        service = _service()
        if request.httprequest.method == "PATCH":
            return json_response(service.update_profile(json_body()), 200, cors)
        return json_response(service.profile(), 200, cors)

    @http.route("/account/business", type="http", auth="public", methods=["PATCH", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["PATCH"])
    def business(self, cors, **kw):
        rate_limit("sf_account")
        return json_response(_service().update_business(json_body()), 200, cors)

    @http.route("/account/orders", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def orders(self, cors, **kw):
        rate_limit("sf_account")
        return json_response(_service().orders(request.httprequest.args.to_dict()), 200, cors)

    @http.route("/account/notifications", type="http", auth="public", methods=["GET", "PUT", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET", "PUT"])
    def notifications(self, cors, **kw):
        rate_limit("sf_account")
        service = _service()
        if request.httprequest.method == "PUT":
            return json_response(service.save_prefs(json_body()), 200, cors)
        return json_response(service.prefs(), 200, cors)

    @http.route("/account/password", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def password(self, cors, **kw):
        rate_limit("sf_account")
        service = _service()
        Token = request.env["leaders.auth.refresh.token"].sudo()
        # the refresh cookie is scoped to /auth and never reaches this route, so the current device
        # cannot be told apart: drop every refresh token and give this device a fresh one
        latest = Token.search([("user_id", "=", service.user.id), ("revoked", "=", False)], order="id desc", limit=1)
        remember = bool(latest.remember)
        service.change_password(json_body())
        Token.revoke_user(service.user)
        headers = request.httprequest.headers
        raw = Token.issue(service.user, remember, user_agent=headers.get("User-Agent", ""),
                          ip_address=request.httprequest.remote_addr or "")
        response = json_response(None, 204, cors)
        return _session_cookies(response, raw, Token.lifetime_seconds(remember))

    # ---------------------------------------------------------------- avatar
    @http.route("/account/avatar", type="http", auth="public", methods=["POST", "DELETE", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST", "DELETE"])
    def avatar(self, cors, **kw):
        rate_limit("sf_account")
        service = _service()
        if request.httprequest.method == "DELETE":
            service.partner.image_1920 = False
            return json_response(service.profile(), 200, cors)
        upload = request.httprequest.files.get("file")
        data = upload.read() if upload else b""
        if not data:
            raise validation_error({"file": "Choose a picture file."})
        if len(data) > AVATAR_MAX_BYTES:
            raise validation_error({"file": "The picture can be at most 5 MB."})
        if guess_mimetype(data, default="") not in AVATAR_TYPES:
            raise validation_error({"file": "The picture must be a JPEG, PNG or WebP file."})
        service.partner.image_1920 = base64.b64encode(data)
        return json_response(service.profile(), 200, cors)

    @http.route("/account/avatar/<int:partner_id>/<string:signature>", type="http", auth="public", methods=["GET"],
                csrf=False, save_session=False)
    def avatar_image(self, partner_id, signature, **kw):
        """The picture of ``GET /account/profile``'s ``avatar``: readable by the link alone, which only its owner
        is given. A stale or forged signature is 404; the link changes with the picture, so it is cached for good."""
        import hmac as std_hmac
        from werkzeug.exceptions import NotFound
        from ..services.auth_service import AuthService
        partner = request.env["res.partner"].sudo().browse(partner_id).exists()
        auth = AuthService(request.env)
        if not partner or not std_hmac.compare_digest(auth.avatar_signature(partner), signature) \
                or not partner.image_1920 or auth._is_generated_svg(partner.image_1920):
            raise NotFound()
        stream = request.env["ir.binary"].sudo()._get_image_stream_from(partner, "avatar_256")
        return stream.get_response(max_age=365 * 24 * 3600, immutable=True)

    # -------------------------------------------------------------- wishlist
    @http.route("/wishlist", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def wishlist(self, cors, **kw):
        rate_limit("sf_account")
        user = bearer_user(required=True)
        store = Storefront(request.env)
        items = request.env["leaders.wishlist.item"].sudo().search([("user_id", "=", user.id)])
        listed = store.Template.search(store.base_domain() + [("id", "in", items.product_tmpl_id.ids)])
        # newest first, and only products still on sale
        return json_response([store.product(item.product_tmpl_id) for item in items
                              if item.product_tmpl_id in listed], 200, cors)

    @http.route("/wishlist/<string:product_id>", type="http", auth="public", methods=["PUT", "DELETE", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["PUT", "DELETE"])
    def wishlist_item(self, cors, product_id, **kw):
        rate_limit("sf_account")
        user = bearer_user(required=True)
        Item = request.env["leaders.wishlist.item"].sudo()
        tmpl_id = int(product_id) if product_id.isdigit() else 0
        if request.httprequest.method == "DELETE":
            Item.search([("user_id", "=", user.id), ("product_tmpl_id", "=", tmpl_id)]).unlink()
            return json_response(None, 204, cors)
        store = Storefront(request.env)
        if not store.Template.search_count(store.base_domain() + [("id", "=", tmpl_id)]):
            raise AuthApiError(404, "not_found", "Product not found.")
        if not Item.search_count([("user_id", "=", user.id), ("product_tmpl_id", "=", tmpl_id)]):
            Item.create({"user_id": user.id, "product_tmpl_id": tmpl_id})
        return json_response(None, 204, cors)
