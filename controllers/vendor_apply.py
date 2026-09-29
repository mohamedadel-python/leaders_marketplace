import base64
import os
from datetime import timedelta

from werkzeug.exceptions import NotFound

from odoo import fields, http, _
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools import email_normalize
from odoo.tools.mimetypes import guess_mimetype

from odoo.addons.leaders_marketplace.models.vendor_core import DOCUMENT_TYPES

TEXT_FIELDS = (
    "business_name", "store_name", "business_activity", "business_email", "business_phone",
    "street", "city", "legal_name", "commercial_registration_number", "tax_number",
    "owner_name", "owner_email", "owner_phone", "representative_name",
    "representative_position", "representative_id_number", "bank_name",
    "bank_account_holder", "bank_account_number",
)
OPEN_STATES = ("draft", "submitted", "under_review")


class VendorApplyController(http.Controller):

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def _param(key, default):
        return request.env["ir.config_parameter"].sudo().get_param(key, default)

    def _apply_enabled(self):
        return self._param("leaders_marketplace.vendor_apply_enabled", "") in ("True", "true", "1")

    def _int_param(self, key, default):
        try:
            return int(self._param(key, default))
        except (TypeError, ValueError):
            return default

    def _document_error(self, upload):
        """``(base64, error)`` for one uploaded KYC document (PDF or image)."""
        data = upload.read()
        if not data:
            return False, _("The file %s is empty.", upload.filename)
        max_mb = max(self._int_param("leaders_marketplace.vendor_document_max_mb", 5), 1)
        if len(data) > max_mb * 1024 * 1024:
            return False, _("The file %(name)s is too large (max %(size)s MB).", name=upload.filename, size=max_mb)
        mimetype = guess_mimetype(data, default="")
        if mimetype != "application/pdf" and not mimetype.startswith("image/"):
            return False, _("The file %s must be a PDF or an image.", upload.filename)
        return base64.b64encode(data), False

    def _form_values(self, values=None, errors=None):
        values = values or {}
        Application = request.env["leaders.vendor.application"]
        required = []
        if values.get("legal_type"):
            probe = Application.sudo().new({
                "legal_type": values["legal_type"], "tax_number": values.get("tax_number")})
            required = probe._required_document_types()
        return {
            "v": values,
            "errors": errors or [],
            "legal_types": Application._fields["legal_type"]._description_selection(request.env),
            "document_types": DOCUMENT_TYPES,
            "required_types": required,
            "categories": request.env["product.public.category"].sudo().search([]),
            "countries": request.env["res.country"].sudo().search([]),
            "terms": request.env["leaders.vendor.terms"].sudo().search([], order="version desc", limit=1),
            "selected_categories": [int(c) for c in (values.get("product_category_ids") or [])],
        }

    def _rate_limited(self, ip, email):
        limit = self._int_param("leaders_marketplace.vendor_apply_daily_limit", 3)
        if limit <= 0:
            return False
        since = fields.Datetime.now() - timedelta(days=1)
        Application = request.env["leaders.vendor.application"].sudo()
        domain = [("create_date", ">=", since)]
        by_ip = Application.search_count(domain + [("request_ip", "=", ip)]) if ip else 0
        by_email = Application.search_count(domain + [("owner_email", "=", email)]) if email else 0
        return max(by_ip, by_email) >= limit

    # -------------------------------------------------------------------------
    # Application form
    # -------------------------------------------------------------------------
    @http.route("/vendor/apply", type="http", auth="public", website=True, methods=["GET", "POST"], csrf=True, sitemap=False)
    def vendor_apply(self, **post):
        if not self._apply_enabled():
            raise NotFound()
        if request.httprequest.method == "GET":
            return request.render("leaders_marketplace.vendor_apply_form", self._form_values())

        form = request.httprequest.form
        if post.get("website_url"):
            # Honeypot: a human never sees this field. Look successful, create nothing.
            return request.render("leaders_marketplace.vendor_apply_form", self._form_values())

        values = {name: (post.get(name) or "").strip() for name in TEXT_FIELDS}
        values["legal_type"] = post.get("legal_type") or ""
        values["country_id"] = post.get("country_id") or ""
        values["product_category_ids"] = form.getlist("product_category_ids")
        values["terms_accepted"] = bool(post.get("terms_accepted"))
        values["seller_agreement_accepted"] = bool(post.get("seller_agreement_accepted"))

        Application = request.env["leaders.vendor.application"].sudo()
        errors = []
        if not values["business_name"]:
            errors.append(_("Business name is required."))
        if values["legal_type"] not in dict(Application._fields["legal_type"].selection):
            values["legal_type"] = False
        email = email_normalize(values["owner_email"])
        if values["owner_email"] and not email:
            errors.append(_("Please enter a valid owner email address."))
        if values["business_email"] and not email_normalize(values["business_email"]):
            errors.append(_("Please enter a valid business email address."))
        ip = request.httprequest.remote_addr
        if self._rate_limited(ip, email):
            errors.append(_("Too many applications from your address today. Please try again tomorrow."))

        documents = []
        for doc_type, label in DOCUMENT_TYPES:
            upload = request.httprequest.files.get("doc_%s" % doc_type)
            if not upload or not upload.filename:
                continue
            data, error = self._document_error(upload)
            if error:
                errors.append(error)
                continue
            documents.append((0, 0, {
                "name": label, "document_type": doc_type, "file": data,
                "file_name": os.path.basename(upload.filename)[:100],
            }))

        categories = request.env["product.public.category"].sudo().search([
            ("id", "in", [int(c) for c in values["product_category_ids"] if str(c).isdigit()])])
        country = request.env["res.country"].sudo().browse(
            int(values["country_id"]) if str(values["country_id"]).isdigit() else 0).exists()
        vals = {name: values[name] or False for name in TEXT_FIELDS}
        vals.update({
            "legal_type": values["legal_type"] or False,
            "country_id": country.id or False,
            "product_category_ids": [(6, 0, categories.ids)],
            "terms_accepted": values["terms_accepted"],
            "seller_agreement_accepted": values["seller_agreement_accepted"],
            "document_ids": documents,
        })
        vals["owner_email"] = email or values["owner_email"] or False

        # The model owns the rules: what is missing is asked of a throw-away
        # record, so a rejected attempt leaves nothing behind (not even a number).
        probe = Application.new(dict(vals, owner_email=email or values["owner_email"]))
        errors += probe._get_missing_requirements(check_verification=False)
        if not errors and email:
            try:
                probe._check_owner_email_available()
            except UserError as error:
                errors.append(error.args[0])
        if errors:
            return request.render("leaders_marketplace.vendor_apply_form", self._form_values(values, errors))

        try:
            with request.env.cr.savepoint():
                application = Application.create(dict(vals, request_ip=ip))
                application.action_submit()
        except UserError as error:
            return request.render(
                "leaders_marketplace.vendor_apply_form", self._form_values(values, [error.args[0]]))
        terms = request.env["leaders.vendor.terms"].sudo().search([], order="version desc", limit=1)
        application.message_post(body=_(
            "Submitted through the public form from %(ip)s. Terms accepted: %(terms)s.",
            ip=ip or "-", terms=("%s v%s" % (terms.name, terms.version)) if terms else _("none published")))
        application._notify_public_submission()
        return request.redirect("/vendor/apply/status/%s?submitted=1" % application.access_token)

    # -------------------------------------------------------------------------
    # Status page
    # -------------------------------------------------------------------------
    def _application_by_token(self, token):
        if not token:
            return request.env["leaders.vendor.application"]
        return request.env["leaders.vendor.application"].sudo().search([("access_token", "=", token)], limit=1)

    def _status_values(self, application, **extra):
        return dict({
            "application": application,
            "missing": [line for line in (application.missing_requirements or "").splitlines() if line],
            "can_reupload": application.state in OPEN_STATES,
            "state_label": dict(application._fields["state"]._description_selection(request.env)).get(application.state),
            "submitted": False,
            "error": False,
            "reuploaded": False,
        }, **extra)

    @http.route("/vendor/apply/status/<string:token>", type="http", auth="public", website=True, sitemap=False)
    def vendor_apply_status(self, token, **kw):
        if not self._apply_enabled():
            raise NotFound()
        application = self._application_by_token(token)
        if not application:
            raise NotFound()
        return request.render("leaders_marketplace.vendor_apply_status", self._status_values(
            application, submitted=bool(kw.get("submitted")), reuploaded=bool(kw.get("reuploaded")),
            error=kw.get("error")))

    @http.route("/vendor/apply/status/<string:token>/document/<int:document_id>", type="http", auth="public",
                website=True, methods=["POST"], csrf=True, sitemap=False)
    def vendor_apply_reupload(self, token, document_id, **post):
        if not self._apply_enabled():
            raise NotFound()
        application = self._application_by_token(token)
        document = application.document_ids.filtered(lambda d: d.id == document_id)
        if not application or not document:
            raise NotFound()
        url = "/vendor/apply/status/%s" % token
        if application.state not in OPEN_STATES or document.verification_status != "rejected":
            return request.render("leaders_marketplace.vendor_apply_status", self._status_values(
                application, error=_("This document cannot be replaced.")))
        upload = request.httprequest.files.get("file")
        if not upload or not upload.filename:
            return request.render("leaders_marketplace.vendor_apply_status", self._status_values(
                application, error=_("Please choose a file to upload.")))
        data, error = self._document_error(upload)
        if error:
            return request.render("leaders_marketplace.vendor_apply_status", self._status_values(
                application, error=error))
        # The model resets verification (and KYC) on any change of the file.
        document.write({"file": data, "file_name": os.path.basename(upload.filename)[:100]})
        return request.redirect(url + "?reuploaded=1")
