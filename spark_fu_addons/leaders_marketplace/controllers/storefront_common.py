"""Shared plumbing of the storefront contract (catalog, cart, account): JSON, CORS, bearer, errors."""
import json
import logging

from odoo.exceptions import AccessDenied, UserError, ValidationError
from odoo.http import request

from ..tools.auth_errors import AuthApiError, error_response, json_response
from ..tools.auth_tokens import InvalidAccessToken, decode_access_token
from .auth_api import _cors_headers

_logger = logging.getLogger(__name__)
CART_COOKIE = "spark_cart"
CART_MAX_AGE = 30 * 24 * 3600
NO_CACHE_PARAM = "leaders_marketplace.api_no_cache"
RATE_LIMIT_PARAM = "leaders_marketplace.storefront_rate_limit"
ONLINE_PAYMENTS_PARAM = "leaders_marketplace.online_payments"


def online_payments_ready(env):
    """Whether a customer can pay online: a real payment gateway is enabled (or in test mode).

    The ``leaders_marketplace.online_payments`` setting overrides it: ``on`` or ``off``; empty means automatic.
    Wire transfer (``custom``) is not a gateway. While this is False the storefront takes cash on delivery only.
    """
    forced = (env["ir.config_parameter"].sudo().get_param(ONLINE_PAYMENTS_PARAM) or "").strip().lower()
    if forced in ("on", "off"):
        return forced == "on"
    return bool(env["payment.provider"].sudo().search_count([
        ("state", "in", ("enabled", "test")), ("code", "not in", ("custom", "none")),
        ("company_id", "in", (env.company.id, False))]))


def json_body():
    try:
        data = json.loads(request.httprequest.get_data(as_text=True) or "{}")
    except ValueError:
        raise AuthApiError(400, "invalid_request", "The request body is not valid JSON.")
    if not isinstance(data, dict):
        raise AuthApiError(400, "invalid_request", "The request body must be a JSON object.")
    return data


def bearer_user(required=True):
    """The user of the Bearer token. Missing or stale: 401 ``session_expired`` when required, else None."""
    header = request.httprequest.headers.get("Authorization", "")
    expired = AuthApiError(401, "session_expired", "Your session has expired. Sign in again.")
    if not header.startswith("Bearer "):
        if required:
            raise expired
        return None
    try:
        user_id = decode_access_token(request.env, header[7:].strip())
    except InvalidAccessToken:
        raise expired
    user = request.env["res.users"].sudo().browse(user_id).exists()
    if not user or not user.active:
        raise expired
    return user


def cache_control(response, value):
    """Set ``Cache-Control`` to ``value``, or ``no-store`` while the no-cache switch is on (development)."""
    off = request.env["ir.config_parameter"].sudo().get_param(NO_CACHE_PARAM) == "1"
    response.headers["Cache-Control"] = "no-store" if off else value
    return response


def rate_limit(kind, limit=120, window=60, raisable=True):
    """Count the request per IP. A system parameter can raise the per-minute limit, listed IPs are exempt.

    Form endpoints pass ``raisable=False``: their own small limit is an anti-spam rule, not a load rule.
    """
    ip = request.httprequest.remote_addr or ""
    params = request.env["ir.config_parameter"].sudo()
    exempt = params.get_param("leaders_marketplace.storefront_rate_limit_exempt_ips", "")
    if ip in {value.strip() for value in exempt.split(",") if value.strip()}:
        return
    raised = params.get_param(RATE_LIMIT_PARAM, "")
    if raisable and raised.strip().isdigit() and int(raised) > limit:
        limit = int(raised)
    if not request.env["leaders.auth.rate"].sudo().hit("%s:%s" % (kind, ip), limit, window):
        raise AuthApiError(429, "rate_limited", "Too many requests. Wait a moment.")


def storefront_api(methods):
    """Run the handler, answer preflight and render any failure as ``{code, message, fields?}``."""

    def decorate(handler):
        def run(self, **kwargs):
            cors = _cors_headers()
            if request.httprequest.method == "OPTIONS":
                if cors:
                    cors += [("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS"),
                             ("Access-Control-Allow-Headers", "Content-Type, Authorization, Accept-Language"),
                             ("Access-Control-Max-Age", "600")]
                return json_response(None, 204, cors)
            try:
                _apply_language()
                return handler(self, cors, **kwargs)
            except AuthApiError as error:
                return error_response(error, cors)
            except (AccessDenied, UserError, ValidationError) as exc:
                return error_response(AuthApiError(400, "invalid_request", str(exc)), cors)
            except Exception:
                _logger.exception("Storefront API failure")
                return error_response(AuthApiError(500, "server_error", "Something went wrong."), cors)
        run.__name__ = handler.__name__
        return run
    return decorate


def _apply_language():
    """Arabic names when the client asks for Arabic and the language is installed."""
    wanted = (request.httprequest.headers.get("Accept-Language") or "").lower()
    if wanted.startswith("ar"):
        lang = request.env["res.lang"].sudo().search([("code", "like", "ar%"), ("active", "=", True)], limit=1)
        if lang:
            request.update_context(lang=lang.code)
