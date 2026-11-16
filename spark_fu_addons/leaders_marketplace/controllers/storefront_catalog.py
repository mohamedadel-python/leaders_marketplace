"""Catalog endpoints of the storefront contract: categories, products, facets, shelves."""
from odoo import http
from odoo.http import request

from ..services.storefront import Storefront, number
from ..tools.auth_errors import AuthApiError, json_response
from ..tools.auth_errors import validation_error
from .storefront_common import cache_control, json_body, rate_limit, storefront_api


class StorefrontCatalog(http.Controller):

    def _find(self, slug):
        store = Storefront(request.env)
        tmpl = store.Template.search(store.base_domain() + [("leaders_slug", "=", slug)], limit=1)
        if not tmpl:
            raise AuthApiError(404, "not_found", "Product not found.")
        return store, tmpl

    @http.route("/categories", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def categories(self, cors, **kw):
        rate_limit("sf_catalog")
        return cache_control(json_response(Storefront(request.env).categories(), 200, cors), "public, max-age=300")

    @http.route("/products", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def products(self, cors, **kw):
        rate_limit("sf_catalog")
        return json_response(Storefront(request.env).listing(request.httprequest.args.to_dict()), 200, cors)

    @http.route("/products/facets", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def facets(self, cors, **kw):
        rate_limit("sf_catalog")
        return json_response(Storefront(request.env).facets(request.httprequest.args.to_dict()), 200, cors)

    @http.route("/products/filters", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def filters(self, cors, **kw):
        rate_limit("sf_catalog")
        return json_response(Storefront(request.env).filters(request.httprequest.args.to_dict()), 200, cors)

    @http.route("/products/search-click", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False,
                save_session=False)
    @storefront_api(["POST"])
    def search_click(self, cors, **kw):
        """The customer opened a product from search results: feeds the click rate of the search report."""
        rate_limit("sf_catalog")
        body = json_body()
        errors = {}
        search_id, product_id = str(body.get("searchId") or ""), str(body.get("productId") or "")
        if not search_id.isdigit():
            errors["searchId"] = "Required."
        if not product_id.isdigit():
            errors["productId"] = "Required."
        if errors:
            raise validation_error(errors)
        request.env["leaders.search.log"].sudo().leaders_click(int(search_id), int(product_id))
        return json_response(None, 204, cors)

    @http.route("/products/<string:slug>", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def product(self, cors, slug, **kw):
        rate_limit("sf_catalog")
        store, tmpl = self._find(slug)
        return json_response(store.product(tmpl), 200, cors)

    @http.route("/products/<string:slug>/related", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def related(self, cors, slug, **kw):
        rate_limit("sf_catalog")
        store, tmpl = self._find(slug)
        limit = min(max(int(number(request.httprequest.args.get("limit")) or 4), 1), 12)
        return json_response(store.related(tmpl, limit), 200, cors)

    @http.route("/products/<string:slug>/bought-together", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def bought_together(self, cors, slug, **kw):
        rate_limit("sf_catalog")
        store, tmpl = self._find(slug)
        limit = min(max(int(number(request.httprequest.args.get("limit")) or 4), 1), 12)
        return json_response(store.bought_together(tmpl, limit), 200, cors)
