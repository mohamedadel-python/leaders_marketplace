"""Storefront content endpoints: equipment listings, site settings, pages, company film and testimonials."""
from odoo import _, fields, http
from odoo.http import request

from ..models.storefront_content import video_asset
from ..services.auth_service import PHONE_RE
from ..tools.auth_errors import AuthApiError, error_response, json_response, validation_error
from .auth_api import _cors_headers
from .storefront_common import bearer_user, cache_control, json_body, rate_limit, storefront_api

INTENTS = ("sell", "rent")
CONDITIONS = ("new", "used", "refurbished")
IMAGE_CACHE = "public, max-age=300"


def _base_url():
    return request.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")


def _stamp(record):
    """Changes with the record, so a new picture gets a new URL."""
    return fields.Datetime.to_string(record.write_date or fields.Datetime.now()).replace(" ", "T")


def _image(record, field):
    """The picture of ``record`` to anyone; a JSON 404 when there is none."""
    if not record or not record[field]:
        return error_response(AuthApiError(404, "not_found", "Not found."), _cors_headers())
    stream = request.env["ir.binary"]._get_image_stream_from(record, field)
    return cache_control(stream.get_response(), IMAGE_CACHE)


class StorefrontContent(http.Controller):

    # ------------------------------------------------------- equipment listings
    @staticmethod
    def _check_listing(body):
        errors = {}

        def text(key):
            value = body.get(key)
            return value.strip() if isinstance(value, str) else value

        name, phone, kind = text("name"), text("phone"), text("equipmentType")
        if not name:
            errors["name"] = _("Name is required.")
        elif not isinstance(name, str) or not 2 <= len(name) <= 80:
            errors["name"] = _("Name must be 2 to 80 characters.")
        if not phone:
            errors["phone"] = _("Phone is required.")
        elif not isinstance(phone, str) or not PHONE_RE.match(phone):
            errors["phone"] = _("Enter the phone in international format, e.g. +201001234567.")
        if body.get("intent") not in INTENTS:
            errors["intent"] = _("Choose sell or rent.")
        if not kind:
            errors["equipmentType"] = _("Required.")
        elif not isinstance(kind, str) or not 2 <= len(kind) <= 120:
            errors["equipmentType"] = _("Equipment must be 2 to 120 characters.")
        if body.get("condition") not in CONDITIONS:
            errors["condition"] = _("Choose new, used or refurbished.")
        quantity = body.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            errors["quantity"] = _("Quantity must be a whole number, 1 or more.")
        description = text("description")
        if description not in (None, "") and (not isinstance(description, str) or len(description) > 1000):
            errors["description"] = _("The description can be at most 1000 characters.")
        if errors:
            raise validation_error(errors, _("Validation failed."))
        return name, phone, kind, description or False

    @http.route("/equipment-listings", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def equipment_listing(self, cors, **kw):
        rate_limit("sf_listing", limit=5, window=600, raisable=False)
        body = json_body()
        name, phone, kind, description = self._check_listing(body)
        user = None
        try:
            user = bearer_user(required=False)
        except AuthApiError:
            pass  # an invalid Bearer counts as a guest on this endpoint
        locale = body.get("locale") if body.get("locale") in ("ar", "en") else (
            "ar" if (request.env.lang or "").startswith("ar") else "en")
        listing = request.env["leaders.equipment.listing"].sudo().create({
            "contact_name": name, "phone": phone, "intent": body["intent"], "equipment_type": kind,
            "condition": body["condition"], "quantity": body["quantity"], "description": description,
            "locale": locale, "user_id": user.id if user else False,
            "request_ip": request.httprequest.remote_addr or False,
        })
        listing.notify_sales()
        return json_response({"reference": listing.name}, 201, cors)

    # ------------------------------------------------------------ site settings
    @http.route("/site-settings", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def site_settings(self, cors, **kw):
        rate_limit("sf_catalog")
        settings = request.env["leaders.site.settings"].get_settings().with_context(lang=request.env.lang)
        company = request.env.company.sudo()
        address = ", ".join(part for part in (company.street, company.street2, company.city,
                                              company.state_id.name, company.country_id.name) if part)
        out = {
            "phone": settings.phone or company.phone or "",
            "whatsapp": settings.whatsapp or settings.phone or company.phone or "",
            "email": settings.email or company.email or "",
            "address": settings.address or address,
            "legalName": settings.legal_name or company.name or "",
        }
        for key, value in (("commercialRegister", settings.commercial_register or company.company_registry),
                           ("appStoreUrl", settings.app_store_url), ("googlePlayUrl", settings.google_play_url)):
            if value:
                out[key] = value
        return json_response(out, 200, cors)

    # ----------------------------------------------------------------- partners
    @http.route("/partners", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def partners(self, cors, **kw):
        """The partner and client logos of the home page strip, in the admin's order; ``[]`` when there are none."""
        rate_limit("sf_catalog")
        base = _base_url()
        out = [{"id": str(p.id), "name": p.name,
                "logoUrl": "%s/partners/%s/logo?unique=%s" % (base, p.id, _stamp(p)),
                "website": p.website or None}
               for p in request.env["leaders.site.partner"].sudo().search([])]
        return json_response(out, 200, cors)

    @http.route("/partners/<int:partner_id>/logo", type="http", auth="public", methods=["GET"], csrf=False,
                save_session=False)
    def partner_logo(self, partner_id, **kw):
        """The logo to anyone (no cookie), so the storefront can show it on any origin."""
        return _image(request.env["leaders.site.partner"].sudo().browse(partner_id).exists(), "logo")

    # -------------------------------------------------------------------- pages
    @http.route("/pages/<string:slug>", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def page(self, cors, slug, **kw):
        rate_limit("sf_catalog")
        page = request.env["leaders.site.page"].sudo().search([("slug", "=", slug), ("published", "=", True)], limit=1)
        if not page:
            raise AuthApiError(404, "not_found", _("Page not found."))
        return json_response({
            "slug": page.slug, "title": page.title, "content": str(page.content or ""),
            "updatedAt": page.write_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, 200, cors)

    # ------------------------------------------------------------- company film
    # @http.route("/company-story", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    # @storefront_api(["GET"])
    # def company_story(self, cors, **kw):
    #     rate_limit("sf_catalog")
    #     settings = request.env["leaders.site.settings"].get_settings()
    #     out = {}
    #     video = video_asset(settings.story_video_kind, settings.story_video_url)
    #     if video:
    #         out["video"] = video
    #     if settings.story_poster:
    #         out["poster"] = "%s/company-story/poster?unique=%s" % (_base_url(), _stamp(settings))
    #     return json_response(out, 200, cors)

    # @http.route("/company-story/poster", type="http", auth="public", methods=["GET"], csrf=False, save_session=False)
    # def company_story_poster(self, **kw):
    #     return _image(request.env["leaders.site.settings"].get_settings(), "story_poster")

    # ------------------------------------------------------------- testimonials
    # @http.route("/testimonials", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    # @storefront_api(["GET"])
    # def testimonials(self, cors, **kw):
    #     rate_limit("sf_catalog")
    #     raw = request.httprequest.args.get("limit", "4")
    #     limit = min(max(int(raw), 1), 12) if raw.isdigit() else 4
    #     rows = request.env["leaders.testimonial"].sudo().search(
    #         [("published", "=", True), ("permission", "=", True)], limit=limit)
    #     out = []
    #     for row in rows:
    #         item = {"id": str(row.id), "name": row.name, "role": row.role, "quote": row.quote,
    #                 "image": "%s/testimonials/%s/image?unique=%s" % (_base_url(), row.id, _stamp(row))}
    #         if row.company:
    #             item["company"] = row.company
    #         video = video_asset(row.video_kind, row.video_url)
    #         if video:
    #             item["video"] = video
    #         out.append(item)
    #     return json_response(out, 200, cors)

    # @http.route("/testimonials/<int:testimonial_id>/image", type="http", auth="public", methods=["GET"], csrf=False, save_session=False)
    # def testimonial_image(self, testimonial_id, **kw):
    #     row = request.env["leaders.testimonial"].sudo().search(
    #         [("id", "=", testimonial_id), ("published", "=", True), ("permission", "=", True)], limit=1)
    #     return _image(row, "image")
