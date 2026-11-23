"""Who carries a service out: the providers endpoint, the choice in a service request, the vendor's offer."""
import json
from datetime import date, timedelta

from odoo.tests import tagged
from odoo.tests.common import HttpCase

PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@tagged("post_install", "-at_install")
class TestServiceProviders(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        params = env["ir.config_parameter"].sudo()
        params.set_param("leaders_marketplace.storefront_rate_limit_exempt_ips", "127.0.0.1")
        params.set_param("leaders_marketplace.auth_cookie_secure", "False")
        cls.service = env.ref("leaders_marketplace.leaders_service_inspection")
        cls.service.is_published = True
        cls.other_service = env.ref("leaders_marketplace.leaders_service_maintenance")
        cls.generators = env.ref("leaders_marketplace.specialty_generators")
        cls.load_banks = env.ref("leaders_marketplace.specialty_load_banks")
        states = env["res.country.state"].with_context(lang="en_US")
        cls.cairo = states.search([("name", "=", "Cairo"), ("country_id.code", "=", "EG")], limit=1)
        cls.giza = states.search([("name", "=", "Giza"), ("country_id.code", "=", "EG")], limit=1)
        Provider = env["leaders.service.provider"]

        def vendor(name, model="subscription"):
            partner = env["res.partner"].create({"name": name, "vendor_store_name": name, "is_marketplace_vendor": True,
                                                 "vendor_state": "approved", "image_1920": PNG})
            partner.leaders_contract_model = model
            return partner
        cls.vendor_a = vendor("Alpha Services")
        cls.vendor_b = vendor("Beta Works")
        cls.hidden = vendor("Hidden Commission", model="commission")
        cls.platform = Provider.create({"service_id": cls.service.id, "is_platform": True, "response_hours": 24,
                                        "specialty_ids": [(6, 0, (cls.generators | cls.load_banks).ids)]})
        cls.pa = Provider.create({
            "service_id": cls.service.id, "vendor_id": cls.vendor_a.id, "state": "approved", "response_hours": 12,
            "all_governorates": False, "governorate_ids": [(6, 0, (cls.cairo | cls.giza).ids)],
            "specialty_ids": [(6, 0, cls.generators.ids)]})
        cls.pb = Provider.create({
            "service_id": cls.service.id, "vendor_id": cls.vendor_b.id, "state": "approved", "response_hours": 6,
            "verified": False, "all_governorates": False, "governorate_ids": [(6, 0, cls.giza.ids)]})
        Provider.create({"service_id": cls.service.id, "vendor_id": cls.hidden.id, "state": "approved"})  # sells as the platform
        cls.pending = env["res.partner"].create({"name": "Pending", "is_marketplace_vendor": True, "vendor_state": "approved"})
        Provider.create({"service_id": cls.service.id, "vendor_id": cls.pending.id, "state": "submitted"})  # not approved
        env.flush_all()

    def get(self, path, lang="en"):
        r = self.opener.get(self.base_url() + path, headers={"Accept-Language": lang}, timeout=30)
        return r, (r.json() if r.content and "json" in r.headers.get("Content-Type", "") else None)

    def names(self, body):
        return [i["name"] for i in body["items"]]

    # ------------------------------------------------------------------ the list
    def test_the_list_shape_the_platform_first_and_who_is_left_out(self):
        r, body = self.get("/services/inspection/providers")
        self.assertEqual(r.status_code, 200, body)
        self.assertEqual((body["totalCount"], body["page"], body["pageSize"]), (3, 1, 8))
        self.assertEqual(self.names(body), ["Spark Fu", "Alpha Services", "Beta Works"])  # recommended
        platform, alpha, beta = body["items"]
        self.assertEqual((platform["id"], platform["type"], platform["typeLabel"], platform["coverage"]),
                         ("platform", "platform", "Platform", "all"))
        self.assertEqual((alpha["id"], alpha["type"], alpha["typeLabel"], alpha["verified"], alpha["responseHours"]),
                         (str(self.pa.id), "vendor", "Merchant", True, 12))
        self.assertEqual(alpha["completedJobs"], {"min": 1, "max": 10})
        self.assertEqual(sorted(c["id"] for c in alpha["coverage"]), ["cairo", "giza"])
        self.assertEqual(alpha["specialties"], [{"id": "generators", "label": "Generators"}])
        self.assertTrue(alpha["logo"].endswith("/sellers/%s/logo?unique=%s" % (self.vendor_a.id, alpha["logo"].split("=")[-1])))
        self.assertFalse(beta["verified"])

    def test_a_providers_own_logo_is_served_and_the_platform_is_named(self):
        self.pb.logo = PNG
        self.env.flush_all()
        items = {i["id"]: i for i in self.get("/services/inspection/providers")[1]["items"]}
        self.assertIn("/service-providers/%s/logo?unique=" % self.pb.id, items[str(self.pb.id)]["logo"])
        r = self.opener.get(self.base_url() + "/service-providers/%s/logo" % self.pb.id, timeout=30)
        self.assertEqual((r.status_code, r.headers["Content-Type"].split(";")[0]), (200, "image/png"))
        self.assertEqual(self.opener.get(self.base_url() + "/service-providers/%s/logo" % self.pa.id, timeout=30).status_code, 404)
        self.assertEqual(self.platform.name, "Spark Fu")
        self.assertEqual(items["platform"]["name"], "Spark Fu")

    def test_the_filters_count_everyone_and_never_shrink(self):
        _r, all_ = self.get("/services/inspection/providers")
        _r, narrowed = self.get("/services/inspection/providers?type=vendor&governorate=cairo&specialty=generators")
        self.assertEqual(narrowed["filters"], all_["filters"])
        self.assertEqual(self.names(narrowed), ["Alpha Services"])
        filters = all_["filters"]
        self.assertEqual({t["id"]: t["count"] for t in filters["types"]}, {"platform": 1, "vendor": 2})
        counts = {g["id"]: g["count"] for g in filters["governorates"]}
        self.assertEqual((counts["cairo"], counts["giza"]), (2, 3))  # the platform covers all: one in each
        self.assertEqual({s["id"]: s["count"] for s in filters["specialties"]}, {"generators": 2, "load-banks": 1})

    def test_search_in_both_languages_and_the_sorts(self):
        self.pa.name = "Alpha Services"
        arabic = self.env["res.lang"].search([("code", "like", "ar%"), ("active", "=", True)], limit=1)
        if arabic:
            self.pa.with_context(lang=arabic.code).name = "ألفا للخدمات"
            self.env.flush_all()
            self.assertEqual(self.names(self.get("/services/inspection/providers?q=ألفا", "en")[1]), ["Alpha Services"])
        self.assertEqual(self.names(self.get("/services/inspection/providers?q=ALPHA", "ar")[1])[:1],
                         ["ألفا للخدمات" if arabic else "Alpha Services"])
        self.assertEqual(self.get("/services/inspection/providers?q=zzz")[1]["totalCount"], 0)
        fastest = self.get("/services/inspection/providers?sort=fastest")[1]
        self.assertEqual(self.names(fastest), ["Beta Works", "Alpha Services", "Spark Fu"])
        self.assertEqual(self.get("/services/inspection/providers?pageSize=2&page=2")[1]["items"][0]["name"], "Beta Works")

    def test_jobs_are_a_band_not_a_number(self):
        Request = self.env["leaders.customer.request"]
        for i in range(12):
            Request.create({"kind": "service", "title": "x", "service": "inspection", "service_id": self.service.id,
                            "service_provider_id": self.pa.id, "state": "completed"})
        self.env.flush_all()
        alpha = [i for i in self.get("/services/inspection/providers")[1]["items"] if i["id"] == str(self.pa.id)][0]
        self.assertEqual(alpha["completedJobs"], {"min": 10, "max": 50})
        self.assertEqual(self.env["leaders.service.provider"]._leaders_band(75), {"min": 50})
        mostjobs = self.get("/services/inspection/providers?sort=mostJobs")[1]
        self.assertEqual(self.names(mostjobs)[0], "Alpha Services")

    def test_errors_and_an_empty_service(self):
        r, err = self.get("/services/inspection/providers?sort=bad&type=x&pageSize=99&page=0")
        self.assertEqual((r.status_code, err["code"]), (400, "validation_error"))
        self.assertEqual(set(err["fields"]), {"sort", "type", "pageSize", "page"})
        self.assertEqual(self.get("/services/nope/providers")[0].status_code, 404)
        r, empty = self.get("/services/%s/providers" % self.other_service.leaders_slug)
        self.assertEqual((r.status_code, empty["items"], empty["totalCount"], empty["filters"]),
                         (200, [], 0, {"types": [], "governorates": [], "specialties": []}))
        self.assertEqual(self.get("/services/inspection/providers", "ar")[1]["items"][0]["typeLabel"],
                         "المنصة" if self.env["res.lang"].search([("code", "like", "ar%"), ("active", "=", True)]) else "Platform")

    # ------------------------------------------------------------------ the choice
    def quote(self, **extra):
        body = {"name": "Ali Hassan", "phone": "+201001234567", "items": [], "service": "inspection",
                "serviceDetails": {"neededBy": (date.today() + timedelta(days=1)).isoformat(), "equipmentType": "generator"}}
        body.update(extra)
        r = self.opener.post(self.base_url() + "/quotes", data=json.dumps(body),
                             headers={"Content-Type": "application/json", "Accept-Language": "en"}, timeout=30)
        return r, (r.json() if r.content else None)

    def test_a_request_carries_the_chosen_provider(self):
        Request = self.env["leaders.customer.request"]
        r, made = self.quote(serviceProvider=str(self.pa.id))
        self.assertEqual(r.status_code, 201, made)
        record = Request.search([("name", "=", made["reference"])])
        self.assertEqual((record.service_provider_id, record.vendor_id), (self.pa, self.vendor_a))  # the vendor's request
        r, made = self.quote(serviceProvider="platform")
        record = Request.search([("name", "=", made["reference"])])
        self.assertEqual((r.status_code, record.service_provider_id, record.vendor_id), (201, self.platform, self.env["res.partner"]))
        r, made = self.quote()  # nobody chosen: as before
        record = Request.search([("name", "=", made["reference"])])
        self.assertEqual((r.status_code, record.service_provider_id), (201, self.env["leaders.service.provider"]))

    def test_a_provider_that_is_not_listed_is_refused(self):
        for bad in ("999999", "abc", str(self.pending.id)):
            r, err = self.quote(serviceProvider=bad)
            self.assertEqual((r.status_code, err["code"], list(err["fields"])), (400, "validation_error", ["serviceProvider"]), bad)
        hidden = self.env["leaders.service.provider"].search([("vendor_id", "=", self.hidden.id)])
        self.assertEqual(self.quote(serviceProvider=str(hidden.id))[0].status_code, 400)  # not listed: not choosable
        waiting = self.env["leaders.service.provider"].search([("vendor_id", "=", self.pending.id)])
        self.assertEqual(self.quote(serviceProvider=str(waiting.id))[0].status_code, 400)

    # ------------------------------------------------------------------ the vendor's offer and the panel texts
    def test_an_offer_is_approved_by_the_back_office_and_the_vendor_is_told(self):
        Provider = self.env["leaders.service.provider"]
        offer = Provider.create({"service_id": self.other_service.id, "vendor_id": self.vendor_a.id})
        offer.action_submit()
        self.assertEqual(offer.state, "submitted")
        self.assertNotIn(offer, Provider._leaders_public(self.other_service))
        offer.action_approve()
        self.assertIn(offer, Provider._leaders_public(self.other_service))
        again = Provider.create({"service_id": self.other_service.id, "is_platform": True})
        self.assertEqual(again.state, "approved")  # the platform is its own approver
        offer2 = Provider.create({"service_id": self.other_service.id, "vendor_id": self.vendor_b.id, "state": "submitted",
                                  "reject_reason": "Not enough experience"})
        offer2.action_reject()
        self.assertEqual(offer2.state, "rejected")
        self.assertNotIn(offer2, Provider._leaders_public(self.other_service))

    def test_the_panel_texts_ride_with_the_service(self):
        self.service.write({"leaders_service_coverage_text": "29 governorates",
                            "leaders_service_response_text": "Within 24 hours", "leaders_service_price_note": "Priced after review."})
        self.env.flush_all()
        _r, body = self.get("/services/inspection")
        self.assertEqual((body["coverageText"], body["responseText"], body["priceNote"]),
                         ("29 governorates", "Within 24 hours", "Priced after review."))
        _r, plain = self.get("/services/maintenance")
        self.assertFalse({"coverageText", "responseText", "priceNote"} & set(plain))  # left out while empty
