"""Services pages contract: GET /services, GET /services/:slug, service requests on POST /quotes."""
import json
import time
from datetime import date, timedelta

from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import Form, HttpCase

from ..services.storefront import Storefront
from ..tools.auth_tokens import encode_access_token

SLUGS = ["inspection", "maintenance", "solar", "load-bank", "temporary-power", "installation", "delivery"]


@tagged("post_install", "-at_install")
class TestServicesApi(HttpCase):

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.storefront_rate_limit_exempt_ips", "127.0.0.1")
        self.env["res.lang"]._activate_lang("ar_001")
        self.env["ir.module.module"]._load_module_terms(["leaders_marketplace"], ["ar_001"], overwrite=True)
        self.inspection = self.env.ref("leaders_marketplace.leaders_service_inspection")
        self.inspection_tile = self.env.ref("leaders_marketplace.leaders_categ_inspection")
        self.services_root = self.env.ref("leaders_marketplace.leaders_categ_services")
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()

    def get(self, path, lang="en"):
        return self.opener.get(self.base_url() + path, headers={"Accept-Language": lang}, timeout=30)

    def post(self, path, body, lang="en", token=None):
        headers = {"Content-Type": "application/json", "Accept-Language": lang}
        if token:
            headers["Authorization"] = "Bearer " + token
        return self.opener.post(self.base_url() + path, data=json.dumps(body), headers=headers, timeout=30)

    def request_body(self, **details):
        return {"name": "Ali Hassan", "phone": "+201001234567", "items": [], "service": "inspection",
                "serviceDetails": {"neededBy": self.tomorrow, "equipmentType": "generator", **details}}

    def make_user(self):
        user = self.env["res.users"].sudo().with_context(no_reset_password=True).create({
            "name": "Mona Adel", "login": "mona@example.com", "email": "mona@example.com",
            "groups_id": [Command.set([self.env.ref("base.group_portal").id])]})
        self.env.flush_all()
        return user, encode_access_token(self.env, user)

    def test_the_needed_by_date_of_the_form_reaches_the_request(self):
        """The service form sends its date inside serviceDetails: it is stored as the request's needed-by date."""
        response = self.post("/quotes", self.request_body())
        self.assertEqual(response.status_code, 201, response.text)
        record = self.env["leaders.customer.request"].sudo().search([("name", "=", response.json()["reference"])])
        self.assertEqual(str(record.needed_by), self.tomorrow)
        body = self.request_body(email="ali@example.com")
        body["locale"] = "ar"
        response = self.post("/quotes", body)
        record = self.env["leaders.customer.request"].sudo().search([("name", "=", response.json()["reference"])])
        # the name typed, the email of the form and the page's language are kept with the request
        self.assertEqual((record.contact_name, record.contact_email, record.request_locale),
                         ("Ali Hassan", "ali@example.com", "ar"))

    def test_a_guest_who_asks_again_keeps_his_contact(self):
        """The same person asking twice without signing in is one contact, not two."""
        Partner = self.env["res.partner"].sudo()
        before = Partner.search_count([])
        first = self.post("/quotes", self.request_body(email="again@example.com"))
        second = self.post("/quotes", self.request_body(email="Again@Example.com"))  # the case of the email is no difference
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(Partner.search_count([]), before + 1)
        names = [first.json()["reference"], second.json()["reference"]]
        requests = self.env["leaders.customer.request"].sudo().search([("name", "in", names)])
        self.assertEqual(len(requests.partner_id), 1)
        # without an email: the same phone and name is the same person (he is the contact already kept), another name
        # is another person
        before = Partner.search_count([])
        self.post("/quotes", self.request_body())
        self.post("/quotes", self.request_body())
        self.assertEqual(Partner.search_count([]), before)
        other = self.request_body()
        other["name"] = "Someone Else"
        self.post("/quotes", other)
        self.post("/quotes", other)
        self.assertEqual(Partner.search_count([]), before + 1)
        # a registered customer's own contact is never reused for a guest's request
        user, _token = self.make_user()
        guest = self.request_body(email="mona@example.com")
        guest["name"] = "Mona Adel"
        self.post("/quotes", guest)
        mona = self.env["leaders.customer.request"].sudo().search([("contact_email", "=", "mona@example.com")], limit=1)
        self.assertNotEqual(mona.partner_id, user.partner_id)

    # ------------------------------------------------------------------ one list for both endpoints
    def test_every_service_has_its_tile_in_categories(self):
        """The frontend reported 7 services in /services but 5 tiles in /categories: two services sat on the
        Services root. Each published service now has its own sub-category, with its name, slug and picture."""
        Template = self.env["product.template"].sudo()
        service = Template.create({
            "name": "Crane Hire", "type": "service", "leaders_slug": "crane-hire", "is_published": True,
            "public_categ_ids": [Command.set(self.services_root.ids)],
            "image_1920": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="})
        service.with_context(lang="ar_001").name = "تأجير الأوناش"
        tile = service._leaders_service_tile()
        self.assertTrue(tile)
        self.assertEqual((tile.parent_id, tile.leaders_slug), (self.services_root, "crane-hire"))
        self.env.flush_all()
        services = {s["slug"] for s in self.get("/services").json()}
        tiles = {c["href"].rsplit("/", 1)[-1]: c for c in self.get("/categories").json()
                 if c["group"] == "services" and c["parentSlug"]}
        self.assertEqual(services - set(tiles), set())  # every service has a tile that opens its page
        self.assertTrue(tiles["crane-hire"].get("image"))  # the service's picture when the tile has none
        # load-bank is also a power sub-category: its services tile has another slug but opens /services/load-bank
        self.assertEqual((tiles["load-bank"]["slug"], tiles["load-bank"]["href"]), ("load-bank-service", "/services/load-bank"))
        tiles_ar = {c["slug"]: c for c in self.get("/categories", "ar").json()}
        self.assertEqual(tiles_ar["crane-hire"]["name"], "تأجير الأوناش")

    # ------------------------------------------------------------------ pages
    def test_list_is_published_in_admin_order_and_cached(self):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.api_no_cache", "0")
        self.inspection_tile.leaders_icon = False
        response = self.get("/services", "ar")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=300")
        data = response.json()
        self.assertEqual([s["slug"] for s in data], SLUGS)
        self.assertEqual(data[0]["name"], "الفحص الفني")
        self.assertEqual(set(data[0]), {"id", "slug", "name", "summary", "mandatoryOnPurchase", "requestTitle"})
        self.assertEqual(data[0]["requestTitle"], "طلب خدمة فحص وتوثيق")
        self.assertEqual([s["mandatoryOnPurchase"] for s in data], [True, False, False, False, False, False, False])

        self.env.ref("leaders_marketplace.leaders_service_solar").sequence = 1
        self.inspection.is_published = False
        self.assertEqual([s["slug"] for s in self.get("/services").json()],
                         ["solar", "maintenance", "load-bank", "temporary-power", "installation", "delivery"])

    def test_details_match_the_contract(self):
        data = self.get("/services/inspection", "ar").json()
        self.assertEqual(data["requestTitle"], "طلب خدمة فحص وتوثيق")
        self.assertEqual(len(data["checklist"]), 8)
        self.assertEqual(data["checklist"][0], "حالة المحرك وساعات التشغيل الفعلية")
        for key in ("lead", "panelTitle", "checklistTitle", "checklistLead", "deliverable", "deliverableShort"):
            self.assertTrue(data[key], key)
        english = self.get("/services/inspection", "en").json()
        self.assertEqual(english["name"], "Technical inspection")

    def test_unknown_and_unpublished_answer_404(self):
        response = self.get("/services/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")
        self.inspection.is_published = False
        self.assertEqual(self.get("/services/inspection").status_code, 404)

    def test_missing_translation_falls_back_to_source(self):
        self.env["leaders.service.checklist.item"].with_context(lang="en_US").create(
            {"product_tmpl_id": self.inspection.id, "sequence": 999, "name": "Warranty card"})
        self.assertEqual(self.get("/services/inspection", "ar").json()["checklist"][-1], "Warranty card")

    def test_icon_is_served_to_visitors(self):
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        self.inspection.leaders_service_icon = png
        icon = self.get("/services").json()[0]["icon"]
        self.assertTrue(icon.endswith("/services/inspection/icon"))
        self.assertEqual(self.get("/services/inspection/icon").status_code, 200)

    def test_service_tile_carries_the_service_name(self):
        tiles = {t["slug"]: t for t in self.get("/categories").json()}
        self.assertEqual(tiles["maintenance"]["name"], "Maintenance & repair")
        self.assertEqual(tiles["temporary-power"]["name"], "Temporary power")  # every service has its tile

    def test_service_pages_stay_out_of_the_shop_and_products(self):
        listed = self.env["product.template"].search(Storefront(self.env).base_domain())
        self.assertFalse(listed.filtered("leaders_service_page"))
        self.assertTrue(self.inspection.is_published)
        self.assertFalse(self.inspection.leaders_kind)
        self.assertFalse(self.inspection.sale_ok)

    # ------------------------------------------------- services are products
    def new_service(self, categ, slug, **vals):
        return self.env["product.template"].create({
            "name": "Generator rental advice", "type": "service", "sale_ok": False, "purchase_ok": False,
            "leaders_slug": slug, "public_categ_ids": [Command.set(categ.ids)], "is_published": True, **vals})

    def test_a_product_in_a_services_category_is_a_service(self):
        service = self.new_service(self.services_root, "rental-advice", leaders_service_summary="Advice.")
        self.assertTrue(service.leaders_service_page)
        self.assertIn("rental-advice", [s["slug"] for s in self.get("/services").json()])
        self.assertEqual(self.get("/services/rental-advice").json()["summary"], "Advice.")
        self.assertIn("rental-advice", [t["slug"] for t in self.get("/categories").json()])  # its own tile
        service.is_published = False
        self.assertEqual(self.get("/services/rental-advice").status_code, 404)
        parts = self.env["product.public.category"].search([("leaders_group", "=", "parts")], limit=1)
        service.public_categ_ids = [Command.set(parts.ids)]
        self.assertFalse(service.leaders_service_page)

    def test_the_form_fills_type_and_slug_from_the_category(self):
        tile = self.env["product.public.category"].create({
            "name": "Rental advice", "leaders_slug": "rental-advice", "parent_id": self.services_root.id,
            "leaders_is_service": True, "leaders_can_buy": False})
        with Form(self.env["product.template"], view="leaders_marketplace.view_product_template_marketplace_base") as form:
            form.name = "Rental advice"
            form.public_categ_ids.add(tile)
            self.assertEqual(form.type, "service")
            self.assertEqual(form.leaders_slug, "rental-advice")
            self.assertFalse(form.sale_ok)
        self.assertTrue(form.record.leaders_service_page)

    def test_a_tile_shows_only_with_a_published_service(self):
        self.assertIn("inspection", [t["slug"] for t in self.get("/categories").json()])
        self.inspection.is_published = False
        tiles = [t["slug"] for t in self.get("/categories").json()]
        self.assertNotIn("inspection", tiles)
        self.assertIn("services", tiles)  # the group's own tile stays

    def test_a_tile_holds_one_service_with_its_slug(self):
        with self.assertRaises(ValidationError):
            self.new_service(self.inspection_tile, "rental-advice")
        with self.assertRaises(ValidationError):
            self.new_service(self.inspection_tile, "inspection-2")
        with self.assertRaises(ValidationError):
            self.new_service(self.services_root + self.inspection_tile, "rental-advice-2")

    def test_the_service_follows_its_tile_slug(self):
        self.inspection_tile.leaders_slug = "technical-check"
        self.assertEqual(self.inspection.leaders_slug, "technical-check")
        self.assertEqual(self.get("/services/technical-check").status_code, 200)
        tile = {t["slug"]: t for t in self.get("/categories").json()}["technical-check"]
        self.assertEqual(tile["href"], "/services/technical-check")
        self.assertEqual(self.post("/quotes", {**self.request_body(), "service": "technical-check"}).status_code, 201)
        with self.assertRaises(UserError):  # requests use it now
            self.inspection_tile.leaders_slug = "inspection"

    # --------------------------------------------------------------- requests
    def test_every_problem_is_reported_together_in_arabic(self):
        before = self.env["leaders.customer.request"].search_count([])
        response = self.post("/quotes", {
            "phone": "+201001234567", "items": [], "service": "inspection",
            "serviceDetails": {"neededBy": "2001-01-01", "equipmentType": "spaceship"}}, "ar")
        self.assertEqual(response.status_code, 422)
        data = response.json()
        self.assertEqual(data["message"], "البيانات غير صحيحة.")
        self.assertEqual(set(data["fields"]), {"name", "serviceDetails.neededBy", "serviceDetails.equipmentType"})
        self.assertEqual(data["fields"]["name"], "الاسم مطلوب.")
        self.assertEqual(self.env["leaders.customer.request"].search_count([]), before)

    def test_field_rules(self):
        body = self.request_body(neededBy="not-a-date", email="not-an-email", governorate="atlantis",
                                 company="c" * 121, brandModel="b" * 121, address="a" * 301)
        body.update(name="x", notes="n" * 1001)
        fields_ = self.post("/quotes", body).json()["fields"]
        self.assertEqual(set(fields_), {"name", "notes", "serviceDetails.neededBy", "serviceDetails.email",
                                        "serviceDetails.governorate", "serviceDetails.company",
                                        "serviceDetails.brandModel", "serviceDetails.address"})
        for details in ("x", None):
            body = self.request_body()
            body["serviceDetails"] = details
            self.assertEqual(set(self.post("/quotes", body).json()["fields"]), {"serviceDetails"})

    def test_e164_phone_only_when_turned_on(self):
        body = self.request_body()
        body["phone"] = "0100 123 4567"
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.storefront_service_phone_e164", "0")
        self.assertEqual(self.post("/quotes", body).status_code, 201)
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.storefront_service_phone_e164", "1")
        self.assertEqual(set(self.post("/quotes", body).json()["fields"]), {"phone"})
        body["phone"] = "+201001234567"
        self.assertEqual(self.post("/quotes", body).status_code, 201)

    def test_empty_strings_are_unset_and_governorate_key_is_accepted(self):
        cairo = self.env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = self.env["leaders.vendor.delivery.zone"]
        if not Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]):
            Zone.create({"is_platform": True, "state_id": cairo.id, "price": 50})
        body = self.request_body(email="", company="", governorate="cairo", brandModel="", address="")
        body["notes"] = ""
        response = self.post("/quotes", body)
        self.assertEqual(response.status_code, 201, response.text)
        record = self.env["leaders.customer.request"].search([("name", "=", response.json()["reference"])])
        self.assertEqual(json.loads(record.service_details),
                         {"neededBy": self.tomorrow, "equipmentType": "generator", "governorate": "cairo"})

    def test_request_is_a_service_order_with_its_name_activity_and_cancel(self):
        user, token = self.make_user()
        response = self.post("/quotes", self.request_body(), token=token)
        self.assertEqual(response.status_code, 201, response.text)
        reference = response.json()["reference"]
        self.assertTrue(reference.startswith("SV-"))
        record = self.env["leaders.customer.request"].search([("name", "=", reference)])
        self.assertEqual(record.service_id, self.inspection)
        self.assertEqual(record.activity_ids.user_id, self.env.ref("base.user_admin"))

        orders = self.opener.get(self.base_url() + "/account/orders?kind=service",
                                 headers={"Authorization": "Bearer " + token}, timeout=30).json()
        rows = orders["items"] if isinstance(orders, dict) else orders
        self.assertIn(reference, [r["reference"] for r in rows])
        quotes = self.opener.get(self.base_url() + "/account/orders?kind=quote",
                                 headers={"Authorization": "Bearer " + token}, timeout=30).json()
        self.assertNotIn(reference, [r["reference"] for r in (quotes["items"] if isinstance(quotes, dict) else quotes)])

        detail = self.opener.get(self.base_url() + "/quotes/" + reference, timeout=30,
                                 headers={"Authorization": "Bearer " + token, "Accept-Language": "ar"}).json()
        self.assertEqual((detail["service"], detail["serviceName"]), ("inspection", "الفحص الفني"))

        self.inspection.is_published = False  # old requests keep showing the service
        detail = self.opener.get(self.base_url() + "/quotes/" + reference, timeout=30,
                                 headers={"Authorization": "Bearer " + token}).json()
        self.assertEqual(detail["serviceName"], "Technical inspection")
        cancelled = self.post("/quotes/%s/cancel" % reference, {}, token=token)
        self.assertEqual(cancelled.status_code, 200)
        self.assertTrue(cancelled.json()["cancelled"])

    def test_unpublished_service_cannot_be_requested(self):
        self.inspection.is_published = False
        fields_ = self.post("/quotes", self.request_body()).json()["fields"]
        self.assertEqual(set(fields_), {"service"})

    def test_slug_is_stable_once_used(self):
        self.assertEqual(self.post("/quotes", self.request_body()).status_code, 201)
        with self.assertRaises(UserError):
            self.inspection.leaders_slug = "check"
        solar = self.env.ref("leaders_marketplace.leaders_service_solar")
        with self.assertRaises(ValidationError):  # a tile's service keeps the tile's slug
            solar.leaders_slug = "solar-power"
        self.env.ref("leaders_marketplace.leaders_categ_solar").leaders_slug = "solar-power"  # no request yet
        self.assertEqual(solar.leaders_slug, "solar-power")
        with self.assertRaises(ValidationError):  # load-bank has its tile now: its slug stays too
            self.env.ref("leaders_marketplace.leaders_service_load_bank").leaders_slug = "load-test"

    def test_quote_request_rules_are_unchanged(self):
        data = self.post("/quotes", {"phone": "1", "items": []}).json()
        self.assertEqual(data["fields"], {"name": "Name is required.",
                                          "items": "At least one item or service is required."})
        data = self.post("/quotes", {"name": "Ali", "phone": "1", "items": [{"productId": "999999"}]}).json()
        self.assertEqual(data["fields"], {"items.0.productId": "Unknown product."})

    # ------------------------------------------------------- services cycle
    def test_no_cache_switch_covers_every_cached_read(self):
        params = self.env["ir.config_parameter"].sudo()
        self.assertEqual(params.get_param("leaders_marketplace.api_no_cache"), "1")
        for path in ("/services", "/services/inspection", "/categories"):
            self.assertEqual(self.get(path).headers["Cache-Control"], "no-store", path)
        params.set_param("leaders_marketplace.api_no_cache", "0")
        for path in ("/services", "/services/inspection", "/categories"):
            self.assertEqual(self.get(path).headers["Cache-Control"], "public, max-age=300", path)

    def test_rate_limit_can_be_raised_but_not_lowered(self):
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("leaders_marketplace.storefront_rate_limit_exempt_ips", "")
        Rate = self.env["leaders.auth.rate"].sudo()

        def used(count):  # the counter of this minute already holds ``count`` requests
            Rate.search([]).unlink()
            self.env.cr.execute("INSERT INTO leaders_auth_rate (bucket, window_start, count) VALUES (%s, %s, %s)",
                                ("sf_catalog:127.0.0.1", int(time.time()) // 60, count))

        used(120)
        self.assertEqual(self.get("/services").status_code, 429)
        params.set_param("leaders_marketplace.storefront_rate_limit", "125")
        used(120)
        self.assertEqual([self.get("/services").status_code for _i in range(6)], [200] * 5 + [429])
        params.set_param("leaders_marketplace.storefront_rate_limit", "5")  # below the default: ignored
        used(0)
        self.assertEqual(self.get("/services").status_code, 200)

    def test_phones_are_e164_after_install(self):
        body = self.request_body()
        body["phone"] = "0100 123 4567"
        self.assertEqual(set(self.post("/quotes", body).json()["fields"]), {"phone"})

    def test_coverage_is_sent_only_when_set(self):
        self.assertNotIn("coverageGovernorates", self.get("/services").json()[0])
        self.inspection.leaders_service_coverage = 27
        self.assertEqual(self.get("/services").json()[0]["coverageGovernorates"], 27)
        self.assertEqual(self.get("/services/inspection").json()["coverageGovernorates"], 27)
        with self.assertRaises(Exception):
            self.inspection.leaders_service_coverage = -1
            self.env.flush_all()

    def test_shipped_services_without_a_tile_have_icons(self):
        services = {s["slug"]: s for s in self.get("/services").json()}
        for slug in ("load-bank", "temporary-power"):
            self.assertTrue(services[slug]["icon"].endswith("/services/%s/icon" % slug))
            response = self.get("/services/%s/icon" % slug)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"<svg", response.content)

    def test_icon_url_is_one_shape_and_falls_back_to_the_tile(self):
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        self.inspection.leaders_service_icon = False
        self.inspection_tile.leaders_icon = png
        self.assertTrue(self.get("/services").json()[0]["icon"].endswith("/services/inspection/icon"))
        self.assertEqual(self.get("/services/inspection/icon").status_code, 200)
        self.inspection_tile.leaders_icon = False
        self.assertNotIn("icon", self.get("/services").json()[0])
        response = self.get("/services/inspection/icon")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")

    def _mails(self, reference):
        """Mails to the customer; the responsible's to-do notice names the reference too."""
        return self.env["mail.mail"].sudo().search([("subject", "like", reference), ("email_to", "!=", False)])

    def test_confirmation_email_in_the_page_language(self):
        body = self.request_body(email="ali@example.com")
        body["locale"] = "ar"
        reference = self.post("/quotes", body, "en").json()["reference"]
        mail = self._mails(reference)
        self.assertEqual(len(mail), 1)
        self.assertEqual(mail.email_to, "ali@example.com")
        self.assertIn("استلمنا طلبك", mail.subject)
        self.assertIn("الفحص الفني", mail.body_html)
        english = self.post("/quotes", self.request_body(email="ali@example.com"), "en").json()["reference"]
        self.assertIn("We received your request", self._mails(english).subject)
        arabic = self.post("/quotes", self.request_body(email="ali@example.com"), "ar").json()["reference"]
        self.assertIn("استلمنا طلبك", self._mails(arabic).subject)

    def test_no_email_no_confirmation(self):
        reference = self.post("/quotes", self.request_body()).json()["reference"]
        self.assertFalse(self._mails(reference))

    def test_guest_request_joins_the_account_with_the_same_email_and_phone(self):
        body = self.request_body(email="Mona@Example.com")
        mine = self.post("/quotes", body).json()["reference"]
        body = self.request_body(email="mona@example.com")
        body["phone"] = "+201009999999"
        other_phone = self.post("/quotes", body).json()["reference"]
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.auth_register_otp", "0")
        response = self.post("/auth/register", {"name": "Mona Adel", "email": "mona@example.com",
                                                "phone": "+201001234567", "password": "Secret123!"})
        self.assertIn(response.status_code, (200, 201), response.text)
        user = self.env["res.users"].sudo().search([("login", "=", "mona@example.com")])
        Request = self.env["leaders.customer.request"].sudo()
        self.assertEqual(Request.search([("name", "=", mine)]).partner_id, user.partner_id)
        self.assertNotEqual(Request.search([("name", "=", other_phone)]).partner_id, user.partner_id)
        token = encode_access_token(self.env, user)
        self.assertEqual(self.get_auth("/quotes/%s" % mine, token).status_code, 200)
        self.assertEqual(self.get_auth("/quotes/%s" % other_phone, token).status_code, 404)

    def get_auth(self, path, token):
        return self.opener.get(self.base_url() + path, headers={"Authorization": "Bearer " + token}, timeout=30)

    def test_scope_of_the_service_is_optional_in_the_details(self):
        service = self.env.ref("leaders_marketplace.leaders_service_maintenance")
        service.write({"leaders_service_includes": False, "leaders_service_excludes": False,
                       "leaders_service_warranty_months": 0})
        body = self.get("/services/maintenance", "en").json()
        self.assertFalse({"includes", "excludes", "warrantyMonths"} & set(body))  # unset: the keys are left out
        service.write({"leaders_service_includes": "Oil and filter change\n- Belt check\n\n",
                       "leaders_service_excludes": "Spare parts price", "leaders_service_warranty_months": 3})
        service.with_context(lang="ar_001").leaders_service_includes = "تغيير الزيت والفلتر\nفحص السير"
        body = self.get("/services/maintenance", "en").json()
        self.assertEqual(body["includes"], ["Oil and filter change", "Belt check"])
        self.assertEqual((body["excludes"], body["warrantyMonths"]), (["Spare parts price"], 3))
        self.assertEqual(self.get("/services/maintenance", "ar").json()["includes"], ["تغيير الزيت والفلتر", "فحص السير"])
        self.assertNotIn("includes", self.get("/services", "en").json()[1])  # the list keeps its shape

