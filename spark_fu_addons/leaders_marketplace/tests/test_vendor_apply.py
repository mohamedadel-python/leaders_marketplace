"""Vendor application from the website (self-onboarding with the customer's own account)."""
import io
import json
import re
from datetime import timedelta
from urllib.parse import urlparse

from odoo import Command, fields
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from ..tools.auth_tokens import encode_access_token

PDF = b"%PDF-1.4\n%test\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


@tagged("post_install", "-at_install")
class TestVendorApply(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.category = cls.env["product.public.category"].create({"name": "Apply Category"})
        cls.reviewer = cls.env["res.users"].create({
            "name": "Apply Reviewer", "login": "lm.apply.reviewer@example.com",
            "email": "lm.apply.reviewer@example.com",
            "groups_id": [Command.set([
                cls.env.ref("base.group_user").id,
                cls.env.ref("leaders_marketplace.group_marketplace_reviewer").id])],
        })
        cls.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.vendor_apply_enabled", "True")
        cls.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.storefront_rate_limit_exempt_ips", "127.0.0.1")
        cls.applicant = cls._customer("apply.owner@example.com", "Apply Owner")

    @classmethod
    def _customer(cls, email, name):
        return cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": name, "login": email, "email": email, "password": "Secret123A",
            "street": "12 Tahrir St", "city": "Giza",
            "groups_id": [Command.set([cls.env.ref("base.group_portal").id])]})

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.vendor_instant_activation", "0")  # review flow
        self.authenticate("apply.owner@example.com", "Secret123A")

    def _data(self, **over):
        cairo = self.env["res.country.state"].search([("country_id.code", "=", "EG"), ("code", "=", "C")])
        data = {
            "owner_name": "Apply Owner", "business_name": "Apply Co", "owner_phone": "+201001234567",
            "owner_email": "apply.owner@example.com",
            "business_type_id": str(self.env.ref("leaders_marketplace.vendor_business_type_trader").id),
            "state_id": str(cairo.id),
            "commercial_registration_number": "CR-1", "tax_number": "TAX-1",
            "product_category_ids": str(self.category.id), "delivery_method": "Own trucks",
            "business_activity": "Generator spare parts",
            "terms_accepted": "1", "data_confirmed": "1",
        }
        data.update(over)
        return data

    def _files(self, types=("id_passport", "commercial_register", "tax_certificate")):
        return [("doc_%s" % t, ("%s.pdf" % t, io.BytesIO(PDF), "application/pdf")) for t in types]

    def _apply(self, data=None, files=None, lang=""):
        page = self.url_open(lang + "/vendor/apply")
        self.assertEqual(page.status_code, 200)
        data = dict(data or self._data(), csrf_token=re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1))
        return self.url_open(lang + "/vendor/apply", data=data, files=self._files() if files is None else files)

    def _application(self, email="apply.owner@example.com"):
        return self.env["leaders.vendor.application"].sudo().search([("owner_email", "=", email)])

    @staticmethod
    def _field_error(html, name):
        match = re.search(r'class="lm-af-error" data-for="%s">([^<]*)<' % name, html)
        return match.group(1) if match else ""

    def test_signed_in_customer_submits_a_complete_application(self):
        response = self._apply()
        application = self._application()
        self.assertEqual(len(application), 1)
        self.assertEqual(application.state, "submitted")
        self.assertEqual(application.applicant_user_id, self.applicant)
        self.assertEqual(sorted(application.document_ids.mapped("document_type")),
                         ["commercial_register", "id_passport", "tax_certificate"])
        self.assertEqual((application.owner_phone, application.business_type_id.name, application.state_id.code,
                          application.country_id.code, application.delivery_method),
                         ("+201001234567", "Trader / Retailer", "C", "EG", "Own trucks"))
        self.assertTrue(application.terms_accepted and application.seller_agreement_accepted and application.data_confirmed)
        self.assertIn(application.name, response.text)
        self.assertTrue(application.access_token)
        mails = self.env["mail.mail"].sudo().search([
            ("model", "=", "leaders.vendor.application"), ("res_id", "=", application.id),
            ("email_to", "=", "apply.owner@example.com")])
        self.assertEqual(len(mails), 1)
        self.assertIn(application.access_token, mails.body_html)
        self.assertTrue(application.activity_ids.filtered(lambda a: a.user_id == self.reviewer))

    def test_only_the_required_inputs_are_needed(self):
        """Step 2, delivery method and the description are optional."""
        self._apply(self._data(commercial_registration_number="", tax_number="", delivery_method="",
                               business_activity=""), files=self._files(("id_passport",)))
        self.assertEqual(self._application().state, "submitted")

    def test_missing_data_shows_errors_under_each_input_and_creates_nothing(self):
        response = self._apply(self._data(owner_phone="", business_type_id="", state_id="", product_category_ids="",
                                          terms_accepted="", data_confirmed=""), files=[])
        for name in ("owner_phone", "business_type_id", "state_id", "doc_id_passport"):
            self.assertEqual(self._field_error(response.text, name), "This field is required.", name)
        self.assertEqual(self._field_error(response.text, "product_category_ids"), "Choose at least one category.")
        self.assertIn("You must accept", self._field_error(response.text, "terms_accepted"))
        self.assertIn("You must confirm", self._field_error(response.text, "data_confirmed"))
        self.assertIn('data-step="1"', response.text)  # the form reopens on the first step with a problem
        self.assertFalse(self._application())

    def test_wrong_values_are_refused(self):
        response = self._apply(self._data(owner_phone="12ab", business_type_id="999999", state_id="999999"))
        self.assertEqual(self._field_error(response.text, "owner_phone"), "Enter a valid phone number.")
        self.assertEqual(self._field_error(response.text, "business_type_id"), "This field is required.")
        self.assertEqual(self._field_error(response.text, "state_id"), "This field is required.")
        self.assertFalse(self._application())

    def test_a_problem_on_the_last_step_reopens_it(self):
        response = self._apply(self._data(data_confirmed=""))
        self.assertIn('data-step="3"', response.text)
        self.assertFalse(self._application())

    def test_a_non_pdf_non_image_file_is_refused(self):
        files = self._files(("commercial_register",))
        files.append(("doc_id_passport", ("id.exe", io.BytesIO(b"MZ\x90\x00binary"), "application/pdf")))
        response = self._apply(files=files)
        self.assertIn("must be a PDF, JPG or PNG", self._field_error(response.text, "doc_id_passport"))
        self.assertFalse(self._application())

    def test_the_form_has_the_three_steps_in_english_and_arabic(self):
        page = self.url_open("/vendor/apply").text
        for text in ("1. Business information", "2. Legal information", "3. Products and operations",
                     "Governorate", "Business type", "Trader / Retailer", "Cairo"):
            self.assertIn(text, page)
        self.env["res.lang"]._activate_lang("ar_001")
        self.env["ir.module.module"]._load_module_terms(["leaders_marketplace"], ["ar_001"], overwrite=True)
        website = self.env["website"].get_current_website()
        website.language_ids |= self.env["res.lang"].search([("code", "=", "ar_001")])
        page = self.url_open("/ar/vendor/apply").text
        for text in ("قدّم طلبك للبيع في منصتنا", "1. معلومات النشاط التجاري", "2. المعلومات القانونية",
                     "3. المنتجات والتشغيل", "المحافظة", "القاهرة", "تاجر / تاجر تجزئة"):
            self.assertIn(text, page)

    def test_the_selling_terms_page_is_public(self):
        self.env["leaders.vendor.terms"].sudo().create({"name": "Seller Terms", "version": 3, "body": "<p>Be honest.</p>"})
        page = self.url_open("/vendor/apply/terms")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Be honest.", page.text)

    def test_honeypot_creates_nothing(self):
        self._apply(self._data(website_url="http://spam.example"))
        self.assertFalse(self._application())

    def test_rate_limit(self):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.vendor_apply_daily_limit", "1")
        self._apply()
        self._customer("second.owner@example.com", "Second Owner")
        self.authenticate("second.owner@example.com", "Secret123A")
        response = self._apply()
        self.assertIn("Too many applications", response.text)
        self.assertFalse(self._application("second.owner@example.com"))

    def test_disabled_by_default(self):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.vendor_apply_enabled", "False")
        self.assertEqual(self.url_open("/vendor/apply").status_code, 404)

    def test_status_page_lists_what_is_missing_and_allows_reupload(self):
        self._apply()
        application = self._application()
        url = "/vendor/apply/status/%s" % application.access_token
        page = self.url_open(url)
        self.assertIn("Document not verified: ID / Passport", page.text)

        document = application.document_ids.filtered(lambda d: d.document_type == "id_passport")
        document.write({"verification_status": "rejected", "notes": "Blurry"})
        page = self.url_open(url)
        self.assertIn("Blurry", page.text)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = self.url_open(
            "%s/document/%s" % (url, document.id), data={"csrf_token": token},
            files=[("file", ("new.pdf", io.BytesIO(PDF), "application/pdf"))])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(document.verification_status, "pending")
        self.assertEqual(document.file_name, "new.pdf")
        self.assertEqual(self.url_open("/vendor/apply/status/nope").status_code, 404)

    # ------------------------------------------------------------- the account
    def _approve(self, application):
        application = application.sudo()
        application.document_ids.action_verify()
        application.action_start_review()
        application.action_approve()
        return application

    def _reject(self, application, reason="The ID photo is blurry"):
        application = application.sudo()
        application.action_start_review()
        application.reject_reason = reason
        application.action_reject()
        return application

    def _apply_again(self, data=None, files=None):
        page = self.url_open("/vendor/apply?again=1")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        return self.url_open("/vendor/apply", data=dict(data or self._data(), csrf_token=token, again="1"),
                             files=files or [])

    def _link(self, user=None, lang="en"):
        token = encode_access_token(self.env, user or self.applicant)
        response = self.url_open("/vendor-apply/link", data=json.dumps({}), headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + token, "Accept-Language": lang})
        return response.status_code, response.json()

    def _use_session(self, response):
        """Keep the session the server just opened (the test opener holds a cookie of its own)."""
        sid = response.cookies.get("session_id")
        self.assertTrue(sid)
        self.opener.cookies.clear()
        self.opener.cookies.set("session_id", sid)

    @staticmethod
    def _path(url):
        parsed = urlparse(url)
        return parsed.path + ("?" + parsed.query if parsed.query else "")

    def test_approval_makes_the_applicant_account_the_owner(self):
        users_before = self.env["res.users"].with_context(active_test=False).search_count([])
        self._apply()
        application = self._approve(self._application())
        self.assertEqual(application.state, "approved")
        self.assertEqual(application.portal_user_id, self.applicant)
        self.assertEqual(self.env["res.users"].with_context(active_test=False).search_count([]), users_before)
        person = self.applicant.partner_id
        self.assertEqual(person.parent_id, application.partner_id)
        self.assertEqual(person.commercial_partner_id, application.partner_id)
        self.assertEqual((person.street, person.city), ("12 Tahrir St", "Giza"))  # own address kept
        self.assertTrue(self.applicant.has_group("leaders_marketplace.group_vendor_owner"))
        self.assertFalse(self.env["mail.mail"].sudo().search([
            ("email_to", "ilike", "apply.owner@example.com"), ("subject", "ilike", "password")]))

    def test_the_marketplace_settings_switch_instant_approval(self):
        icp = self.env["ir.config_parameter"].sudo()
        Settings = self.env["res.config.settings"]
        icp.search([("key", "=", "leaders_marketplace.vendor_instant_activation")]).unlink()
        self.assertTrue(Settings.create({}).marketplace_vendor_instant_activation)  # missing: on, as in the guide
        Settings.create({"marketplace_vendor_instant_activation": False}).execute()
        self.assertEqual(icp.get_param("leaders_marketplace.vendor_instant_activation"), "0")
        self.assertFalse(Settings.create({}).marketplace_vendor_instant_activation)
        response = self._apply()
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self._application().state, "approved")  # waits for staff
        Settings.create({"marketplace_vendor_instant_activation": True}).execute()
        self.assertEqual(icp.get_param("leaders_marketplace.vendor_instant_activation"), "1")

    # ------------------------------------------------------------ visitor: email code first
    def _email_step(self, **data):
        page = self.url_open("/vendor/apply")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        return self.url_open("/vendor/apply/email", data=dict(data, csrf_token=token), allow_redirects=False)

    def _emailed_code(self, email):
        mail = self.env["mail.mail"].sudo().search([("email_to", "=", email)], order="id desc", limit=1)
        match = re.search(r"<strong[^>]*>(\d{6})</strong>", mail.body_html or "")
        return match.group(1) if match else None

    def test_guests_confirm_an_email_before_the_form(self):
        self.authenticate(None, None)
        page = self.url_open("/vendor/apply", allow_redirects=False)
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="lm_apply_email_page"', page.text)
        self.assertNotIn('id="lm_apply_form"', page.text)

    def test_a_new_email_gets_a_new_account_after_the_code(self):
        self.authenticate(None, None)
        email = "brand.new.seller@example.com"
        sent = self._email_step(action="send", email=email, owner_name="New Seller")
        self.assertIn('id="lm_code_form"', sent.text)
        code = self._emailed_code(email)
        self.assertTrue(code)
        self.assertFalse(self.env["res.users"].sudo().search([("login", "=", email)]))  # nothing before the code
        wrong = self._email_step(action="verify", code="000000" if code != "000000" else "111111")
        self.assertIn("The code is not correct", wrong.text)
        done = self._email_step(action="verify", code=code)
        self.assertEqual((done.status_code, urlparse(done.headers["Location"]).path), (303, "/vendor/apply"))
        self._use_session(done)
        user = self.env["res.users"].sudo().search([("login", "=", email)])
        self.assertTrue(user.share and user.has_group("base.group_portal"))
        self.assertGreaterEqual(user.partner_id.customer_rank, 1)
        self.assertEqual(user.name, "New Seller")
        form = self.url_open("/vendor/apply").text
        self.assertIn('id="lm_apply_form"', form)  # signed in as the new account
        self.assertIn(email, form)

    def test_an_existing_email_continues_with_that_account(self):
        self.authenticate(None, None)
        self._email_step(action="send", email="Apply.Owner@Example.com", owner_name="Apply Owner")
        code = self._emailed_code("apply.owner@example.com")
        done = self._email_step(action="verify", code=code)
        self.assertEqual(urlparse(done.headers["Location"]).path, "/vendor/apply")
        self._use_session(done)
        self.assertEqual(self.env["res.users"].sudo().search_count([("login", "=", "apply.owner@example.com")]), 1)
        response = self._apply()  # filed with the existing account, which becomes the owner on approval
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._application().applicant_user_id, self.applicant)

    def test_a_staff_email_signs_in_the_normal_way(self):
        self.env["res.users"].create({
            "name": "Staff Two", "login": "lm.staff2@example.com", "email": "lm.staff2@example.com",
            "password": "Secret123A", "groups_id": [Command.set([self.env.ref("base.group_user").id])]})
        self.authenticate(None, None)
        self._email_step(action="send", email="lm.staff2@example.com", owner_name="Staff Two")
        done = self._email_step(action="verify", code=self._emailed_code("lm.staff2@example.com"))
        self.assertIn("/web/login", done.headers["Location"])

    def test_an_expired_or_overused_code_is_refused(self):
        self.authenticate(None, None)
        self._email_step(action="send", email="tries.seller@example.com", owner_name="Tries Seller")
        code = self._emailed_code("tries.seller@example.com")
        for _i in range(5):
            self._email_step(action="verify", code="999999" if code != "999999" else "888888")
        refused = self._email_step(action="verify", code=code)
        self.assertIn("Too many wrong codes", refused.text)
        self.assertFalse(self.env["res.users"].sudo().search([("login", "=", "tries.seller@example.com")]))

    def test_the_form_uses_the_account_email(self):
        page = self.url_open("/vendor/apply").text
        email_input = re.search(r'<input[^>]*id="lm_af_owner_email"[^>]*>', page).group(0)
        self.assertIn('value="apply.owner@example.com"', email_input)
        self.assertIn('readonly="readonly"', email_input)
        self.assertIn('value="Apply Owner"', page)
        self._apply(self._data(owner_email="someone.else@example.com"))
        self.assertEqual(self._application().owner_email, "apply.owner@example.com")
        self.assertFalse(self._application("someone.else@example.com"))

    def test_an_open_application_sends_the_customer_to_its_status(self):
        self._apply()
        application = self._application()
        response = self.url_open("/vendor/apply", allow_redirects=False)
        self.assertIn("/vendor/apply/status/%s" % application.access_token, response.headers["Location"])
        self.assertEqual(len(self._application()), 1)
        with self.assertRaises(Exception):
            self.env["leaders.vendor.application"].sudo().create({
                "business_name": "Twice", "owner_name": "Apply Owner", "owner_email": "apply.owner@example.com",
                "applicant_user_id": self.applicant.id})

    def test_a_rejected_customer_files_again_with_the_old_data(self):
        self._apply()
        old = self._reject(self._application())
        status = self.url_open("/vendor/apply/status/%s" % old.access_token).text
        self.assertIn("The ID photo is blurry", status)
        self.assertIn('href="/vendor/apply?again=1"', status)
        self.assertIn("/vendor/apply/status/", self.url_open("/vendor/apply", allow_redirects=False).headers["Location"])
        form = self.url_open("/vendor/apply?again=1").text
        self.assertIn('value="Apply Co"', form)
        self.assertIn("Uploaded before: <bdi>id_passport.pdf</bdi>", form)
        self._apply_again(self._data(business_name="Apply Co 2"))  # no new file: the documents are carried over
        new = self._application() - old
        self.assertEqual(new.state, "submitted")
        self.assertEqual((new.previous_application_id, new.partner_id), (old, old.partner_id))
        self.assertEqual(sorted(new.document_ids.mapped("document_type")),
                         ["commercial_register", "id_passport", "tax_certificate"])

    def test_staff_accounts_cannot_apply(self):
        staff = self.env["res.users"].create({
            "name": "Staff", "login": "lm.staff@example.com", "password": "Secret123A",
            "groups_id": [Command.set([self.env.ref("base.group_user").id])]})
        code, body = self._link(staff)
        self.assertEqual((code, body["code"]), (403, "account_not_allowed"))
        self.authenticate("lm.staff@example.com", "Secret123A")
        page = self.url_open("/vendor/apply").text
        self.assertIn("Staff accounts cannot apply", page)
        self.assertNotIn('id="lm_apply_form"', page)

    # ------------------------------------------------------------ storefront link
    def test_link_opens_the_arabic_pages_when_the_storefront_is_arabic(self):
        ar = self.env["res.lang"]._activate_lang("ar_001")
        website = self.env["website"].search([], limit=1)
        website.language_ids = [Command.link(ar.id)]
        self.env.flush_all()
        self.authenticate(None, None)
        code, body = self._link(lang="ar")
        self.assertEqual(code, 200, body)
        self.assertTrue(urlparse(body["url"]).path.startswith("/ar/vendor/apply/start"), body["url"])
        start = self.url_open(self._path(body["url"]), allow_redirects=False)
        self.assertEqual(urlparse(start.headers["Location"]).path, "/ar/vendor/apply")
        self.assertFalse(urlparse(self._link(lang="en")[1]["url"]).path.startswith("/ar"))
        guest = self.url_open("/vendor-apply/link", data="{}", headers={"Content-Type": "application/json",
                                                                         "Accept-Language": "ar"}).json()
        self.assertEqual(urlparse(guest["url"]).path, "/ar/vendor/apply")
        page = self.url_open("/ar/vendor/apply")
        self.assertEqual(page.status_code, 200)
        self.assertIn("قدّم طلبك للبيع في منصتنا", page.text)

    def test_link_without_a_token_opens_the_form(self):
        response = self.url_open("/vendor-apply/link", data="{}", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "none")
        self.assertEqual(urlparse(response.json()["url"]).path, "/vendor/apply")
        stale = self.url_open("/vendor-apply/link", data="{}", headers={"Content-Type": "application/json",
                                                                         "Authorization": "Bearer nope"})
        self.assertEqual(stale.status_code, 401)  # a stale token still asks to sign in again

    def test_link_signs_the_customer_in_once(self):
        self.authenticate(None, None)
        code, body = self._link()
        self.assertEqual((code, body["status"]), (200, "none"))
        self.assertNotIn("reference", body)
        start = self.url_open(self._path(body["url"]), allow_redirects=False)
        self.assertEqual(urlparse(start.headers["Location"]).path, "/vendor/apply")
        self._use_session(start)
        page = self.url_open("/vendor/apply").text
        self.assertIn('id="lm_apply_form"', page)  # signed in as the applicant
        self.assertIn("apply.owner@example.com", page)
        self.authenticate(None, None)
        again = self.url_open(self._path(body["url"]), allow_redirects=False)  # a ticket works once
        self.assertIn("/web/login", again.headers["Location"])

    def test_an_expired_link_asks_to_sign_in(self):
        self.authenticate(None, None)
        body = self._link()[1]
        self.env["leaders.vendor.apply.ticket"].sudo().search([]).write(
            {"expires_at": fields.Datetime.now() - timedelta(seconds=1)})
        response = self.url_open(self._path(body["url"]), allow_redirects=False)
        self.assertIn("/web/login", response.headers["Location"])

    def test_link_follows_the_application(self):
        self._apply()
        application = self._application()
        code, body = self._link()
        self.assertEqual((body["status"], body["reference"]), ("pending", application.name))
        target = self.env["leaders.vendor.apply.ticket"].sudo().search([], order="id desc", limit=1).target
        self.assertEqual(target, "/vendor/apply/status/%s" % application.access_token)
        self._reject(application)
        self.assertEqual(self._link()[1]["status"], "rejected")
        self._apply_again()
        self._approve(self._application() - application)
        body = self._link()[1]
        self.assertEqual(body["status"], "approved")
        target = self.env["leaders.vendor.apply.ticket"].sudo().search([], order="id desc", limit=1).target
        self.assertEqual(target, "/my/vendor")
        self.assertIn("/my/vendor", self.url_open("/vendor/apply", allow_redirects=False).headers["Location"])

    def test_link_when_applications_are_closed(self):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.vendor_apply_enabled", "False")
        code, body = self._link(lang="ar")
        self.assertEqual((code, body["code"]), (403, "applications_closed"))
