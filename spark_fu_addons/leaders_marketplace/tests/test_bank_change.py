"""Vendor payout bank account change (TASK 29 / A4)."""
import base64
import importlib.util

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.modules.module import get_module_path
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import PORTAL_PASSWORD, MarketplacePortalUserCommon

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _migration(name):
    path = get_module_path("leaders_marketplace") + "/migrations/18.0.4.0.54/%s.py" % name
    spec = importlib.util.spec_from_file_location("lm_migration_%s" % name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BankChangeCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Change = cls.env["leaders.vendor.bank.change"]
        cls.Mail = cls.env["mail.mail"].sudo()
        cls.old_account = cls.env["res.partner.bank"].create({
            "partner_id": cls.vendor.id, "acc_number": "OLD1111"})
        cls.vendor.vendor_payout_bank_id = cls.old_account
        cls.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.block_payout_on_pending_bank_change", "True")
        for key in ("vendor_bank_change_approved", "vendor_bank_change_rejected"):
            cls.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.notify_%s" % key, "True")

    def _request(self, **vals):
        values = {
            "vendor_id": self.vendor.id, "requested_by": self.owner_user.id,
            "bank_name": "New Bank", "account_holder": "Vendor Holder",
            "account_number": "sa12 3456 7890", "proof": base64.b64encode(PDF), "proof_name": "proof.pdf",
        }
        values.update(vals)
        return self.Change.create(values)

    def _mails(self, subject, since):
        return self.Mail.search([("id", ">", since)]).filtered(lambda m: subject.lower() in (m.subject or "").lower())

    def _last_mail(self):
        return self.Mail.search([], order="id desc", limit=1).id or 0

    def _settlement(self, amount=50.0):
        return self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": fields.Date.today(),
            "period_to": fields.Date.today(), "currency_id": self.env.company.currency_id.id,
            "transaction_ids": [Command.create({
                "vendor_id": self.vendor.id, "type": "adjustment", "amount": amount,
                "currency_id": self.env.company.currency_id.id, "state": "available",
                "available_date": fields.Date.today(), "reference": "bank change"})],
        })


@tagged("post_install", "-at_install")
class TestBankChangeModel(BankChangeCommon):

    def test_request_is_pending_normalised_and_numbered(self):
        change = self._request()
        self.assertEqual(change.state, "pending")
        self.assertEqual(change.account_number, "SA1234567890")
        self.assertEqual(change.masked_account_number, "****7890")
        self.assertTrue(change.name.startswith("VBC/"))

    def test_proof_is_mandatory(self):
        with self.assertRaises(Exception):
            self._request(proof=False)

    def test_only_one_pending_request_per_vendor(self):
        self._request()
        with self.assertRaises(UserError):
            self._request(account_number="OTHER999")

    def test_security_email_goes_to_the_owner_and_is_not_switchable(self):
        for key in ("vendor_bank_change_approved", "vendor_bank_change_rejected"):
            self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.notify_%s" % key, False)
        since = self._last_mail()
        change = self._request()
        mails = self._mails("Security notice", since)
        self.assertEqual(len(mails), 1)
        self.assertIn(self.owner_user.partner_id, mails.recipient_ids)
        self.assertNotIn(self.staff_user.partner_id, mails.recipient_ids)
        self.assertNotIn(change.account_number, mails.body_html)
        self.assertIn("7890", mails.body_html)

    def test_approve_creates_the_account_and_sets_the_payout_account(self):
        change = self._request()
        since = self._last_mail()
        change.action_approve()
        self.assertEqual(change.state, "approved")
        account = change.bank_id
        self.assertEqual(account.acc_number, "SA1234567890")
        self.assertEqual(account.partner_id, self.vendor)
        self.assertEqual(self.vendor.vendor_payout_bank_id, account)
        self.assertEqual(change.previous_bank_id, self.old_account)
        self.assertEqual(len(self._mails("was approved", since)), 1)
        # Approved again is refused; a new request is possible afterwards.
        with self.assertRaises(UserError):
            change.action_approve()
        self._request(account_number="NEXT0001")

    def test_reject_needs_a_reason_and_keeps_the_account(self):
        change = self._request()
        with self.assertRaises(UserError):
            change.action_reject()
        since = self._last_mail()
        change.reject_reason = "Proof is unreadable"
        change.action_reject()
        self.assertEqual(change.state, "rejected")
        self.assertEqual(self.vendor.vendor_payout_bank_id, self.old_account)
        mails = self._mails("was rejected", since)
        self.assertEqual(len(mails), 1)
        self.assertIn("Proof is unreadable", mails.body_html)

    def test_a_vendor_user_cannot_review(self):
        change = self._request()
        with self.assertRaises(AccessError):
            change.with_user(self.owner_user).action_approve()
        with self.assertRaises(AccessError):
            self.Change.with_user(self.owner_user).search([])

    def test_payout_is_blocked_while_pending_and_allowed_after(self):
        settlement = self._settlement()
        settlement.action_approve()
        self.assertEqual(settlement.payout_bank_id, self.old_account)
        change = self._request()
        with self.assertRaisesRegex(UserError, "payout account change"):
            settlement.action_mark_paid()
        self.assertEqual(settlement.state, "approved")
        change.reject_reason = "No"
        change.action_reject()
        settlement.action_mark_paid()
        self.assertEqual(settlement.state, "paid")

    def test_the_block_can_be_switched_off(self):
        settlement = self._settlement()
        settlement.action_approve()
        self._request()
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.block_payout_on_pending_bank_change", False)
        settlement.action_mark_paid()
        self.assertEqual(settlement.state, "paid")

    def test_settlement_shows_the_new_payout_account_after_approval(self):
        settlement = self._settlement()
        change = self._request()
        change.action_approve()
        self.assertEqual(settlement.payout_bank_id, change.bank_id)

    def test_application_approval_sets_the_payout_account(self):
        self.vendor.vendor_payout_bank_id = False
        app = self.env["leaders.vendor.application"].new({
            "bank_name": "Approval Bank", "bank_account_holder": "Holder", "bank_account_number": "AP0001"})
        account = app._ensure_vendor_bank_account(self.vendor)
        self.assertEqual(self.vendor.vendor_payout_bank_id, account)
        # A payout account already set is never replaced by an application.
        other = self.env["leaders.vendor.application"].new({
            "bank_name": "Approval Bank", "bank_account_holder": "Holder", "bank_account_number": "AP0002"})
        other._ensure_vendor_bank_account(self.vendor)
        self.assertEqual(self.vendor.vendor_payout_bank_id, account)

    def test_migration_fills_the_payout_account(self):
        second = self.env["res.partner.bank"].create({"partner_id": self.vendor.id, "acc_number": "MIG0002"})
        self.vendor.vendor_payout_bank_id = False
        self.env["leaders.vendor.application"].search([("partner_id", "=", self.vendor.id)]).unlink()
        self.env.flush_all()
        _migration("post-A4-payout-bank-fill").migrate(self.env.cr, "18.0.4.0.53")
        self.vendor.invalidate_recordset()
        # No matching application: the vendor's first bank account.
        self.assertEqual(self.vendor.vendor_payout_bank_id, self.old_account)
        self.assertNotEqual(self.vendor.vendor_payout_bank_id, second)
        # An approved application's account wins over the first one.
        self.vendor.vendor_payout_bank_id = False
        self.env["leaders.vendor.application"].create({
            "business_name": "Mig Co", "store_name": "Mig", "owner_name": "Mig Owner", "owner_email": "mig.owner@example.com", "partner_id": self.vendor.id,
            "bank_account_number": "MIG0002", "state": "approved"})
        self.env.flush_all()
        _migration("post-A4-payout-bank-fill").migrate(self.env.cr, "18.0.4.0.53")
        self.vendor.invalidate_recordset()
        self.assertEqual(self.vendor.vendor_payout_bank_id, second)


