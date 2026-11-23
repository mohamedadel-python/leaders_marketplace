"""Services pages of the storefront contract: the list, one service and its icon."""
from odoo import _, http
from odoo.http import request

from ..services.storefront import Storefront, number
from ..tools.auth_errors import AuthApiError, error_response, json_response, validation_error
from .auth_api import _cors_headers
from .storefront_common import cache_control, rate_limit, storefront_api
from .storefront_orders import _gov_key, _name_sort_key
from .vendor_apply import GOVERNORATES_AR

CACHE = "public, max-age=300"
SORTS = ("recommended", "fastest", "mostJobs")
TYPES = ("platform", "vendor")
DEFAULT_SIZE, MAX_SIZE = 8, 50


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

    # ---------------------------------------------------------------- the providers of a service
    @staticmethod
    def _provider_params(params):
        """The query, checked: every problem in one answer."""
        errors = {}
        q = (params.get("q") or "").strip()
        if len(q) > 100:
            errors["q"] = _("At most 100 characters.")
        kind = params.get("type") or None
        if kind and kind not in TYPES:
            errors["type"] = _("One of: %s.", ", ".join(TYPES))
        sort = params.get("sort") or "recommended"
        if sort not in SORTS:
            errors["sort"] = _("One of: %s.", ", ".join(SORTS))
        page = number(params.get("page")) if params.get("page") else 1
        size = number(params.get("pageSize")) if params.get("pageSize") else DEFAULT_SIZE
        if page is None or int(page) != page or page < 1:
            errors["page"] = _("Must be 1 or more.")
        if size is None or int(size) != size or not 1 <= size <= MAX_SIZE:
            errors["pageSize"] = _("pageSize must be between 1 and %s.", MAX_SIZE)
        if errors:  # this endpoint's contract: 400 (the other endpoints answer 422)
            raise AuthApiError(400, "validation_error", _("Validation failed."), fields=errors)
        return {"q": q.lower(), "type": kind, "governorate": params.get("governorate") or None,
                "specialty": params.get("specialty") or None, "sort": sort, "page": int(page), "pageSize": int(size)}

    @staticmethod
    def _provider_logo(store, provider):
        """The provider's own picture, else the vendor's logo (the company's for the platform); ``None`` when none."""
        if provider.logo:
            stamp = provider.write_date.strftime("%Y-%m-%dT%H:%M:%S") if provider.write_date else ""
            return "%s/service-providers/%s/logo?unique=%s" % (store.base_url, provider.id, stamp)
        return store.seller_logo(None if provider.is_platform else provider.vendor_id.commercial_partner_id)

    @http.route("/service-providers/<int:provider_id>/logo", type="http", auth="public", methods=["GET"], csrf=False,
                save_session=False)
    def provider_logo(self, provider_id, **kw):
        """The picture of a listed provider, to anyone (no cookie); a JSON ``404`` for one that is not listed."""
        provider = request.env["leaders.service.provider"].sudo().browse(provider_id).exists()
        listed = provider and provider in request.env["leaders.service.provider"]._leaders_public(provider.service_id)
        if not listed or not provider.logo:
            return error_response(AuthApiError(404, "not_found", _("Not found.")), _cors_headers())
        stream = request.env["ir.binary"].sudo()._get_image_stream_from(provider, "logo")
        return cache_control(stream.get_response(), CACHE)

    def _provider_row(self, store, provider, arabic_code, states):
        """The card of one provider, and what the filters and the sort need to know about it (kept apart, in ``_x``)."""
        platform = bool(provider.is_platform)
        arabic = (request.env.lang or "").startswith("ar")
        names = {provider.with_context(lang=lang)._leaders_provider_name().lower() for lang in ("en_US", arabic_code) if lang}
        jobs = provider.jobs
        row = {"id": "platform" if platform else str(provider.id), "name": provider._leaders_provider_name(),
               "logo": self._provider_logo(store, provider),
               "type": "platform" if platform else "vendor",
               "typeLabel": _("Platform") if platform else _("Merchant"),
               "verified": bool(provider.verified), "completedJobs": provider._leaders_band(jobs),
               "responseHours": provider.response_hours}
        if provider.all_governorates:
            row["coverage"] = "all"
            keys = set(states)
        else:
            covered = provider.governorate_ids
            row["coverage"] = [{"id": _gov_key(st), "label": (GOVERNORATES_AR.get(st.code) if arabic else None)
                                or st.with_context(lang="en_US").name or ""} for st in covered]
            row["coverage"].sort(key=lambda c: _name_sort_key(c["label"]))
            keys = {_gov_key(st) for st in covered}
        row["specialties"] = [{"id": sp.slug, "label": sp.name} for sp in provider.specialty_ids.sorted("sequence")]
        row["_x"] = {"names": names, "keys": keys, "specialties": {sp.slug for sp in provider.specialty_ids}, "jobs": jobs}
        return row

    @http.route("/services/<string:slug>/providers", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False,
                save_session=False)
    @storefront_api(["GET"])
    def providers(self, cors, slug, **kw):
        """Who carries a service out: the platform and the vendors who chose it, with the filters to narrow them."""
        rate_limit("sf_catalog")
        query = self._provider_params(request.httprequest.args.to_dict())
        store, tmpl = self._find(slug)
        arabic = (request.env.lang or "").startswith("ar")
        arabic_code = store._arabic_code()
        egypt = request.env["res.country.state"].sudo().search([("country_id.code", "=", "EG"), ("code", "in", list(GOVERNORATES_AR))])
        states = {_gov_key(st): st for st in egypt}
        listed = request.env["leaders.service.provider"]._leaders_public(tmpl)
        rows = [self._provider_row(store, p, arabic_code, states) for p in listed]

        # the filters count every provider of the service, whatever is chosen now, so the choices never shrink
        types = [{"id": t, "label": _("Platform") if t == "platform" else _("Merchant"),
                  "count": sum(1 for r in rows if r["type"] == t)} for t in TYPES]
        gov_counts = {key: sum(1 for r in rows if key in r["_x"]["keys"]) for key in states}
        governorates = [{"id": key, "label": (GOVERNORATES_AR.get(st.code) if arabic else None)
                         or st.with_context(lang="en_US").name or key, "count": gov_counts[key]}
                        for key, st in states.items() if gov_counts[key]]
        governorates.sort(key=lambda g: _name_sort_key(g["label"]))
        specialties = []
        for sp in request.env["leaders.service.specialty"].sudo().search([]):
            count = sum(1 for r in rows if sp.slug in r["_x"]["specialties"])
            if count:
                specialties.append({"id": sp.slug, "label": sp.name, "count": count})
        filters = {"types": [t for t in types if t["count"]], "governorates": governorates, "specialties": specialties}

        chosen = [r for r in rows
                  if (not query["q"] or any(query["q"] in n for n in r["_x"]["names"]))
                  and (not query["type"] or r["type"] == query["type"])
                  and (not query["governorate"] or query["governorate"] in r["_x"]["keys"])
                  and (not query["specialty"] or query["specialty"] in r["_x"]["specialties"])]
        if query["sort"] == "fastest":
            chosen.sort(key=lambda r: (r["responseHours"], -r["_x"]["jobs"]))
        elif query["sort"] == "mostJobs":
            chosen.sort(key=lambda r: (-r["completedJobs"]["min"], -r["_x"]["jobs"]))
        else:  # the platform first, then the verified, then who completed the most
            chosen.sort(key=lambda r: (r["type"] != "platform", not r["verified"], -r["_x"]["jobs"]))
        page, size = query["page"], query["pageSize"]
        items = []
        for r in chosen[(page - 1) * size: page * size]:
            items.append({k: v for k, v in r.items() if k != "_x"})
        return json_response({"items": items, "totalCount": len(chosen), "page": page, "pageSize": size,
                              "filters": filters}, 200, cors)

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
