"""Services pages of the storefront contract: the list, one service and its icon."""
from odoo import _, http
from odoo.http import request

from ..services.storefront import Storefront
from ..tools.auth_errors import AuthApiError, error_response, json_response
from .auth_api import _cors_headers
from .storefront_common import cache_control, rate_limit, storefront_api

CACHE = "public, max-age=300"


class StorefrontServices(http.Controller):

    def _find(self, slug):
        store = Storefront(request.env)
        tmpl = store.Template.search(store.service_domain() + [("leaders_slug", "=", slug)], limit=1)
        if not tmpl:
            raise AuthApiError(404, "not_found", _("Service not found."))
        return store, tmpl

    @http.route("/services", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def services(self, cors, **kw):
        rate_limit("sf_catalog")
        return cache_control(json_response(Storefront(request.env).services(), 200, cors), CACHE)

    @http.route("/services/<string:slug>", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def service(self, cors, slug, **kw):
        rate_limit("sf_catalog")
        store, tmpl = self._find(slug)
        return cache_control(json_response(store.service(tmpl), 200, cors), CACHE)

    @http.route("/services/<string:slug>/icon", type="http", auth="public", methods=["GET"], csrf=False, save_session=False)
    def icon(self, slug, **kw):
        """The service's own icon, else the icon of its landing tile; a JSON ``404`` when it has neither."""
        store = Storefront(request.env)
        tmpl = store.Template.search(store.service_domain() + [("leaders_slug", "=", slug)], limit=1)
        record, field = store.service_icon(tmpl)
        if not record:
            return error_response(AuthApiError(404, "not_found", _("This service has no icon.")), _cors_headers())
        stream = request.env["ir.binary"]._get_image_stream_from(record, field)
        return cache_control(stream.get_response(), CACHE)