@tagged("post_install", "-at_install")
class TestBankChangePortal(HttpCase, BankChangeCommon):

    def _profile_post(self, **data):
        files = {"proof": ("proof.pdf", PDF, "application/pdf")} if data.pop("with_proof", True) else None
        return self._post("/my/vendor/profile/bank-change", "/my/vendor/profile", files=files, **data)

    def _fields(self, **over):
        data = {"bank_name": "Portal Bank", "account_holder": "Owner Holder", "account_number": "pt 0000 9999"}
        data.update(over)
        return data

    def test_the_owner_sees_the_profile_and_requests_a_change(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/profile")
        self.assertEqual(page.status_code, 200)
        self.assertIn("****1111", page.text)
        self.assertNotIn("OLD1111", page.text)
        since = self._last_mail()
        page = self._profile_post(**self._fields())
        self.assertIn("waiting for review", page.text)
        change = self.Change.search([("vendor_id", "=", self.vendor.id)])
        self.assertEqual(len(change), 1)
        self.assertEqual(change.account_number, "PT00009999")
        self.assertEqual(change.requested_by, self.owner_user)
        self.assertEqual(change.proof_name, "proof.pdf")
        self.assertTrue(change.proof)
        self.assertEqual(len(self._mails("Security notice", since)), 1)
        # History shows the state; a second request is refused (the form is gone).
        self.assertIn(change.name, page.text)
        self.assertIn("Pending", page.text)

    def test_the_proof_is_mandatory_and_validated(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self._profile_post(with_proof=False, **self._fields())
        self.assertIn("bank proof", page.text)
        page = self._post("/my/vendor/profile/bank-change", "/my/vendor/profile",
                          files={"proof": ("bad.txt", b"just some text", "text/plain")}, **self._fields())
        self.assertIn("must be a PDF or an image", page.text)
        self.assertFalse(self.Change.search([("vendor_id", "=", self.vendor.id)]))

    def test_a_second_request_while_pending_is_refused(self):
        self._request()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self._post("/my/vendor/profile/bank-change", "/my/vendor/finance",
                   files={"proof": ("proof.pdf", PDF, "application/pdf")}, **self._fields())
        self.assertEqual(self.Change.search_count([("vendor_id", "=", self.vendor.id)]), 1)

    def test_staff_cannot_open_or_post(self):
        self.authenticate(self.staff_user.login, PORTAL_PASSWORD)
        self.assertEqual(self.url_open("/my/vendor/profile").status_code, 403)
        data = dict(self._fields(), csrf_token=self._csrf("/my/vendor/products/new"))
        response = self.url_open("/my/vendor/profile/bank-change", data=data,
                                 files={"proof": ("proof.pdf", PDF, "application/pdf")})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.Change.search([("vendor_id", "=", self.vendor.id)]))

    def test_finance_page_shows_the_payout_account(self):
        self._settlement()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/finance")
        self.assertEqual(page.status_code, 200)
        self.assertIn("****1111", page.text)
