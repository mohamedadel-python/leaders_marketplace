"""Public vendor application (self-onboarding)."""
import io
import re

from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import HttpCase

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

    def _data(self, **over):
        data = {
            "business_name": "Apply Co", "store_name": "Apply Store",
            "business_activity": "Gadgets", "legal_type": "company", "legal_name": "Apply Co LLC",
            "commercial_registration_number": "CR-1", "tax_number": "TAX-1",
            "owner_name": "Apply Owner", "owner_email": "apply.owner@example.com",
            "representative_name": "Rep", "representative_position": "GM",
            "representative_id_number": "ID-1", "bank_name": "Apply Bank",
            "bank_account_holder": "Apply Co LLC", "bank_account_number": "SA00 1234",
            "product_category_ids": str(self.category.id),
            "terms_accepted": "1", "seller_agreement_accepted": "1",
        }
        data.update(over)
        return data

    def _files(self, types=("id_passport", "bank_proof", "address_proof",
                            "commercial_register", "tax_certificate", "vat_certificate")):
        return [("doc_%s" % t, ("%s.pdf" % t, io.BytesIO(PDF), "application/pdf")) for t in types]

    def _apply(self, data=None, files=None):
        page = self.url_open("/vendor/apply")
        self.assertEqual(page.status_code, 200)
        data = dict(data or self._data(), csrf_token=re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1))
        return self.url_open("/vendor/apply", data=data, files=self._files() if files is None else files)

    def _application(self, email="apply.owner@example.com"):
        return self.env["leaders.vendor.application"].sudo().search([("owner_email", "=", email)])

    def test_anonymous_applicant_submits_a_complete_application(self):
        response = self._apply()
        application = self._application()
        self.assertEqual(len(application), 1)
        self.assertEqual(application.state, "submitted")
        self.assertEqual(len(application.document_ids), 6)
        self.assertIn(application.name, response.text)
        self.assertTrue(application.access_token)
        mails = self.env["mail.mail"].sudo().search([
            ("model", "=", "leaders.vendor.application"), ("res_id", "=", application.id),
            ("email_to", "=", "apply.owner@example.com")])
        self.assertEqual(len(mails), 1)
        self.assertIn(application.access_token, mails.body_html)
        self.assertTrue(application.activity_ids.filtered(lambda a: a.user_id == self.reviewer))

    def test_missing_data_shows_errors_and_creates_nothing(self):
        response = self._apply(self._data(representative_name="", terms_accepted=""), files=self._files(("id_passport",)))
        self.assertIn("Missing: Representative Name", response.text)
        self.assertIn("Terms &amp; Conditions not accepted", response.text)
        self.assertIn("Document not uploaded: Bank Proof", response.text)
        self.assertFalse(self._application())

    def test_a_non_pdf_non_image_file_is_refused(self):
        files = self._files(("bank_proof", "address_proof", "commercial_register", "tax_certificate", "vat_certificate"))
        files.append(("doc_id_passport", ("id.exe", io.BytesIO(b"MZ\x90\x00binary"), "application/pdf")))
        response = self._apply(files=files)
        self.assertIn("must be a PDF or an image", response.text)
        self.assertFalse(self._application())

    def test_honeypot_creates_nothing(self):
        self._apply(self._data(website_url="http://spam.example"))
        self.assertFalse(self._application())

    def test_rate_limit(self):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.vendor_apply_daily_limit", "1")
        self._apply()
        response = self._apply(self._data(owner_email="second.owner@example.com"))
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

    def test_reviewer_approval_still_creates_the_owner_portal_user(self):
        self._apply()
        application = self._application().sudo()
        application.document_ids.action_verify()
        application.action_start_review()
        application.action_approve()
        self.assertEqual(application.state, "approved")
        self.assertEqual(application.portal_user_id.login, "apply.owner@example.com")
