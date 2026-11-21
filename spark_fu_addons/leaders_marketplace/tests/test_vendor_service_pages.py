"""A vendor's service becomes a real service page of the storefront once the back office approves it: the app reads it
from the endpoints it already uses, and the requests of the page are the vendor's."""
import json
from unittest import mock

from odoo import Command, http
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_catalog_spec import TestStorefrontApi

PUSH_APP = "odoo.addons.leaders_marketplace.models.push._firebase_app"


@tagged("post_install", "-at_install")
class TestVendorServicePages(TestStorefrontApi):

    def setUp(self):
        super().setUp()
        env = self.env
        self.vendor = env["res.partner"].create({"name": "Page Vendor", "vendor_store_name": "Page Store",
                                                 "is_marketplace_vendor": True, "vendor_state": "approved",
                                                 "leaders_contract_model": "subscription"})
        self.vendor._marketplace_ensure_vendor_stock()
        contact = env["res.partner"].create({"name": "Page Owner", "parent_id": self.vendor.id, "type": "contact",
                                             "email": "pageowner@example.com"})
        self.owner = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Page Owner", "login": "pageowner@example.com", "email": "pageowner@example.com",
            "password": "Secret123!x", "partner_id": contact.id,
            "groups_id": [Command.set([env.ref("base.group_portal").id, env.ref("leaders_marketplace.group_vendor_owner").id])]})
        self.reviewer = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Page Reviewer", "login": "pagereviewer", "password": "Secret123!x",
            "groups_id": [Command.set([env.ref("base.group_user").id, env.ref("leaders_marketplace.group_marketplace_reviewer").id])]})
        self.Service = env["leaders.vendor.service"]

    def _post(self, url="/my/vendor/own-services/new", **data):
        self.authenticate("pageowner@example.com", "Secret123!x")
        data["csrf_token"] = http.Request.csrf_token(self)
        return self.url_open(url, data=data)

    def _services(self):
        return {s["slug"]: s for s in self.get("/services")[1]}

    def _send(self, service=None, **extra):
        data = {"name": "Crane Hire", "summary": "A crane with its operator", "description": "We bring the crane.",
                "includes": "Operator\nFuel", "excludes": "Permits", "checklist_title": "What we check",
                "checklist": "Ground\nWind", "deliverable": "A signed report", "coverage": "5", "warranty_months": "3"}
        data.update(extra)
        self._post("/my/vendor/own-services/%s" % service.id if service else "/my/vendor/own-services/new", **data)
        return self.Service.search([("vendor_id", "=", self.vendor.id), ("name", "=", data["name"])])

    # ---- from the vendor to the page
    def test_the_vendor_sends_it_and_it_is_not_on_the_website_until_approved(self):
        service = self._send()
        self.assertEqual((service.state, service.live), ("submitted", False))
        self.assertTrue(service.slug and not service.service_tmpl_id)
        self.assertNotIn(service.slug, self._services())
        self.assertFalse(self.env["leaders.vendor.service"].search([("id", "=", service.id)]).filtered(lambda s: s in self.eq.leaders_own_service_ids))
        # the reviewers were told (a message to them; a push when push is on)
        self.assertIn(self.reviewer.partner_id, service.message_ids.partner_ids)
        # an incomplete form is refused with the missing part
        page = self._post(name="No summary", summary="")
        self.assertIn("Write a one-sentence summary", page.text)
        self.assertFalse(self.Service.search([("name", "=", "No summary")]))

    def test_approved_it_is_a_service_page_the_app_already_reads(self):
        service = self._send()
        with self.assertRaises(AccessError):
            service.with_user(self.owner).action_approve()  # the vendor cannot approve his own
        service.with_user(self.reviewer).action_approve()
        slug = service.slug
        self.assertEqual((service.state, service.live), ("approved", True))
        card = self._services()[slug]
        self.assertEqual((card["name"], card["summary"], card["provider"]), ("Crane Hire", "A crane with its operator", {"name": "Page Store"}))
        page = self.get("/services/%s" % slug)[1]
        self.assertEqual((page["name"], page["checklist"], page["includes"], page["excludes"], page["deliverable"]),
                         ("Crane Hire", ["Ground", "Wind"], ["Operator", "Fuel"], ["Permits"], "A signed report"))
        self.assertEqual((page["coverageGovernorates"], page["warrantyMonths"]), (5, 3))
        tiles = {t["slug"]: t for t in self.get("/categories")[1]}
        self.assertEqual((tiles[slug]["name"], tiles[slug]["href"]), ("Crane Hire", "/services/%s" % slug))
        # the platform's own services keep no provider
        self.assertTrue(all("provider" not in c for s, c in self._services().items() if s != slug))
        # the vendor got the news by email (queued) with the same text a push would carry
        self.assertTrue(self.env["mail.mail"].search([("subject", "ilike", "Crane Hire"), ("subject", "ilike", "approved")]))

    def test_a_request_on_the_page_is_the_vendors(self):
        service = self._send()
        service.with_user(self.reviewer).action_approve()
        body = {"service": service.slug, "name": "Buyer", "phone": "+201001234567",
                "serviceDetails": {"neededBy": "2099-01-01", "equipmentType": "loader"}}
        r, ref = self.send("POST", "/quotes", body, token=self.token())
        self.assertEqual(r.status_code, 201, ref)
        request = self.env["leaders.customer.request"].search([("name", "=", ref["reference"])])
        self.assertEqual((request.kind, request.service_id, request.vendor_id), ("service", service.service_tmpl_id, self.vendor))
        self.assertTrue(request._leaders_vendor_visible() and request._leaders_vendor_sees_buyer())
        self.assertIn(request, self.env["leaders.customer.request"]._leaders_vendor_requests(self.vendor))
        self.assertEqual(request._leaders_request_label(), "Crane Hire")
        # the vendor prices it and the offer reaches the customer at once
        self.assertEqual(request._leaders_vendor_next_step(), "quote")
        request.leaders_vendor_send_price(4000, "Ready in a week")
        self.assertEqual(request.state, "offered")
        _s, quote = self.get("/quotes/%s" % request.name, self.token())
        self.assertEqual((quote["stage"], quote["offer"]["total"]), ("offered", 400000))
        # a platform service request is nobody's but the platform's
        platform = self.env["product.template"].search([("leaders_service_page", "=", True), ("leaders_service_vendor_id", "=", False)], limit=1)
        own = self.env["leaders.customer.request"].create({"kind": "service", "service": platform.leaders_slug, "service_id": platform.id,
                                                           "title": platform.leaders_slug, "partner_id": self.user.partner_id.id, "phone": "1"})
        self.assertFalse(own.vendor_id)

    def test_a_commission_vendors_request_waits_for_the_platform(self):
        self.vendor.leaders_contract_model = "commission"
        service = self._send()
        service.with_user(self.reviewer).action_approve()
        request = self.env["leaders.customer.request"].create({
            "kind": "service", "service": service.slug, "service_id": service.service_tmpl_id.id, "title": service.slug,
            "partner_id": self.user.partner_id.id, "phone": "+201001234567"})
        self.assertEqual(request.vendor_id, self.vendor)
        self.assertFalse(request._leaders_vendor_visible() or request._leaders_vendor_sees_buyer())
        # a commission vendor's name is not shown on the card (the buyer deals with the platform)
        self.assertNotIn("provider", self._services()[service.slug])

    # ---- the decision
    def test_a_refusal_needs_its_reason_and_the_vendor_can_send_it_again(self):
        service = self._send()
        with self.assertRaises(UserError):
            service.with_user(self.reviewer).action_reject()
        service.reject_reason = "Too vague"
        service.with_user(self.reviewer).action_reject()
        self.assertEqual(service.state, "rejected")
        self.assertNotIn(service.slug, self._services())
        self.assertTrue(self.env["mail.mail"].search([("subject", "ilike", "Crane Hire"), ("subject", "ilike", "refused")]))
        again = self._send(service, summary="A crane with an operator, any day")
        self.assertEqual((again, again.state, again.reject_reason), (service, "submitted", False))

    def test_a_change_waits_for_approval_and_the_live_page_keeps_its_text(self):
        service = self._send()
        service.with_user(self.reviewer).action_approve()
        slug = service.slug
        self._send(service, summary="A new summary")
        self.assertEqual(service.state, "submitted")
        self.assertEqual(self._services()[slug]["summary"], "A crane with its operator")  # still the old text
        service.with_user(self.reviewer).action_approve()
        self.assertEqual(self._services()[slug]["summary"], "A new summary")
        self.assertEqual(len(self.env["product.template"].search([("leaders_slug", "=", slug)])), 1)  # one page, updated

    def test_stopping_the_service_takes_the_page_off(self):
        service = self._send()
        service.with_user(self.reviewer).action_approve()
        self.assertIn(service.slug, self._services())
        self._post("/my/vendor/own-services/%s" % service.id, archive="1")
        self.assertFalse(service.active)
        self.assertNotIn(service.slug, self._services())

    def test_only_an_approved_service_is_offered_with_a_machine(self):
        service = self._send(product_ids=[str(self.eq.id)] if False else [])
        self.eq.marketplace_owner_vendor_id = self.vendor
        service.product_tmpl_ids = self.eq
        slugs = lambda: [x["slug"] for x in self.get("/products/%s" % self.eq.leaders_slug)[1].get("services", [])]  # noqa: E731
        self.assertNotIn(service.slug, slugs())
        service.with_user(self.reviewer).action_approve()
        self.assertIn(service.slug, slugs())

    def test_a_vendor_cannot_open_another_vendors_service(self):
        other = self.env["res.partner"].create({"name": "Other Vendor", "is_marketplace_vendor": True, "vendor_state": "approved"})
        foreign = self.Service.create({"name": "Theirs", "vendor_id": other.id})
        self.authenticate("pageowner@example.com", "Secret123!x")
        self.assertEqual(self.url_open("/my/vendor/own-services/%s" % foreign.id).status_code, 403)
        self.assertEqual(self.url_open("/my/vendor/own-services").status_code, 200)

    def test_the_staff_get_a_push_for_a_service_to_approve(self):
        env = self.env
        env["ir.config_parameter"].sudo().set_param("leaders_marketplace.push_enabled", "True")
        env["ir.config_parameter"].sudo().set_param("leaders_marketplace.push_firebase_credentials_json", '{"a": 1}')
        env["leaders.push.device"]._leaders_register(self.reviewer, "fcm-" + "z" * 40, "android", "ar")
        self._send()
        message = env["leaders.push.message"].search([("event", "=", "staff_vendor_service")])
        self.assertEqual((message.user_id, message.title), (self.reviewer, "خدمة مورّد تنتظر الموافقة"))
