"""Storefront quote and service-request endpoints."""
import json
import re
from datetime import date, datetime

import pytz

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from odoo import SUPERUSER_ID, _, fields, http
from odoo.http import request
from odoo.tools import html2plaintext

from ..models.service_page import EQUIPMENT_TYPES
from ..services.auth_service import EMAIL_RE, PHONE_RE
from ..services.storefront import Storefront, minor
from ..tools.auth_errors import AuthApiError, json_response, validation_error
from .storefront_common import bearer_user, json_body, rate_limit, storefront_api
from .storefront_equipment import StorefrontEquipment
from .storefront_orders import _gov_key

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PAYMENT_METHODS = ("cash", "card", "paypal", "applepay")
E164_PARAM = "leaders_marketplace.storefront_service_phone_e164"
# optional serviceDetails text fields and their longest length
DETAIL_TEXTS = (("company", 120), ("brandModel", 120), ("address", 300))


class StorefrontQuotes(http.Controller):

    def _check_quote_request(self, body, name, phone, items):
        """A quote request of products: the rules it always had, now in the request's language."""
        errors = {}
        if len(name.strip()) < 2:
            errors["name"] = _("Name is required.")
        if not phone.strip():
            errors["phone"] = _("Phone is required.")
        if not items:
            errors["items"] = _("At least one item or service is required.")
        if errors:
            raise validation_error(errors, _("Validation failed."))
        details = body.get("serviceDetails")
        if isinstance(details, dict):
            for key in ("neededBy", "equipmentType"):
                if not isinstance(details.get(key), str) or not details[key].strip():
                    errors["serviceDetails.%s" % key] = _("Required.")
        if errors:
            raise validation_error(errors, _("Validation failed."))
        return details

    def _check_service_request(self, body):
        """A service request: every field is checked and every problem is reported in one answer.

        An empty string is an unset field. Returns the cleaned ``serviceDetails`` and the service page.
        """
        errors = {}

        def text(value):
            return value.strip() if isinstance(value, str) else value

        name, phone, notes = text(body.get("name")), text(body.get("phone")), text(body.get("notes"))
        if not name:
            errors["name"] = _("Name is required.")
        elif not isinstance(name, str) or not 2 <= len(name) <= 80:
            errors["name"] = _("Name must be 2 to 80 characters.")
        if not phone:
            errors["phone"] = _("Phone is required.")
        elif not isinstance(phone, str):
            errors["phone"] = _("Enter a valid phone number.")
        elif request.env["ir.config_parameter"].sudo().get_param(E164_PARAM) == "1" and not PHONE_RE.match(phone):
            errors["phone"] = _("Enter the phone in international format, e.g. +201001234567.")
        if notes not in (None, "") and (not isinstance(notes, str) or len(notes) > 1000):
            errors["notes"] = _("Notes can be at most 1000 characters.")

        store = Storefront(request.env)
        slug = body.get("service")
        service = store.Template.search(store.service_domain() + [("leaders_slug", "=", slug)], limit=1) \
            if isinstance(slug, str) else store.Template.browse()
        if not service:
            published = store.Template.search(store.service_domain(), order="sequence, id").mapped("leaders_slug")
            errors["service"] = _("Unknown service. One of: %s.", ", ".join(published))

        raw = body.get("serviceDetails")
        details = {}
        if not isinstance(raw, dict):
            errors["serviceDetails"] = _("Required for a service request.")
        else:
            details = {key: text(value) for key, value in raw.items() if text(value) not in (None, "")}
            needed = details.get("neededBy")
            if not needed:
                errors["serviceDetails.neededBy"] = _("Required.")
            else:
                day = None
                if isinstance(needed, str) and DATE_RE.match(needed):
                    try:
                        day = date.fromisoformat(needed)
                    except ValueError:
                        pass
                if not day:
                    errors["serviceDetails.neededBy"] = _("Enter a date as yyyy-mm-dd.")
                elif day < datetime.now(pytz.timezone("Africa/Cairo")).date():
                    errors["serviceDetails.neededBy"] = _("The date must be today or later.")
            equipment = details.get("equipmentType")
            if not equipment:
                errors["serviceDetails.equipmentType"] = _("Required.")
            elif equipment not in EQUIPMENT_TYPES:
                errors["serviceDetails.equipmentType"] = _("Unknown equipment type. One of: %s.",
                                                           ", ".join(EQUIPMENT_TYPES))
            email = details.get("email")
            if email is not None and (not isinstance(email, str) or not EMAIL_RE.match(email)):
                errors["serviceDetails.email"] = _("Enter a valid email address.")
            governorate = details.get("governorate")
            if governorate is not None:
                zones = request.env["leaders.vendor.delivery.zone"].sudo().search(
                    [("is_platform", "=", True), ("active", "=", True)])
                if governorate not in {_gov_key(zone.state_id) for zone in zones}:
                    errors["serviceDetails.governorate"] = _("Choose a governorate from the list.")
            for key, longest in DETAIL_TEXTS:
                value = details.get(key)
                if value is not None and (not isinstance(value, str) or len(value) > longest):
                    errors["serviceDetails.%s" % key] = _("At most %s characters.", longest)
        if errors:
            raise validation_error(errors, _("Validation failed."))
        return details, service

    @http.route("/quotes", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def create(self, cors, **kw):
        rate_limit("sf_quotes")
        body = json_body()
        name = body.get("name") if isinstance(body.get("name"), str) else ""
        phone = body.get("phone") if isinstance(body.get("phone"), str) else ""
        items = body.get("items") if isinstance(body.get("items"), list) else []
        service = body.get("service") if isinstance(body.get("service"), str) else ""
        if service:
            details, service_tmpl = self._check_service_request(body)
            service = service_tmpl.leaders_slug
        else:
            details, service_tmpl = self._check_quote_request(body, name, phone, items), None
        provider = self._service_provider(body, service_tmpl)
        needed_by = self._needed_by(body)
        if not needed_by and service and isinstance(details, dict) and details.get("neededBy"):
            needed_by = date.fromisoformat(details["neededBy"])  # a service form sends it inside serviceDetails
        user = None
        try:
            user = bearer_user(required=False)
        except AuthApiError:
            pass  # an invalid Bearer is ignored on this endpoint
        partner = user.partner_id.commercial_partner_id if user else request.env["res.partner"]._leaders_guest_contact(
            name, phone, (details or {}).get("email") if isinstance(details, dict) else None)
        lines, first = [], None
        for index, item in enumerate(items):
            item = item if isinstance(item, dict) else {}
            pid = item.get("productId")
            template = request.env["product.template"].sudo().search([("id", "=", int(pid))], limit=1) \
                if isinstance(pid, (int, str)) and str(pid).isdigit() else None
            if not template:
                raise validation_error({"items.%s.productId" % index: _("Unknown product.")}, _("Validation failed."))
            quantity = item.get("quantity", 1)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 999:
                raise validation_error({"items.%s.quantity" % index: _("Quantity must be between 1 and 999.")},
                                       _("Validation failed."))
            options = item.get("options") if isinstance(item.get("options"), dict) else {}
            first = first or template
            lines.append((0, 0, {"product_tmpl_id": template.id, "quantity": quantity, "options": json.dumps(options)}))
        record = request.env["leaders.customer.request"].sudo().create({
            "kind": "service" if service else "quote", "partner_id": partner.id,
            "phone": phone.strip(), "product_tmpl_id": first.id if first else False,
            "title": service or (first.display_name if first else "Storefront quote request"),
            "quantity": lines[0][2]["quantity"] if lines else 1,
            "note": ((body.get("notes") if isinstance(body.get("notes"), str) else "") + ("\nCity: %s" % body["city"] if body.get("city") else "")).strip() or False,
            "service": service or False, "service_id": service_tmpl.id if service_tmpl else False,
            "service_provider_id": provider.id if provider else False,
            "service_details": json.dumps(details) if isinstance(details, dict) else False,
            "line_ids": lines, "needed_by": needed_by,
            # what the form carried besides the request itself: who typed it, how to write back, in which language
            "contact_name": name.strip()[:120] or False,
            "contact_email": ((details or {}).get("email") if isinstance(details, dict) else False) or False,
            "request_locale": body.get("locale") if body.get("locale") in ("ar", "en") else False,
        })
        record._leaders_link_included_maintenance()
        record.notify_service_responsible()
        record.send_confirmation(self._email_lang(body))
        if user and lines:  # quote tab of the cart: those lines are now requested
            cart = request.env["leaders.cart"].sudo().search([("user_id", "=", user.id)], limit=1)
            ids = [line[2]["product_tmpl_id"] for line in lines]
            cart.line_ids.filtered(lambda l: l.product_tmpl_id.id in ids).unlink() if cart else None
        return json_response({"reference": record.name}, 201, cors)

    @staticmethod
    def _service_provider(body, service_tmpl):
        """The optional ``serviceProvider`` of a service request: the ``id`` of one of the providers listed for the
        service (``"platform"`` or a vendor's id). Missing: the platform assigns the request, as before."""
        raw = body.get("serviceProvider")
        if raw in (None, ""):
            return request.env["leaders.service.provider"]
        if not service_tmpl:
            raise AuthApiError(400, "validation_error", _("Validation failed."),
                               fields={"serviceProvider": _("Only a service request takes a provider.")})
        Provider = request.env["leaders.service.provider"].sudo()
        listed = Provider._leaders_public(service_tmpl)
        text = str(raw).strip()
        provider = listed.filtered(lambda p: p.is_platform) if text == "platform" else (
            listed.filtered(lambda p: str(p.id) == text) if text.isdigit() else Provider.browse())
        if not provider:
            raise AuthApiError(400, "validation_error", _("Validation failed."),
                               fields={"serviceProvider": _("Choose one of the providers of this service.")})
        return provider[:1]

    @staticmethod
    def _needed_by(body):
        """Optional top-level ``neededBy`` (YYYY-MM-DD, today or later in Cairo)."""
        value = body.get("neededBy")
        if value in (None, ""):
            return False
        day = None
        if isinstance(value, str) and DATE_RE.match(value):
            try:
                day = date.fromisoformat(value)
            except ValueError:
                day = None
        if not day:
            raise validation_error({"neededBy": _("Enter a date as yyyy-mm-dd.")}, _("Validation failed."))
        if day < datetime.now(pytz.timezone("Africa/Cairo")).date():
            raise validation_error({"neededBy": _("The date must be today or later.")}, _("Validation failed."))
        return day

    @staticmethod
    def _email_lang(body):
        """``locale`` of the page (``ar``/``en``), else the request's language; Arabic only when installed."""
        locale = body.get("locale") if body.get("locale") in ("ar", "en") else (
            "ar" if (request.env.lang or "").startswith("ar") else "en")
        if locale == "ar":
            lang = request.env["res.lang"].sudo().search([("code", "like", "ar%"), ("active", "=", True)], limit=1)
            if lang:
                return lang.code
        return "en_US"

    def _find_quote(self, reference):
        user = bearer_user(required=True)
        record = request.env["leaders.customer.request"].sudo().search([
            ("name", "=", reference), ("kind", "in", ("quote", "service")),
            ("partner_id", "=", user.partner_id.commercial_partner_id.id)], limit=1)
        if not record:
            raise AuthApiError(404, "not_found", _("Quote not found."))
        return record

    def _render(self, record):
        items = []
        for line in record.line_ids:
            template = line.product_tmpl_id
            row = {"productId": str(template.id), "slug": template.leaders_slug or str(template.id),
                   "quantity": line.quantity, "options": json.loads(line.options or "{}"), "name": template.display_name}
            if template.image_1920:
                row["image"] = "%s/web/image/product.template/%s/image_1024" % (
                    request.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/"), template.id)
            items.append(row)
        history = json.loads(record.history or "{}")
        stage = record.stage_of(record.state) if record.state != "cancelled" else max(
            history, key=lambda k: history[k], default="sent")
        result = {"reference": record.name, "placedAt": record.create_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "stage": stage, "history": history, "cancelled": record.state == "cancelled",
                  "phone": record.phone or "", "items": items}
        if record.service:
            result["service"] = record.service
            service_name = record.service_name()
            if service_name:
                result["serviceName"] = service_name
        if record.service_details:
            result["serviceDetails"] = json.loads(record.service_details)
        if record.included_rental_id:  # optional: free, part of the rental contract
            result["includedInRental"] = record.included_rental_id.name
        if record.needed_by:
            result["neededBy"] = str(record.needed_by)
        if record.leaders_offer_open():
            result["offer"] = self._offer(record)
        return result

    @staticmethod
    def _offer(record):
        """The quotation of the request as the contract's ``offer`` (money in minor units)."""
        order = record.quote_order_id.sudo()

        def row(line):
            out = {"id": "l%s" % line.id, "label": line.name or line.product_id.display_name,
                   "amount": minor(line.price_total)}
            if line.leaders_offer_note:
                out["note"] = line.leaders_offer_note
            if line.leaders_optional:
                out["optional"] = True
            return out
        lines = order._leaders_offer_lines()
        due = lines.filtered("leaders_due_now")
        total, due_total = minor(order.amount_total), sum(minor(l.price_total) for l in due)
        offer = {"currency": order.currency_id.name, "lines": [row(l) for l in lines], "total": total,
                 "terms": [t.strip(" -•\t") for t in html2plaintext(order.note or "").splitlines() if t.strip(" -•\t")],
                 "dueNow": [row(l) for l in due], "dueNowTotal": due_total, "remaining": total - due_total}
        if order.leaders_due_now_note:
            offer["dueNowNote"] = order.leaders_due_now_note
        if record.offer_accepted_at:
            offer["acceptedAt"] = record.offer_accepted_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if record.offer_paid_at:
            offer["paidAt"] = record.offer_paid_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if record.sf_payment_method in ("cash", "card", "paypal", "applepay"):
            offer["paymentMethod"] = record.sf_payment_method
        return offer

    @http.route("/quotes/<string:reference>", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def detail(self, cors, reference, **kw):
        rate_limit("sf_quotes")
        return json_response(self._render(self._find_quote(reference)), 200, cors)

    @http.route("/quotes/<string:reference>/cancel", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def cancel(self, cors, reference, **kw):
        rate_limit("sf_quotes")
        record = self._find_quote(reference)
        if record.state in ("cancelled", "completed", "executing") or record.offer_paid_at:
            raise AuthApiError(409, "not_cancellable", _("This quote can no longer be cancelled."))
        # as the superuser: the request runs as the public user, which may not read the quotation's lines
        record = record.with_user(SUPERUSER_ID).sudo()
        order = record.quote_order_id
        if order and order.state in ("draft", "sent"):
            order._action_cancel()
        record.state = "cancelled"
        record.env.flush_all()  # recompute now, as the superuser, not at the end of the public request
        return json_response(self._render(record), 200, cors)

    @http.route("/quotes/<string:reference>/accept", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def accept(self, cors, reference, **kw):
        """The customer accepts the offer's terms, without the optional lines they removed."""
        rate_limit("sf_quotes")
        record = self._find_quote(reference)
        body = json_body()
        if body.get("acceptTerms") is not True:
            raise validation_error({"acceptTerms": _("Accept the terms of the offer.")}, _("Validation failed."))
        removed = body.get("removedLines", [])
        if not isinstance(removed, list) or not all(isinstance(x, str) for x in removed):
            raise validation_error({"removedLines": _("A list of line ids.")}, _("Validation failed."))
        if record.state != "offered" or not record.leaders_offer_open():
            raise AuthApiError(409, "invalid_stage", _("The offer cannot be accepted at this stage."))
        record = record.with_user(SUPERUSER_ID).sudo()
        order = record.quote_order_id
        lines = order._leaders_offer_lines()
        drop = lines.filtered(lambda l: l.leaders_optional and "l%s" % l.id in removed)  # other ids are ignored
        if drop and drop == lines:
            raise validation_error({"removedLines": _("The offer cannot be left empty.")}, _("Validation failed."))
        drop.unlink()
        lines = order._leaders_offer_lines()
        due = sum(lines.filtered("leaders_due_now").mapped("price_total"))
        remaining = order.amount_total - due
        order.write({"leaders_payment_method": "cod" if remaining > 0.005 else "online"})
        record.write({"offer_accepted_at": fields.Datetime.now(), "state": "confirming"})
        if due <= 0.005:  # nothing to pay online: the work starts now
            order.action_confirm()
            if record.state == "confirming":
                record.state = "executing"
        else:
            ratio = min(round(due / order.amount_total, 6), 1.0) if order.amount_total else 1.0
            order.write({"require_payment": True, "prepayment_percent": ratio})
            if order.state == "draft":
                order.state = "sent"
        record.env.flush_all()
        return json_response(self._render(record), 200, cors)

    @http.route("/quotes/<string:reference>/payment", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def payment(self, cors, reference, **kw):
        """A hosted payment session for what is due now: the browser goes to the gateway's page.

        ``method: "cash"`` pays on delivery instead: the offer is confirmed at once (stage ``executing``) and the
        platform collects what is due now with the rest; ``paidAt`` stays unset."""
        rate_limit("sf_quotes")
        record = self._find_quote(reference)
        body = json_body()
        errors = {}
        method = body.get("method", "card")
        if method not in PAYMENT_METHODS:
            errors["method"] = _("One of: %s.", ", ".join(PAYMENT_METHODS))
        cash = method == "cash"
        return_url = False if cash else StorefrontEquipment._return_url(body, errors)
        if not cash and not return_url and "returnUrl" not in errors:
            errors["returnUrl"] = _("Required.")
        if errors:
            raise validation_error(errors, _("Validation failed."))
        if record.offer_paid_at:
            raise AuthApiError(409, "already_paid", _("What was due now is already paid."))
        order = record.quote_order_id.sudo()
        due = sum(order._leaders_offer_lines().filtered("leaders_due_now").mapped("price_total")) if order else 0
        if record.state != "confirming" or not record.offer_accepted_at or due <= 0.005 or order.state not in ("draft", "sent"):
            raise AuthApiError(409, "invalid_stage", _("There is nothing to pay at this stage."))
        if cash:
            # as the superuser: the request runs as the public user, which may not confirm the quotation
            record = record.with_user(SUPERUSER_ID).sudo()
            order = record.quote_order_id
            order.write({"leaders_payment_method": "cod", "require_payment": False})
            record.sf_payment_method = "cash"
            order.action_confirm()  # moves the request to executing
            if record.state == "confirming":
                record.state = "executing"
            record.env.flush_all()
            return json_response({}, 200, cors)
        record.sudo().write({"return_url": return_url, "sf_payment_method": method})
        base = request.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        locale = "ar" if (request.env.lang or "").startswith("ar") else "en"
        return json_response({"redirectUrl": "%s/orders/%s/payment?locale=%s" % (base, order.leaders_public_ref_get(), locale)},
                             200, cors)

    @http.route("/quotes/<string:reference>/payment/return", type="http", auth="public", methods=["GET"], csrf=False, save_session=False)
    def payment_return(self, reference, **kw):
        """Gateway return: back to the ``returnUrl`` with ``?payment=success`` or ``?payment=failed``."""
        record = request.env["leaders.customer.request"].sudo().search([("name", "=", reference)], limit=1)
        if not record or not record.return_url:
            return request.redirect("/", local=True)
        order = record.quote_order_id
        paid = bool(order) and any(t.state in ("done", "authorized") for t in order.transaction_ids)
        url = record.return_url.replace("{reference}", record.name).replace("%7Breference%7D", record.name)
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "payment"] + [("payment", "success" if paid else "failed")]
        return request.redirect(urlunsplit(parts._replace(query=urlencode(query))), local=False)

    @http.route("/quotes/<string:reference>/received", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def received(self, cors, reference, **kw):
        """The customer confirms receipt ("تم استلام الطلب"): the request is completed."""
        rate_limit("sf_quotes")
        record = self._find_quote(reference)
        if record.state != "executing":
            raise AuthApiError(409, "invalid_stage", _("The request is not being carried out."))
        record = record.with_user(SUPERUSER_ID).sudo()
        record.state = "completed"
        record.env.flush_all()
        return json_response(self._render(record), 200, cors)
