"""Vendor terms acceptance (TASK 29 / A5)."""
import importlib.util

from odoo.exceptions import ValidationError
from odoo.modules.module import get_module_path
from odoo.tests import tagged
from odoo.tests.common import HttpCase
from odoo.tools import mute_logger

from .test_security_rpc import PORTAL_PASSWORD, MarketplacePortalUserCommon


class TermsCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Terms = cls.env["leaders.vendor.terms"]
        cls.Contract = cls.env["leaders.vendor.contract"]
        cls.Terms.with_context(active_test=False).search([]).write({"active": False})

    @classmethod
    def _terms(cls, version=1, **vals):
        values = {"name": "Seller Agreement", "version": version, "body": "<p>Body v%s</p>" % version}
        values.update(vals)
        return cls.Terms.create(values)


@tagged("post_install", "-at_install")
class TestVendorTermsModel(TermsCommon):

    def test_no_terms_means_nothing_pending(self):
        self.assertFalse(self.Terms._pending_for(self.vendor))

    def test_only_one_version_is_active(self):
        v1 = self._terms(1)
        v2 = self._terms(2)
        self.assertFalse(v1.active)
        self.assertTrue(v2.active)
        v1.write({"active": True})
        self.assertTrue(v1.active)
        self.assertFalse(v2.active)
        self.assertEqual(self.Terms.search([("active", "=", True)]), v1)

    def test_the_constraint_backstops_direct_activation(self):
        v1 = self._terms(1)
        v2 = self._terms(2)
        with self.assertRaises(ValidationError), mute_logger("odoo.sql_db"):
            self.env.cr.execute("UPDATE leaders_vendor_terms SET active = TRUE WHERE id = %s", (v1.id,))
            v1.invalidate_recordset()
            (v1 | v2)._check_single_active()

    def test_acceptance_creates_a_signed_contract_with_pdf(self):
        terms = self._terms(3)
        self.assertEqual(self.Terms._pending_for(self.vendor), terms)
        contract = terms.action_accept(self.vendor, self.owner_user, "10.1.2.3")
        self.assertEqual(contract.state, "signed")
        self.assertEqual(contract.user_id, self.owner_user)
        self.assertEqual(contract.signed_ip, "10.1.2.3")
        self.assertEqual(contract.terms_version, 3)
        self.assertTrue(contract.signed_at)
        self.assertTrue(contract.accepted)
        self.assertTrue(contract.pdf_id)
        self.assertEqual((contract.pdf_id.res_model, contract.pdf_id.res_id), (contract._name, contract.id))
        self.assertTrue(contract.pdf_id.raw)
        self.assertFalse(self.Terms._pending_for(self.vendor))
        # Idempotent.
        self.assertEqual(terms.action_accept(self.vendor, self.owner_user, "10.1.2.3"), contract)

    def test_a_new_version_requires_reacceptance(self):
        v1 = self._terms(1)
        v1.action_accept(self.vendor, self.owner_user, "1.1.1.1")
        v2 = self._terms(2)
        self.assertEqual(self.Terms._pending_for(self.vendor), v2)

    def test_drawn_signature_path_still_works(self):
        terms = self._terms(1)
        contract = self.Contract.create({"vendor_id": self.vendor.id, "terms_id": terms.id})
        with self.assertRaises(Exception):
            contract.action_sign()
        contract.signature = "iVBORw0KGgo="
        contract.action_sign()
        self.assertEqual(contract.state, "signed")
        self.assertTrue(contract.pdf_id)

    def test_one_signed_contract_per_vendor_and_terms(self):
        terms = self._terms(1)
        terms.action_accept(self.vendor, self.owner_user, "1.1.1.1")
        with self.assertRaises(ValidationError):
            self.Contract.create({"vendor_id": self.vendor.id, "terms_id": terms.id, "state": "signed"})

    def test_the_multiple_active_migration_keeps_the_latest(self):
        v1 = self._terms(1)
        v2 = self._terms(2)
        self.env.cr.execute("UPDATE leaders_vendor_terms SET active = TRUE WHERE id IN %s", ((v1.id, v2.id),))
        path = get_module_path("leaders_marketplace") + "/migrations/18.0.4.0.54/post-A5-single-active-terms.py"
        spec = importlib.util.spec_from_file_location("lm_migration_a5", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, "18.0.4.0.53")
        self.env.invalidate_all()
        self.assertEqual(self.Terms.search([("active", "=", True)]), v2)


