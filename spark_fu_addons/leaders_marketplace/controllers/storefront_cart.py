"""Cart endpoints of the storefront contract. Bearer optional; guests are keyed by ``spark_cart``."""
from odoo import http
from odoo.http import request

from ..services.storefront_cart import CartService
from ..tools.auth_errors import json_response
from .auth_api import COOKIE_DOMAIN_PARAM, COOKIE_SECURE_PARAM, _param
from .storefront_common import CART_COOKIE, CART_MAX_AGE, bearer_user, json_body, rate_limit, storefront_api


def _service():
    user = bearer_user(required=False)
    return CartService(request.env, user, request.httprequest.cookies.get(CART_COOKIE))


def _cart_response(cors, service, cart, status=200):
    response = json_response(service.render(cart), status, cors)
    if not service.user and cart and cart.token != service.token:
        secure = _param(COOKIE_SECURE_PARAM, "True").lower() not in ("false", "0")
        response.set_cookie(CART_COOKIE, cart.token, max_age=CART_MAX_AGE, httponly=True, secure=secure,
                            samesite="None", path="/", domain=_param(COOKIE_DOMAIN_PARAM) or None)
    return response


class StorefrontCart(http.Controller):

    @http.route("/cart", type="http", auth="public", methods=["GET", "DELETE", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET", "DELETE"])
    def cart(self, cors, **kw):
        rate_limit("sf_cart")
        service = _service()
        if request.httprequest.method == "DELETE":
            service.clear()
            return json_response(None, 204, cors)
        return _cart_response(cors, service, service.find())

    @http.route("/cart/items", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def add(self, cors, **kw):
        rate_limit("sf_cart")
        service = _service()
        return _cart_response(cors, service, service.add(json_body()))

    @http.route("/cart/items/<string:line_id>", type="http", auth="public", methods=["PATCH", "DELETE", "OPTIONS"],
                csrf=False, save_session=False)
    @storefront_api(["PATCH", "DELETE"])
    def line(self, cors, line_id, **kw):
        rate_limit("sf_cart")
        service = _service()
        if request.httprequest.method == "DELETE":
            return _cart_response(cors, service, service.remove(line_id))
        return _cart_response(cors, service, service.update(line_id, json_body()))

    @http.route("/cart/merge", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def merge(self, cors, **kw):
        rate_limit("sf_cart")
        user = bearer_user(required=True)
        service = CartService(request.env, user, None)
        cart = service.merge_guest(request.httprequest.cookies.get(CART_COOKIE))
        response = _cart_response(cors, service, cart)
        response.delete_cookie(CART_COOKIE, path="/", domain=_param(COOKIE_DOMAIN_PARAM) or None)
        return response
