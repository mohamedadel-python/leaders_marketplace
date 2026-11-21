import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import quote

from werkzeug.exceptions import NotFound

from odoo import fields, http, _
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools import email_normalize
from odoo.tools.mimetypes import guess_mimetype

from odoo.addons.leaders_marketplace.models.vendor_core import DOCUMENT_TYPES

APPLY_URL = "/vendor/apply"
VENDOR_HOME = "/my/vendor"

# what the public form asks, in its three steps; the other application fields stay for the reviewer
TEXT_FIELDS = (
    "business_name", "owner_name", "owner_phone", "owner_email",
    "commercial_registration_number", "tax_number", "delivery_method", "business_activity",
)
TEXT_LIMITS = {"business_name": 120, "owner_name": 80, "commercial_registration_number": 64, "tax_number": 64,
               "delivery_method": 200, "business_activity": 2000}
FORM_DOCUMENTS = (("id_passport", True), ("commercial_register", False), ("tax_certificate", False))
FILE_TYPES = ("application/pdf", "image/jpeg", "image/png")
SHEET_EXTENSIONS = (".xlsx", ".csv")
# the step each input is on, so the form reopens where the first problem is
STEP_OF = {
    "business_name": 1, "owner_name": 1, "owner_phone": 1, "owner_email": 1, "business_type_id": 1,
    "state_id": 1, "doc_id_passport": 1,
    "commercial_registration_number": 2, "tax_number": 2, "doc_commercial_register": 2, "doc_tax_certificate": 2,
    "product_category_ids": 3, "delivery_method": 3, "business_activity": 3, "terms_accepted": 3,
    "data_confirmed": 3,
}
PHONE_RE = re.compile(r"^\+?[\d\s-]{8,20}$")
# BUG-00904: the Egyptian tax registration number is 9 digits; a commercial registration number 3 to 10 digits
TAX_NUMBER_RE = re.compile(r"^\d{9}$")
COMMERCIAL_REG_RE = re.compile(r"^\d{3,10}$")
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _digits(value):
    """The digits of a typed number: Arabic digits become Latin ones, spaces and dashes are dropped."""
    return re.sub(r"[\s-]", "", (value or "").translate(ARABIC_DIGITS))
