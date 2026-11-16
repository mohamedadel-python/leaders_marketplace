"""Storefront contact form endpoint."""
import json
from odoo import http
from odoo.http import request

from ..tools.auth_errors import AuthApiError, json_response, validation_error
from .storefront_common import rate_limit, storefront_api


class StorefrontContact(http.Controller):

    @http.route("/contact", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def submit(self, cors, **kw):
        rate_limit("sf_contact")
        req = request.httprequest
        if req.mimetype == "application/json":
            try:
                body = json.loads(req.get_data(as_text=True) or "{}")
            except (TypeError, ValueError):
                raise AuthApiError(400, "invalid_request", "The request body is not valid JSON.")
        else:
            body = req.form.to_dict()
        errors = {}
        for key in ("name", "subject", "message"):
            if not isinstance(body.get(key), str) or not body[key].strip():
                errors[key] = "Required."
        email = body.get("email") or ""
        phone = body.get("phone") or ""
        if not email and not phone:
            errors["email"] = "Email or phone is required."
        if errors:
            raise validation_error(errors)
        record = request.env["leaders.contact.request"].sudo().create({
            "sender_name": body["name"].strip(), "email": email.strip() or False,
            "phone": phone.strip() or False, "subject": body["subject"].strip(),
            "message": body["message"].strip(),
        })
        files = list(req.files.values()) if req.files else []
        for uploaded in files[:3]:
            data = uploaded.read()
            if len(data) > 5 * 1024 * 1024:
                raise validation_error({"attachments": "Each attachment must be at most 5 MB."})
            request.env["ir.attachment"].sudo().create({
                "name": uploaded.filename, "res_model": record._name, "res_id": record.id,
                "mimetype": uploaded.mimetype, "datas": __import__("base64").b64encode(data),
            })
        return json_response({"reference": record.name}, 201, cors)
