"""Storefront contract: the "Sell with us" button of a signed-in customer.

``POST /vendor-apply/link`` answers where the customer goes (the application form, the
status of their application or the vendor portal) as a one-time link that signs the same
account into the website, so the customer never signs in twice.
"""
from odoo import http
from odoo.http import request

from ..tools.auth_errors import AuthApiError, json_response
from .storefront_common import bearer_user, rate_limit, storefront_api

TARGETS = {"none": "/vendor/apply", "approved": "/my/vendor"}


class StorefrontVendorApply(http.Controller):

    @staticmethod
    def _lang_prefix():
        """``/ar`` when the client asks for Arabic (``Accept-Language``) and the website offers it, else ``""``:
        the portal pages then open in the storefront's language."""
        lang = request.env.lang or ""
        if not lang.startswith("ar"):
            return ""
        website = request.env["website"].sudo().search([("language_ids.code", "=", lang)], limit=1)
        if not website or website.default_lang_id.code == lang:
            return ""
        return "/" + request.env["res.lang"].sudo()._get_data(code=lang).url_code

    @http.route("/vendor-apply/link", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False,
                save_session=False)
    @storefront_api(["POST"])
    def vendor_apply_link(self, cors, **kw):
        rate_limit("sf_vendor_apply", limit=10, window=60, raisable=False)
        user = bearer_user(required=False)
        base = request.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        prefix = self._lang_prefix()
        if not user:
            # not signed in: the form itself, which first confirms the visitor's email with a code
            if request.env["ir.config_parameter"].sudo().get_param(
                    "leaders_marketplace.vendor_apply_enabled", "") not in ("True", "true", "1"):
                raise AuthApiError(403, "applications_closed", "Vendor applications are closed at the moment.")
            return json_response({"status": "none", "url": "%s%s/vendor/apply" % (base, prefix)}, 200, cors)
        Application = request.env["leaders.vendor.application"].sudo()
        problem = Application._applicant_problem(user)
        status, application = Application._applicant_status(user)
        if problem and status != "approved":
            raise AuthApiError(403, "account_not_allowed", problem)
        enabled = request.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.vendor_apply_enabled", "") in ("True", "true", "1")
        if not enabled and status != "approved":
            raise AuthApiError(403, "applications_closed", "Vendor applications are closed at the moment.")
        if status in TARGETS:
            target = TARGETS[status]
        else:
            application._portal_ensure_token()
            target = "/vendor/apply/status/%s" % application.access_token
        ticket = request.env["leaders.vendor.apply.ticket"].sudo().issue(user, prefix + target)
        out = {"status": status, "url": "%s%s/vendor/apply/start?ticket=%s" % (base, prefix, ticket)}
        if application:
            out["reference"] = application.name
        return json_response(out, 200, cors)