# Egypt's 27 governorates (Helwan and 6th of October were merged back in 2011); res.country.state is not translated
GOVERNORATES_AR = {
    "C": "القاهرة", "GZ": "الجيزة", "ALX": "الإسكندرية", "KB": "القليوبية", "DK": "الدقهلية", "SHR": "الشرقية",
    "GH": "الغربية", "MNF": "المنوفية", "BH": "البحيرة", "KFS": "كفر الشيخ", "DT": "دمياط", "PTS": "بورسعيد",
    "IS": "الإسماعيلية", "SUZ": "السويس", "FYM": "الفيوم", "BNS": "بني سويف", "MN": "المنيا", "AST": "أسيوط",
    "SHG": "سوهاج", "KN": "قنا", "LX": "الأقصر", "ASN": "أسوان", "BA": "البحر الأحمر", "WAD": "الوادي الجديد",
    "MT": "مطروح", "SIN": "شمال سيناء", "JS": "جنوب سيناء", "SU": "6 أكتوبر", "HU": "حلوان",
}
OPEN_STATES = ("draft", "submitted", "under_review")
# email check of a visitor who is not signed in: the code lives in the session, hashed
EMAIL_CODE_SESSION = "lm_vendor_apply_email"
EMAIL_CODE_MINUTES = 15  # also written in the page and the email
EMAIL_CODE_TRIES = 5


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

    def _max_mb(self):
        return max(self._int_param("leaders_marketplace.vendor_document_max_mb", 5), 1)

    def _document_error(self, upload, types=None):
        """``(base64, error)`` for one uploaded KYC document: a PDF or an image, or one of ``types``."""
        data = upload.read()
        if not data:
            return False, _("The file %s is empty.", upload.filename)
        max_mb = self._max_mb()
        if len(data) > max_mb * 1024 * 1024:
            return False, _("The file %(name)s is too large (max %(size)s MB).", name=upload.filename, size=max_mb)
        mimetype = guess_mimetype(data, default="")
        if types:
            if mimetype not in types:
                return False, _("The file %s must be a PDF, JPG or PNG.", upload.filename)
        elif mimetype != "application/pdf" and not mimetype.startswith("image/"):
            return False, _("The file %s must be a PDF or an image.", upload.filename)
        return base64.b64encode(data), False

    @staticmethod
    def _governorates():
        """``[(state, label)]`` of Egypt's governorates, in Arabic on the Arabic site."""
        egypt = request.env.ref("base.eg")
        states = request.env["res.country.state"].sudo().search(
            [("country_id", "=", egypt.id), ("code", "in", list(GOVERNORATES_AR))])
        arabic = (request.env.lang or "").startswith("ar")
        rows = [(state, GOVERNORATES_AR[state.code] if arabic else state.name) for state in states]
        return sorted(rows, key=lambda row: row[1])

    @staticmethod
    def _category_groups():
        """``[(top, children)]``: the categories a vendor can sell in (no service or hidden ones)."""
        Category = request.env["product.public.category"].sudo()
        domain = [("leaders_is_service", "=", False), ("leaders_hidden", "=", False)]
        groups = []
        for top in Category.search(domain + [("parent_id", "=", False)], order="sequence, id"):
            groups.append((top, Category.search(domain + [("parent_id", "=", top.id)], order="sequence, id")))
        return groups

    def _form_values(self, values=None, errors=None, field_errors=None, previous=None, blocked=False):
        values = values or {}
        field_errors = field_errors or {}
        previous = previous or request.env["leaders.vendor.application"]
        step = min([STEP_OF.get(key, 3) for key in field_errors] or [3 if errors else 1])
        selected = [int(c) for c in (values.get("product_category_ids") or []) if str(c).isdigit()]
        return {
            "v": values,
            "errors": errors or [],
            "field_errors": field_errors,
            "step": step,
            "business_types": request.env["leaders.vendor.business.type"].sudo().search([]),
            "governorates": self._governorates(),
            "category_groups": self._category_groups(),
            "selected_categories": request.env["product.public.category"].sudo().browse(selected).exists(),
            "max_mb": self._max_mb(),
            "privacy_url": self._param("leaders_marketplace.vendor_apply_privacy_url", "/privacy"),
            "applicant_email": self._applicant_email(request.env.user),
            "previous": previous,
            "kept_documents": {doc_type: doc.file_name or doc.name
                               for doc_type, doc in self._kept_documents(previous).items()},
            "blocked": blocked,
        }

    # -------------------------------------------------------------------------
    # The applicant's account
    # -------------------------------------------------------------------------
    @staticmethod
    def _applicant_email(user):
        return email_normalize(user.email or "") or email_normalize(user.login or "") or ""

    @staticmethod
    def _kept_documents(previous):
        """``{type: document}`` of a rejected application that are carried over when filing again."""
        kept = {}
        for doc in previous.document_ids.sorted("id"):
            if doc.verification_status != "rejected" and doc.file:
                kept[doc.document_type] = doc
        return kept

    @staticmethod
    def _login_redirect(path=APPLY_URL):
        return request.redirect("/web/login?redirect=%s" % quote(path, safe=""))

    def _status_url(self, application):
        application.sudo()._portal_ensure_token()
        return "/vendor/apply/status/%s" % application.access_token

    def _route_applicant(self, user, again=False):
        """``(response, previous)``: where the account goes instead of a new form, or the form's base."""
        Application = request.env["leaders.vendor.application"]
        status, application = Application._applicant_status(user)
        if status == "approved":
            return request.redirect(VENDOR_HOME), None
        problem = Application._applicant_problem(user)
        if problem:
            return request.render("leaders_marketplace.vendor_apply_form", self._form_values(blocked=problem)), None
        if status == "pending" or (status == "rejected" and not again):
            return request.redirect(self._status_url(application)), None
        return None, application if status == "rejected" else Application

    @staticmethod
    def _values_from(previous, user):
        """Form values of a new application: the rejected one's data, else the account's."""
        if not previous:
            return {"owner_name": user.name or "", "owner_phone": user.partner_id.phone or user.partner_id.mobile or ""}
        values = {name: previous[name] or "" for name in TEXT_FIELDS}
        values.update({
            "business_type_id": str(previous.business_type_id.id or ""),
            "state_id": str(previous.state_id.id or ""),
            "product_category_ids": [str(c) for c in previous.product_category_ids.ids],
        })
        return values

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

    def _check(self, values):
        """``(field_errors, email)``: one message per wrong input, keyed by the input's name."""
        errors = {}
        required = _("This field is required.")
        for name in ("business_name", "owner_name", "owner_phone", "owner_email"):
            if not values[name]:
                errors[name] = required
        for name, longest in TEXT_LIMITS.items():
            if len(values[name]) > longest:
                errors[name] = _("At most %s characters.", longest)
        if values["owner_name"] and "owner_name" not in errors and len(values["owner_name"]) < 2:
            errors["owner_name"] = _("Enter your full name.")
        phone = values["owner_phone"]
        if phone and (not PHONE_RE.match(phone) or len(re.sub(r"\D", "", phone)) < 8):
            errors["owner_phone"] = _("Enter a valid phone number.")
        email = email_normalize(values["owner_email"]) if values["owner_email"] else False
        if values["owner_email"] and not email:
            errors["owner_email"] = _("Enter a valid email address.")
        for name, pattern, message in (
                ("tax_number", TAX_NUMBER_RE, _("The tax number is 9 digits.")),
                ("commercial_registration_number", COMMERCIAL_REG_RE,
                 _("The commercial registration number is 3 to 10 digits."))):
            if values.get(name) and name not in errors:
                digits = _digits(values[name])
                if pattern.match(digits):
                    values[name] = digits  # saved as plain digits
                else:
                    errors[name] = message
        if not values["business_type"]:
            errors["business_type_id"] = required
        if not values["state"]:
            errors["state_id"] = required
        if not values["categories"]:
            errors["product_category_ids"] = _("Choose at least one category.")
        if not values["terms_accepted"]:
            errors["terms_accepted"] = _("You must accept the selling terms and the privacy policy.")
        if not values["data_confirmed"]:
            errors["data_confirmed"] = _("You must confirm that the data is correct.")
        return errors, email

    # -------------------------------------------------------------------------
    # Application form
    # -------------------------------------------------------------------------
    @http.route("/vendor/apply", type="http", auth="public", website=True, methods=["GET", "POST"], csrf=True, sitemap=False)
    def vendor_apply(self, **post):
        if not self._apply_enabled():
            raise NotFound()
        user = request.env.user
        if user._is_public():
            # no sign-in first: the visitor confirms an email with a code, then fills the same form
            return request.render("leaders_marketplace.vendor_apply_email", self._email_values())
        again = bool(post.get("again"))
        response, previous = self._route_applicant(user, again=again)
        if response:
            return response
        if request.httprequest.method == "GET":
            return request.render("leaders_marketplace.vendor_apply_form",
                                  self._form_values(self._values_from(previous, user), previous=previous))

        form = request.httprequest.form
        if post.get("website_url"):
            # Honeypot: a human never sees this field. Look successful, create nothing.
            return request.render("leaders_marketplace.vendor_apply_form", self._form_values(previous=previous))

        values = {name: (post.get(name) or "").strip() for name in TEXT_FIELDS}
        values["owner_email"] = self._applicant_email(user)  # always the account's own email
        values["business_type_id"] = post.get("business_type_id") or ""
        values["state_id"] = post.get("state_id") or ""
        values["product_category_ids"] = form.getlist("product_category_ids")
        values["terms_accepted"] = bool(post.get("terms_accepted"))
        values["data_confirmed"] = bool(post.get("data_confirmed"))

        allowed = {c.id for top, children in self._category_groups() for c in top | children}
        values["categories"] = [int(c) for c in values["product_category_ids"] if str(c).isdigit() and int(c) in allowed]
        values["business_type"] = request.env["leaders.vendor.business.type"].sudo().browse(
            int(values["business_type_id"]) if values["business_type_id"].isdigit() else 0).exists()
        values["state"] = next((state for state, _label in self._governorates()
                                if str(state.id) == values["state_id"]), request.env["res.country.state"])
        field_errors, email = self._check(values)

        Application = request.env["leaders.vendor.application"].sudo()
        errors = []
        ip = request.httprequest.remote_addr
        if self._rate_limited(ip, email):
            errors.append(_("Too many applications from your address today. Please try again tomorrow."))

        documents = []
        labels = dict(DOCUMENT_TYPES)
        kept = self._kept_documents(previous)
        for doc_type, needed in FORM_DOCUMENTS:
            key = "doc_%s" % doc_type
            upload = request.httprequest.files.get(key)
            if not upload or not upload.filename:
                if doc_type in kept:  # filed again: the document that was not refused is carried over
                    doc = kept[doc_type]
                    documents.append((0, 0, {"name": doc.name, "document_type": doc_type, "file": doc.file,
                                             "file_name": doc.file_name, "expiry_date": doc.expiry_date}))
                elif needed:
                    field_errors[key] = _("This field is required.")
                continue
            data, error = self._document_error(upload, FILE_TYPES)
            if error:
                field_errors[key] = error
                continue
            documents.append((0, 0, {
                "name": labels[doc_type], "document_type": doc_type, "file": data,
                "file_name": os.path.basename(upload.filename)[:100],
            }))

        sheet = request.httprequest.files.get("products_sheet")
        sheet_data, sheet_name = False, False
        if sheet and sheet.filename:  # optional: the products to list, in the one products sheet
            sheet_name = os.path.basename(sheet.filename)[:100]
            raw = sheet.read()
            if not sheet_name.lower().endswith(SHEET_EXTENSIONS):
                field_errors["products_sheet"] = _("The products sheet must be an Excel (.xlsx) or CSV file.")
            elif not raw:
                field_errors["products_sheet"] = _("The file %s is empty.", sheet_name)
            elif len(raw) > self._max_mb() * 1024 * 1024:
                field_errors["products_sheet"] = _("The file %(name)s is too large (max %(size)s MB).",
                                                   name=sheet_name, size=self._max_mb())
            else:
                sheet_data = base64.b64encode(raw)

        vals = {name: values[name] or False for name in TEXT_FIELDS}
        vals.update({
            "business_type_id": values["business_type"].id or False,
            "state_id": values["state"].id or False,
            "country_id": values["state"].country_id.id or False,
            "product_category_ids": [(6, 0, values["categories"])],
            "terms_accepted": values["terms_accepted"],
            "seller_agreement_accepted": values["terms_accepted"],  # one checkbox covers the selling terms
            "data_confirmed": values["data_confirmed"],
            "document_ids": documents,
            "leaders_products_sheet": sheet_data, "leaders_products_sheet_name": sheet_name if sheet_data else False,
            "owner_email": email or values["owner_email"] or False,
            "applicant_user_id": user.id,
            "previous_application_id": previous.id or False,
            "partner_id": previous.partner_id.id or False,  # filed again: same vendor company
        })
        if not field_errors and not errors:
            # The model owns the rules: what is missing is asked of a throw-away
            # record, so a rejected attempt leaves nothing behind (not even a number).
            probe = Application.new(vals)
            errors += probe._get_missing_requirements(check_verification=False)
            try:
                probe._check_owner_email_available()
            except UserError as error:
                field_errors["owner_email"] = error.args[0]
        if field_errors or errors:
            return request.render("leaders_marketplace.vendor_apply_form",
                                  self._form_values(values, errors, field_errors, previous=previous))

        try:
            with request.env.cr.savepoint():
                application = Application.create(dict(vals, request_ip=ip))
                application.action_submit()
                if request.env["ir.config_parameter"].sudo().get_param(
                        "leaders_marketplace.vendor_instant_activation", "1") in ("1", "True", "true"):
                    application._leaders_activate_now()
        except UserError as error:
            return request.render(
                "leaders_marketplace.vendor_apply_form", self._form_values(values, [error.args[0]], previous=previous))
        terms = request.env["leaders.vendor.terms"].sudo().search([], order="version desc", limit=1)
        application.message_post(body=_(
            "Submitted through the public form from %(ip)s by the account %(login)s. Terms accepted: %(terms)s.",
            ip=ip or "-", login=user.login,
            terms=("%s v%s" % (terms.name, terms.version)) if terms else _("none published")))
        application._notify_public_submission()
        return request.redirect("/vendor/apply/status/%s?submitted=1" % application.access_token)

    # -------------------------------------------------------------------------
    # Visitor not signed in: confirm the email, then continue as that account
    # -------------------------------------------------------------------------
    @staticmethod
    def _code_hash(email, code):
        secret = request.env["ir.config_parameter"].sudo().get_param("database.secret") or ""
        return hmac.new(secret.encode(), ("%s:%s" % (email, code)).encode(), hashlib.sha256).hexdigest()

    def _email_values(self, email="", name="", error=False, sent=False):
        return {"email": email, "owner_name": name, "error": error, "sent": sent}

    def _allowed(self, bucket, identity, limit, window):
        return request.env["leaders.auth.rate"].sudo().hit("vapply_%s:%s" % (bucket, identity), limit, window)

    @http.route("/vendor/apply/email", type="http", auth="public", website=True, methods=["POST"], csrf=True,
                sitemap=False)
    def vendor_apply_email(self, **post):
        """Step before the form for a visitor: ``send`` mails a code to the email typed, ``verify`` checks it.

        A right code signs the visitor in as the account of that email (one is created when there is none), so
        an existing customer applies with their own account and becomes the vendor owner on approval, and a new
        email gets a new account. The code proves the email is theirs: nobody can apply in someone else's name.
        """
        if not self._apply_enabled():
            raise NotFound()
        if not request.env.user._is_public():
            return request.redirect(APPLY_URL)
        session = request.session
        ip = request.httprequest.remote_addr or "-"
        email = email_normalize((post.get("email") or "").strip()) or ""
        name = (post.get("owner_name") or "").strip()[:80]
        render = lambda **kw: request.render("leaders_marketplace.vendor_apply_email", self._email_values(**kw))
        if post.get("action") == "verify":
            state = session.get(EMAIL_CODE_SESSION) or {}
            email, name = state.get("email", ""), state.get("name", "")
            code = re.sub(r"\D", "", post.get("code") or "")
            if not state or state.get("expires", 0) < time.time():
                session.pop(EMAIL_CODE_SESSION, None)
                return render(email=email, name=name, error=_("The code has expired. Send a new code."))
            if state.get("tries", 0) >= EMAIL_CODE_TRIES:
                session.pop(EMAIL_CODE_SESSION, None)
                return render(email=email, name=name, error=_("Too many wrong codes. Send a new code."))
            if not hmac.compare_digest(state["hash"], self._code_hash(email, code)):
                session[EMAIL_CODE_SESSION] = dict(state, tries=state.get("tries", 0) + 1)
                return render(email=email, name=name, sent=True, error=_("The code is not correct."))
            session.pop(EMAIL_CODE_SESSION, None)
            return self._sign_in_applicant(email, name)
        # send (or send again)
        if not email:
            return render(email=post.get("email") or "", name=name, error=_("Enter a valid email address."))
        if len(name) < 2:
            return render(email=email, name=name, error=_("Enter your full name."))
        if not (self._allowed("ip", ip, 10, 3600) and self._allowed("email", email, 5, 3600)):
            return render(email=email, name=name,
                          error=_("Too many codes were requested. Please try again in an hour."))
        code = "%06d" % secrets.randbelow(1000000)
        session[EMAIL_CODE_SESSION] = {"email": email, "name": name, "hash": self._code_hash(email, code),
                                       "expires": time.time() + EMAIL_CODE_MINUTES * 60, "tries": 0}
        self._send_email_code(email, name, code)
        return render(email=email, name=name, sent=True)

    @staticmethod
    def _send_email_code(email, name, code):
        env = request.env(su=True)
        template = env.ref("leaders_marketplace.mail_template_vendor_apply_code", raise_if_not_found=False)
        if not template:
            return
        try:
            template.with_context(signup_code=code, applicant_name=name).send_mail(
                env.ref("base.user_root").id, email_values={"email_to": email, "recipient_ids": []})
            cron = env.ref("mail.ir_cron_mail_scheduler_action", raise_if_not_found=False)
            if cron:
                cron._trigger()
        except Exception:  # never reveal delivery problems to the visitor
            import logging
            logging.getLogger(__name__).exception("Could not queue the vendor application email code")

    def _sign_in_applicant(self, email, name):
        """Sign the visitor in as the account of ``email``, created as a customer account when there is none."""
        Users = request.env["res.users"].sudo().with_context(active_test=False)
        user = Users.search(["|", ("login", "=ilike", email), ("email", "=ilike", email)], limit=1)
        if user and not user.active:
            return request.render("leaders_marketplace.vendor_apply_email", self._email_values(
                email=email, name=name, error=_("This account is disabled. Contact us to apply.")))
        if user and (not user.share or user._mfa_url()):
            # staff accounts and accounts with two-factor sign-in go through the normal sign-in
            return self._login_redirect(APPLY_URL)
        if not user:
            user = Users.with_context(no_reset_password=True).create({
                "name": name or email.split("@")[0], "login": email, "email": email,
                "groups_id": [(6, 0, [request.env.ref("base.group_portal").id])]})
            user.partner_id.message_post(body=_("Account created by the vendor application form after the email "
                                                "was confirmed with a code."))
            from ..services.auth_service import AuthService
            AuthService.mark_customer(user)
            try:  # the new account has no password yet: an invitation to set one
                with request.env.cr.savepoint():
                    user.with_context(create_user=True).action_reset_password()
            except Exception:
                pass
        session = request.session
        session.pre_login = user.login
        session.pre_uid = user.id
        session.finalize(request.env)
        return request.redirect(APPLY_URL)

    @http.route("/vendor/apply/products-template.<any(xlsx,csv):fmt>", type="http", auth="public", website=True,
                methods=["GET"], sitemap=False)
    def vendor_apply_products_template(self, fmt, **kw):
        """The one products sheet, downloadable before the account is a vendor."""
        if not self._apply_enabled():
            raise NotFound()
        content, mimetype, filename = request.env["leaders.vendor.import"].sudo()._leaders_build_template("products", fmt)
        return request.make_response(content, headers=[
            ("Content-Type", mimetype), ("Content-Disposition", "attachment; filename=%s" % filename)])

    @http.route("/vendor/apply/start", type="http", auth="public", website=True, sitemap=False)
    def vendor_apply_start(self, ticket=None, **kw):
        """Sign in the storefront customer once with the ticket of ``POST /vendor-apply/link``."""
        record = request.env["leaders.vendor.apply.ticket"].sudo().consume(ticket)
        user = record.user_id
        if not record or not user.active or not user.share or user._mfa_url():
            return self._login_redirect(APPLY_URL)
        session = request.session
        if session.uid != user.id:
            if session.uid:
                session.logout(keep_db=True)
            session.pre_login = user.login
            session.pre_uid = user.id
            session.finalize(request.env)
        return request.redirect(record.target)

    @http.route("/vendor/apply/terms", type="http", auth="public", website=True, sitemap=False)
    def vendor_apply_terms(self, **kw):
        """The selling terms an applicant accepts, readable before having an account."""
        if not self._apply_enabled():
            raise NotFound()
        terms = request.env["leaders.vendor.terms"].sudo().search([], order="version desc", limit=1)
        return request.render("leaders_marketplace.vendor_apply_terms", {"terms": terms})

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
