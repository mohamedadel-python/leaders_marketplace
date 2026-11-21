"""POST /item-requests: the "request the product" form of an empty catalog result (guest or signed in)."""
from odoo import _, http
from odoo.http import request

from ..models.item_request import ITEM_TYPES
from ..services.auth_service import EMAIL_RE, PHONE_RE
from ..tools.auth_errors import AuthApiError, json_response, validation_error
from .storefront_common import bearer_user, json_body, rate_limit, storefront_api

# optional text fields and their longest length
TEXTS = (("brand", 120), ("model", 120), ("address", 300))


class StorefrontItemRequests(http.Controller):

    @staticmethod
    def _clean(body):
        """The checked form, every problem reported at once (422 ``validation_error`` with ``fields``)."""
        errors = {}

        def text(key):
            value = body.get(key)
            return value.strip() if isinstance(value, str) else value

        name, phone, product = text("name"), text("phone"), text("productName")
        if not name:
            errors["name"] = _("Name is required.")
        elif not isinstance(name, str) or not 2 <= len(name) <= 80:
            errors["name"] = _("Name must be 2 to 80 characters.")
        if not phone:
            errors["phone"] = _("Phone is required.")
        elif not isinstance(phone, str) or not PHONE_RE.match(phone):
            errors["phone"] = _("Enter the phone in international format, e.g. +201001234567.")
        email = text("email")
        if email not in (None, "") and (not isinstance(email, str) or not EMAIL_RE.match(email)):
            errors["email"] = _("Enter a valid email address.")
        if not product:
            errors["productName"] = _("The product name is required.")
        elif not isinstance(product, str) or len(product) > 200:
            errors["productName"] = _("The product name can be at most 200 characters.")
        kind = body.get("productType")
        if kind not in dict(ITEM_TYPES):
            errors["productType"] = _("Choose the product type: power, equipment or part.")
        quantity = body.get("quantity")
        if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, int) or not 0 <= quantity <= 100000):
            errors["quantity"] = _("The quantity must be a whole number, 0 or more.")
        notes = text("notes")
        if notes not in (None, "") and (not isinstance(notes, str) or len(notes) > 1000):
            errors["notes"] = _("Notes can be at most 1000 characters.")
        extra = {}
        for key, longest in TEXTS:
            value = text(key)
            if value in (None, ""):
                continue
            if not isinstance(value, str) or len(value) > longest:
                errors[key] = _("At most %s characters.", longest)
            else:
                extra[key] = value
        locale = body.get("locale")
        if locale not in ("ar", "en"):
            errors["locale"] = _("Must be ar or en.")
        if errors:
            raise validation_error(errors, _("Validation failed."))
        return {"name": name, "phone": phone, "email": email or False, "product": product, "kind": kind,
                "quantity": quantity, "notes": notes or False, "locale": locale, **extra}

    @http.route("/item-requests", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def create(self, cors, **kw):
        rate_limit("sf_item_requests", limit=10, raisable=False)
        form = self._clean(json_body())
        user = None
        try:
            user = bearer_user(required=False)
        except AuthApiError:
            pass  # an invalid Bearer is ignored: the request is kept as a guest's
        partner = user.partner_id.commercial_partner_id if user else request.env["res.partner"]._leaders_guest_contact(
            form["name"], form["phone"], form["email"])
        brand = request.env["leaders.brand"].sudo().search([("name", "=ilike", form["brand"])], limit=1) \
            if form.get("brand") else request.env["leaders.brand"]
        record = request.env["leaders.customer.request"].sudo().create({
            "kind": "item", "partner_id": partner.id, "title": form["product"],
            "contact_name": form["name"], "phone": form["phone"], "contact_email": form["email"],
            "item_type": form["kind"], "item_brand": form.get("brand") or False, "item_brand_id": brand.id or False,
            "item_model": form.get("model") or False, "shipping_address": form.get("address") or False,
            "quantity": form["quantity"] if form["quantity"] is not None else 1, "note": form["notes"],
            "request_locale": form["locale"],
        })
        record.notify_item_sales()
        if form["email"]:
            record.send_item_confirmation()
        return json_response({"reference": record.name}, 201, cors)

    @http.route("/brands/<int:brand_id>/logo", type="http", auth="public", methods=["GET"], csrf=False, save_session=False)
    def brand_logo(self, brand_id, **kw):
        """The logo of an active brand, public (the brand filter shows it)."""
        brand = request.env["leaders.brand"].sudo().search([("id", "=", brand_id), ("logo", "!=", False)], limit=1)
        if not brand:
            return request.not_found()
        stream = request.env["ir.binary"]._get_image_stream_from(brand, "logo")
        return stream.get_response(max_age=86400)
