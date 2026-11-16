"""Auth API contract (login, registration, password reset) for the Next.js client.

JSON in, JSON out, no envelope, camelCase. The access token lives in browser
memory; the refresh token travels only in an HttpOnly cookie scoped to /auth,
next to a readable ``spark_session`` marker cookie.
"""
import json
import logging

from odoo import http
from odoo.exceptions import AccessDenied, UserError, ValidationError
from odoo.http import request

from ..services.auth_service import AuthService
from ..tools.auth_errors import AuthApiError, error_response, json_response, validation_error
from ..tools.auth_tokens import InvalidAccessToken, decode_access_token

_logger = logging.getLogger(__name__)

REFRESH_COOKIE = "refresh_token"
MARKER_COOKIE = "spark_session"
CORS_PARAM = "leaders_marketplace.auth_cors_origin"
COOKIE_DOMAIN_PARAM = "leaders_marketplace.auth_cookie_domain"
COOKIE_SECURE_PARAM = "leaders_marketplace.auth_cookie_secure"


def _param(key, default=""):
    return request.env["ir.config_parameter"].sudo().get_param(key) or default


def _cors_headers():
    configured = _param(CORS_PARAM)
    allowed_origins = {
        origin.strip()
        for origin in configured.split(",")
        if origin.strip()
    }
    request_origin = request.httprequest.headers.get("Origin", "")
    if request_origin not in allowed_origins:
        return []
    return [("Access-Control-Allow-Origin", request_origin), ("Access-Control-Allow-Credentials", "true"),
            ("Vary", "Origin")]


def _json_body():
    try:
        data = json.loads(request.httprequest.get_data(as_text=True) or "{}")
    except ValueError:
        raise AuthApiError(400, "invalid_request", "The request body is not valid JSON.")
    if not isinstance(data, dict):
        raise AuthApiError(400, "invalid_request", "The request body must be a JSON object.")
    return data


def _session_cookies(response, raw_refresh, max_age):
    """Set (``raw_refresh`` given) or clear the refresh cookie and the marker."""
    domain = _param(COOKIE_DOMAIN_PARAM) or None
    common = {"secure": True, "samesite": "None", "domain": domain}
    if raw_refresh:
        response.set_cookie(REFRESH_COOKIE, raw_refresh, max_age=max_age, httponly=True, path="/auth", **common)
        response.set_cookie(MARKER_COOKIE, "1", max_age=max_age, httponly=False, path="/", **common)
    else:
        response.delete_cookie(REFRESH_COOKIE, path="/auth", domain=domain, secure=True, samesite="None")
        response.delete_cookie(MARKER_COOKIE, path="/", domain=domain, secure=True, samesite="None")
    return response


def _api(handler):
    """Run ``handler`` and render success or any failure in the contract shape."""
    def run(self, **kwargs):
        cors = _cors_headers()
        if request.httprequest.method == "OPTIONS":  # Odoo routes preflight to the same endpoint
            if cors:
                cors += [("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS"),
                         ("Access-Control-Allow-Headers", "Content-Type, Authorization"),
                         ("Access-Control-Max-Age", "600")]
            return json_response(None, 204, cors)
        try:
            return handler(self, cors, **kwargs)
        except AuthApiError as error:
            clear = error.code == "session_expired" and request.httprequest.path.endswith("/refresh")
            response = error_response(error, cors)
            return _session_cookies(response, None, 0) if clear else response
        except (AccessDenied, UserError, ValidationError) as exc:
            return error_response(AuthApiError(400, "invalid_request", str(exc)), cors)
        except Exception:
            _logger.exception("Auth API failure")
            return error_response(AuthApiError(500, "server_error", "Something went wrong."), cors)
    run.__name__ = handler.__name__
    return run


class AuthApiController(http.Controller):

    def _service(self):
        return AuthService(request.env)

    @staticmethod
    def _client():
        httprequest = request.httprequest
        return httprequest.headers.get("User-Agent", "")[:500], httprequest.remote_addr or ""

    def _session_response(self, cors, status, result):
        payload, raw, remember = result
        max_age = request.env["leaders.auth.refresh.token"].lifetime_seconds(remember)
        return _session_cookies(json_response(payload, status, cors), raw, max_age)

    @http.route("/auth/register", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    @_api
    def register(self, cors, **kw):
        agent, ip = self._client()
        result = self._service().register(_json_body(), agent, ip)
        if isinstance(result, dict):  # sign-up code sent: the session opens at /auth/register/verify
            return json_response(result, 202, cors)
        return self._session_response(cors, 201, result)

    @http.route("/auth/register/verify", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    @_api
    def register_verify(self, cors, **kw):
        agent, ip = self._client()
        return self._session_response(cors, 201, self._service().verify_signup(_json_body(), agent, ip))

    @http.route("/auth/register/resend", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    @_api
    def register_resend(self, cors, **kw):
        self._service().resend_signup(_json_body(), self._client()[1])
        return json_response(None, 202, cors)

    @http.route("/auth/login", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    @_api
    def login(self, cors, **kw):
        agent, ip = self._client()
        return self._session_response(cors, 200, self._service().login(_json_body(), agent, ip))

    @http.route("/auth/google", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    @_api
    def google(self, cors, **kw):
        agent, ip = self._client()
        return self._session_response(cors, 200, self._service().google(_json_body(), agent, ip))

    @http.route("/auth/refresh", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    @_api
    def refresh(self, cors, **kw):
        agent, ip = self._client()
        raw = request.httprequest.cookies.get(REFRESH_COOKIE, "")
        return self._session_response(cors, 200, self._service().refresh(raw, agent, ip))

    @http.route("/auth/logout", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    @_api
    def logout(self, cors, **kw):
        header = request.httprequest.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise AuthApiError(401, "session_expired", "Your session has expired. Sign in again.")
        try:
            user_id = decode_access_token(request.env, header[7:].strip())
        except InvalidAccessToken:
            raise AuthApiError(401, "session_expired", "Your session has expired. Sign in again.")
        self._service().logout(request.httprequest.cookies.get(REFRESH_COOKIE, ""), user_id)
        return _session_cookies(json_response(None, 204, cors), None, 0)

    @http.route("/auth/logout-all", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    @_api
    def logout_all(self, cors, **kw):
        """Sign out of every device: all the sessions of the account end (the access token expires on its own)."""
        header = request.httprequest.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise AuthApiError(401, "session_expired", "Your session has expired. Sign in again.")
        try:
            user_id = decode_access_token(request.env, header[7:].strip())
        except InvalidAccessToken:
            raise AuthApiError(401, "session_expired", "Your session has expired. Sign in again.")
        self._service().logout_all(request.env["res.users"].sudo().browse(user_id))
        return _session_cookies(json_response(None, 204, cors), None, 0)

    @http.route("/auth/password/forgot", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    @_api
    def forgot(self, cors, **kw):
        self._service().forgot(_json_body(), self._client()[1])
        return json_response(None, 202, cors)

    @http.route("/auth/password/verify", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    @_api
    def verify(self, cors, **kw):
        return json_response(self._service().verify(_json_body(), self._client()[1]), 200, cors)

    @http.route("/auth/password/reset", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    @_api
    def reset(self, cors, **kw):
        self._service().reset(_json_body(), self._client()[1])
        return json_response(None, 204, cors)
