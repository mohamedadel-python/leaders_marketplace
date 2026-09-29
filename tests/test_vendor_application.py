import base64
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestVendorApplication(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.category = cls.env["product.public.category"].create({"name": "App Test Category"})

    def _doc(self, doc_type, **vals):
        return (0, 0, dict({
            "name": doc_type, "document_type": doc_type, "number": f"N-{doc_type}",
            "file": base64.b64encode(b"scan"), "file_name": f"{doc_type}.pdf",
        }, **vals))

    def _application(self, legal_type="company", docs=None, **vals):
        docs = docs if docs is not None else [
            "id_passport", "bank_proof", "address_proof", "commercial_register", "tax_certificate", "vat_certificate"]
        return self.env["leaders.vendor.application"].create(dict({
            "business_name": "Test Vendor Co",
            "store_name": "Test Store",
            "legal_type": legal_type,
            "legal_name": "Test Vendor Company LLC",
            "commercial_registration_number": "1010101010",
            "tax_number": "300000000000003",
            "business_activity": "Consumer electronics",
            "product_category_ids": [(6, 0, self.category.ids)],
            "owner_name": "Owner Person",
            "owner_email": " Owner.App@Test.Example ",
            "representative_name": "Owner Person",
            "representative_position": "General Manager",
            "representative_id_number": "A1234567",
            "bank_name": "Test Bank",
            "bank_account_holder": "Test Vendor Company LLC",
            "bank_account_number": "sa03 8000 0000 6080 1016 7519",
            "terms_accepted": True,
            "seller_agreement_accepted": True,
            "document_ids": [self._doc(t) for t in docs],
        }, **vals))

    def _to_review(self, app):
        app.action_submit()
        app.action_start_review()
        return app

    def test_complete_application_creates_vendor_contact_user_and_bank(self):
        app = self._to_review(self._application())
        self.assertEqual(app.owner_email, "owner.app@test.example")
        self.assertEqual(app.bank_account_number, "SA0380000000608010167519")
        company = app.partner_id
        self.assertEqual((company.name, company.vendor_state), ("Test Vendor Co", "under_review"))

        with self.assertRaises(UserError):
            app.action_approve()  # documents are still pending
        app.document_ids.action_verify()
        self.assertEqual(set(app.document_ids.mapped("verified_by")), {self.env.user})
        self.assertFalse(app.missing_requirements)

        app.action_approve()
        self.assertEqual((app.state, app.kyc_state), ("approved", "verified"))
        self.assertTrue(company.is_marketplace_vendor)
        self.assertTrue(company.is_company)
        self.assertEqual(company.company_registry, "1010101010")
        self.assertTrue(company.vendor_warehouse_id)
        bank = self.env["res.partner.bank"].search([("partner_id", "=", company.id)])
        self.assertEqual((bank.acc_number, bank.acc_holder_name, bank.bank_id.name),
                         ("SA0380000000608010167519", "Test Vendor Company LLC", "Test Bank"))

        user = app.portal_user_id
        self.assertEqual((user.login, user.partner_id), ("owner.app@test.example", app.owner_partner_id))
        self.assertEqual(app.owner_partner_id.parent_id, company)
        self.assertTrue(user.has_group("leaders_marketplace.group_vendor_owner"))
        self.assertTrue(user.has_group("base.group_portal"))
        app.action_create_portal_user()
        self.assertEqual(app.portal_user_id, user)

    def test_approval_blocked_by_missing_data_and_documents(self):
        app = self._to_review(self._application(
            docs=["id_passport"], bank_account_number=False, seller_agreement_accepted=False))
        app.document_ids.action_verify()
        missing = app.missing_requirements
        self.assertIn("Account Number / IBAN", missing)
        self.assertIn("Seller Agreement not accepted", missing)
        self.assertIn("Document not uploaded: Bank Proof", missing)
        self.assertIn("Document not uploaded: Commercial Register", missing)
        with self.assertRaises(UserError):
            app.action_approve()
        self.assertEqual(app.state, "under_review")

    def test_individual_needs_fewer_documents(self):
        app = self._to_review(self._application(
            legal_type="individual", tax_number=False, commercial_registration_number=False,
            docs=["id_passport", "bank_proof", "address_proof"]))
        app.document_ids.action_verify()
        app.action_approve()
        self.assertEqual(app.state, "approved")
        self.assertFalse(app.partner_id.is_company)

    def test_expired_and_changed_documents_block_approval(self):
        app = self._to_review(self._application())
        app.document_ids.action_verify()
        self.assertFalse(app.missing_requirements)

        id_doc = app.document_ids.filtered(lambda d: d.document_type == "id_passport")
        id_doc.expiry_date = fields.Date.today() - timedelta(days=1)
        self.assertEqual(id_doc.verification_status, "pending")  # changed -> must be checked again
        result = id_doc.action_verify()
        self.assertEqual(result["params"]["type"], "warning")
        self.assertEqual(id_doc.verification_status, "expired")
        self.assertIn("Document expired", app.missing_requirements)
        with self.assertRaises(UserError):
            app.action_approve()

    def test_kyc_and_document_rejection(self):
        app = self._to_review(self._application())
        with self.assertRaises(UserError):
            app.verify_kyc()
        doc = app.document_ids[:1]
        with self.assertRaises(UserError):
            doc.action_reject()  # a note is required
        doc.notes = "Unreadable scan"
        doc.action_reject()
        self.assertEqual(doc.verification_status, "rejected")
        (app.document_ids - doc).action_verify()
        with self.assertRaises(UserError):
            app.verify_kyc()
        doc.file = base64.b64encode(b"new scan")
        doc.action_verify()
        app.verify_kyc()
        self.assertEqual(app.kyc_state, "verified")

    def test_owner_email_must_be_free(self):
        self.env["res.users"].create({"name": "Existing", "login": "owner.app@test.example"})
        app = self._application()
        with self.assertRaises(UserError):
            app.action_submit()