@tagged("post_install", "-at_install")
class TestVendorTermsPortal(HttpCase, TermsCommon):

    ROUTES = ("/my/vendor", "/my/vendor/products", "/my/vendor/orders", "/my/vendor/finance")

    def _get(self, url):
        return self.url_open(url, allow_redirects=False)

    def test_no_terms_nothing_changes(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        for url in self.ROUTES:
            self.assertEqual(self._get(url).status_code, 200, url)
        page = self._get("/my/vendor/terms")
        self.assertEqual(page.status_code, 200)
        self.assertIn("no vendor terms", page.text)

    def test_owner_is_redirected_until_acceptance(self):
        terms = self._terms(1)
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        for url in self.ROUTES:
            response = self._get(url)
            self.assertEqual(response.status_code, 303, url)
            self.assertTrue(response.headers["Location"].endswith("/my/vendor/terms"), url)
        page = self._get("/my/vendor/terms")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Body v1", page.text)
        self.assertIn('name="accept"', page.text)
        # Ticking nothing does not sign.
        page = self._post("/my/vendor/terms/accept", "/my/vendor/terms")
        self.assertIn("must tick the box", page.text)
        self.assertFalse(self.Contract.search([("vendor_id", "=", self.vendor.id)]))
        # Accept.
        response = self.url_open("/my/vendor/terms/accept", data={
            "csrf_token": self._csrf("/my/vendor/terms"), "accept": "1"}, allow_redirects=False)
        self.assertEqual(response.status_code, 303)
        contract = self.Contract.search([("vendor_id", "=", self.vendor.id)])
        self.assertEqual(len(contract), 1)
        self.assertEqual((contract.state, contract.user_id, contract.terms_id), ("signed", self.owner_user, terms))
        self.assertTrue(contract.signed_ip)
        self.assertTrue(contract.pdf_id)
        for url in self.ROUTES:
            self.assertEqual(self._get(url).status_code, 200, url)
        # A new version asks again.
        self._terms(2)
        self.assertEqual(self._get("/my/vendor").status_code, 303)
        self.assertEqual(self._get("/my/vendor/terms").status_code, 200)

    def test_staff_get_a_notice_not_a_redirect_loop(self):
        self._terms(1)
        self.authenticate(self.staff_user.login, PORTAL_PASSWORD)
        for url in ("/my/vendor", "/my/vendor/orders"):
            response = self._get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertIn("waiting for acceptance", response.text)
        # The terms page is readable but staff cannot accept.
        page = self._get("/my/vendor/terms")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('name="accept"', page.text)
        response = self.url_open("/my/vendor/terms/accept", data={
            "csrf_token": self._csrf("/my/account"), "accept": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.Contract.search([("vendor_id", "=", self.vendor.id)]))

    def test_logout_stays_reachable(self):
        self._terms(1)
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        response = self.url_open("/web/session/logout", allow_redirects=False)
        self.assertIn(response.status_code, (200, 303))

    def test_the_public_application_form_shows_the_active_terms(self):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.vendor_apply_enabled", "True")
        self._terms(1)
        self._terms(2, name="Newest Agreement")
        page = self.url_open("/vendor/apply")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Newest Agreement", page.text)
        self.assertNotIn("Body v1", page.text)

    def test_api_routes_answer_403_json_until_acceptance(self):
        key = self.env["res.users.apikeys"].with_user(self.owner_user).sudo()._generate(
            None, "lm terms test", None)
        headers = {"Authorization": "Bearer %s" % key}
        for url in ("/api/v1/vendor/dashboard", "/api/v1/vendor/orders"):
            self.assertEqual(self.url_open(url, headers=headers).status_code, 200, url)
        terms = self._terms(1)
        for url in ("/api/v1/vendor/dashboard", "/api/v1/vendor/orders"):
            response = self.url_open(url, headers=headers)
            self.assertEqual(response.status_code, 403, url)
            self.assertEqual(response.json()["error"], "terms_not_accepted")
        terms.action_accept(self.vendor, self.owner_user, "1.1.1.1")
        self.assertEqual(self.url_open("/api/v1/vendor/orders", headers=headers).status_code, 200)
