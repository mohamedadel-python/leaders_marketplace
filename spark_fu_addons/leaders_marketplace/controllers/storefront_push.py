"""Devices of the push notifications: the app tells the server its Firebase token (and when it signs out).

``POST /account/devices`` registers the token of the signed-in person's device, ``DELETE /account/devices`` removes it.
Both are new routes; nothing that exists changes. The Bearer token is the one of ``POST /auth/login``.
"""
import re

from odoo import http
from odoo.http import request

from ..tools.auth_errors import json_response, validation_error
from .storefront_common import bearer_user, json_body, rate_limit, storefront_api

TOKEN_RE = re.compile(r"^\S{20,4096}$")  # a Firebase token is one long string without spaces
PLATFORMS = ("android", "ios", "web")


class StorefrontPush(http.Controller):

    @http.route("/account/devices", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def register(self, cors, **kw):
        """Body ``{"token": "...", "platform": "android|ios|web", "locale": "ar|en"}`` (platform and locale optional)."""
        rate_limit("sf_push")
        user = bearer_user(required=True)
        body = json_body()
        errors = {}
        token = body.get("token")
        if not isinstance(token, str) or not TOKEN_RE.match(token.strip()):
            errors["token"] = "Send the Firebase registration token of this device."
        platform = body.get("platform", "android")
        if platform not in PLATFORMS:
            errors["platform"] = "One of: %s." % ", ".join(PLATFORMS)
        locale = body.get("locale")
        if locale not in (None, "ar", "en"):
            errors["locale"] = "ar or en."
        if errors:
            raise validation_error(errors)
        request.env["leaders.push.device"]._leaders_register(user, token, platform, locale)
        return json_response({"registered": True}, 200, cors)

    @http.route("/account/devices", type="http", auth="public", methods=["DELETE", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["DELETE"])
    def unregister(self, cors, **kw):
        """The token goes in the body (``{"token": "..."}``) or in the query (``?token=...``)."""
        rate_limit("sf_push")
        user = bearer_user(required=True)
        token = request.httprequest.args.get("token")
        if not token and request.httprequest.get_data():
            token = json_body().get("token")
        if not isinstance(token, str) or not token.strip():
            raise validation_error({"token": "Send the token to remove."})
        request.env["leaders.push.device"]._leaders_unregister(user, token)
        return json_response(None, 204, cors)
