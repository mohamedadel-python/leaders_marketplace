"""Storefront content contract: equipment listings, site settings, pages, company film, testimonials,
avatar, wishlist, Google sign-in, and errors in the request's language."""
import base64
import io
import json
import time
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from ..services.storefront import Storefront
from ..tools.auth_tokens import encode_access_token

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


@tagged("post_install", "-at_install")
class TestStorefrontContent(HttpCase):

    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.params.set_param("leaders_marketplace.storefront_rate_limit_exempt_ips", "127.0.0.1")
        self.env["res.lang"]._activate_lang("ar_001")
        self.env["ir.module.module"]._load_module_terms(["leaders_marketplace"], ["ar_001"], overwrite=True)
        self.user = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Wish User", "login": "wish@example.com", "email": "wish@example.com", "password": "Secret123A",
            "groups_id": [(6, 0, [self.env.ref("base.group_portal").id])]})
        # start from empty content, whatever the database already holds
        self.env["leaders.site.settings"].search([]).unlink()
        self.env["leaders.testimonial"].search([]).unlink()
        self.env["leaders.site.page"].search([]).write({"published": False, "content": False})
        self.env.flush_all()
        self.token = encode_access_token(self.env, self.user)

    def call(self, method, path, body=None, lang="en", token=None, files=None):
        headers = {"Accept-Language": lang}
        if token:
            headers["Authorization"] = "Bearer " + token
        if files is None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body) if body is not None else None
        else:
            data = None
        response = self.opener.request(method, self.base_url() + path, data=data, files=files, headers=headers, timeout=30)
        return response, (response.json() if response.content and "json" in response.headers.get("Content-Type", "") else None)

    def listing(self, **over):
        return dict({"name": "Ali Hassan", "phone": "+201001234567", "intent": "sell",
                     "equipmentType": "Perkins 250 kVA generator", "condition": "used", "quantity": 1,
                     "description": "Runs well", "locale": "ar"}, **over)

    # ------------------------------------------------------- equipment listings
    def test_listing_is_saved_numbered_and_the_sales_team_is_told(self):
        sales = self.env.ref("leaders_marketplace.group_equipment_listing_sales")
        response, data = self.call("POST", "/equipment-listings", self.listing())
        self.assertEqual(response.status_code, 201)
        self.assertRegex(data["reference"], r"^SL-\d{4}$")
        listing = self.env["leaders.equipment.listing"].search([("name", "=", data["reference"])])
        self.assertEqual((listing.contact_name, listing.intent, listing.condition, listing.quantity, listing.locale),
                         ("Ali Hassan", "sell", "used", 1, "ar"))
        self.assertFalse(listing.user_id)
        self.assertEqual(set(listing.activity_ids.user_id.ids), set(sales.users.ids))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_listing_reports_every_problem_in_arabic_and_saves_nothing(self):
        before = self.env["leaders.equipment.listing"].search_count([])
        response, data = self.call("POST", "/equipment-listings", {}, lang="ar")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(data["code"], "validation_error")
        self.assertEqual(data["message"], "البيانات غير صحيحة.")
        self.assertEqual(set(data["fields"]), {"name", "phone", "intent", "equipmentType", "condition", "quantity"})
        bad = self.listing(name="A", phone="0100", intent="give", equipmentType="x", condition="old",
                           quantity=0, description="d" * 1001)
        data = self.call("POST", "/equipment-listings", bad)[1]
        self.assertEqual(set(data["fields"]),
                         {"name", "phone", "intent", "equipmentType", "condition", "quantity", "description"})
        self.assertEqual(self.env["leaders.equipment.listing"].search_count([]), before)

    def test_a_valid_bearer_links_the_listing_and_an_invalid_one_is_a_guest(self):
        reference = self.call("POST", "/equipment-listings", self.listing(), token=self.token)[1]["reference"]
        self.assertEqual(self.env["leaders.equipment.listing"].search([("name", "=", reference)]).user_id, self.user)
        response, data = self.call("POST", "/equipment-listings", self.listing(), token="broken")
        self.assertEqual(response.status_code, 201)
        self.assertFalse(self.env["leaders.equipment.listing"].search([("name", "=", data["reference"])]).user_id)

    def test_listing_limit_is_5_per_10_minutes_and_cannot_be_raised(self):
        self.params.set_param("leaders_marketplace.storefront_rate_limit_exempt_ips", "")
        self.params.set_param("leaders_marketplace.storefront_rate_limit", "500")
        Rate = self.env["leaders.auth.rate"].sudo()
        Rate.search([]).unlink()
        self.env.cr.execute("INSERT INTO leaders_auth_rate (bucket, window_start, count) VALUES (%s, %s, %s)",
                            ("sf_listing:127.0.0.1", int(time.time()) // 600, 4))
        self.assertEqual(self.call("POST", "/equipment-listings", self.listing())[0].status_code, 201)
        response, data = self.call("POST", "/equipment-listings", self.listing())
        self.assertEqual((response.status_code, data["code"]), (429, "rate_limited"))

    # ------------------------------------------------------- settings and pages
    def test_site_settings_fall_back_to_the_company_and_translate(self):
        settings = self.env["leaders.site.settings"].get_settings()
        settings.write({"phone": "+201001112223", "email": "hello@spark-fu.com", "address": "Nasr City, Cairo",
                        "legal_name": "Spark Fu LLC", "google_play_url": "https://play.google.com/x"})
        settings.with_context(lang="ar_001").address = "مدينة نصر، القاهرة"
        data = self.call("GET", "/site-settings")[1]
        self.assertEqual(data["phone"], "+201001112223")
        self.assertEqual(data["whatsapp"], "+201001112223")  # falls back to the phone
        self.assertEqual(data["address"], "Nasr City, Cairo")
        self.assertEqual(data["googlePlayUrl"], "https://play.google.com/x")
        self.assertNotIn("appStoreUrl", data)
        self.assertEqual(self.call("GET", "/site-settings", lang="ar")[1]["address"], "مدينة نصر، القاهرة")

    def test_pages_answer_only_when_published(self):
        page = self.env.ref("leaders_marketplace.site_page_privacy")
        response, data = self.call("GET", "/pages/privacy", lang="ar")
        self.assertEqual((response.status_code, data["code"], data["message"]), (404, "not_found", "الصفحة غير موجودة."))
        page.write({"content": "<p>We keep your data safe.</p>", "published": True})
        data = self.call("GET", "/pages/privacy")[1]
        self.assertEqual((data["slug"], data["title"]), ("privacy", "Privacy policy"))
        self.assertIn("We keep your data safe.", data["content"])
        self.assertRegex(data["updatedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.call("GET", "/pages/nope")[0].status_code, 404)

    # ------------------------------------------------- company film, testimonials
    def test_company_story_turns_a_youtube_link_into_an_embed(self):
        self.assertEqual(self.call("GET", "/company-story")[1], {})
        settings = self.env["leaders.site.settings"].get_settings()
        settings.write({"story_video_kind": "youtube", "story_video_url": "https://youtu.be/dQw4w9WgXcQ",
                        "story_poster": base64.b64encode(PNG)})
        data = self.call("GET", "/company-story")[1]
        self.assertEqual(data["video"], {"kind": "youtube", "url": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"})
        self.assertIn("/company-story/poster", data["poster"])
        self.assertEqual(self.url_open("/company-story/poster").status_code, 200)
        with self.assertRaises(Exception):
            settings.write({"story_video_kind": "file", "story_video_url": "http://plain.example/a.mp4"})
            self.env.flush_all()

    def test_only_published_testimonials_with_permission_are_sent(self):
        Testimonial = self.env["leaders.testimonial"]
        with self.assertRaises(Exception):
            Testimonial.create({"name": "Omar", "role": "Owner", "quote": "Great", "image": base64.b64encode(PNG),
                                "published": True})
        rows = Testimonial.create([{
            "name": "Customer %s" % n, "role": "Site manager", "quote": "Quote %s" % n, "sequence": n,
            "image": base64.b64encode(PNG), "permission": True, "published": True,
            "video_kind": "file" if n == 1 else False, "video_url": "https://cdn.example/a.mp4" if n == 1 else False,
        } for n in range(1, 7)])
        rows[-1].published = False
        data = self.call("GET", "/testimonials?limit=4")[1]
        self.assertEqual([t["name"] for t in data], ["Customer 1", "Customer 2", "Customer 3", "Customer 4"])
        self.assertEqual(data[0]["video"], {"kind": "file", "url": "https://cdn.example/a.mp4"})
        self.assertNotIn("company", data[0])
        self.assertEqual(len(self.call("GET", "/testimonials")[1]), 4)
        self.assertEqual(len(self.call("GET", "/testimonials?limit=12")[1]), 5)
        image = self.url_open("/testimonials/%s/image" % rows[0].id)
        self.assertEqual(image.status_code, 200)
        self.assertEqual(self.url_open("/testimonials/%s/image" % rows[-1].id).status_code, 404)
        with self.assertRaises(Exception):
            rows[0].quote = "q" * 161
            self.env.flush_all()

    # ------------------------------------------------------------ avatar, wishlist
    def test_avatar_upload_and_delete(self):
        response, data = self.call("POST", "/account/avatar", token=self.token,
                                   files={"file": ("me.png", io.BytesIO(PNG), "image/png")})
        self.assertEqual(response.status_code, 200)
        self.assertIn("/account/avatar/%s/" % self.user.partner_id.id, data["avatar"])
        self.assertEqual(data["email"], "wish@example.com")  # the usual AccountProfile
        response, data = self.call("POST", "/account/avatar", token=self.token, lang="ar",
                                   files={"file": ("x.txt", io.BytesIO(b"hello"), "text/plain")})
        self.assertEqual((response.status_code, data["fields"]["file"]),
                         (422, "يجب أن تكون الصورة بصيغة JPEG أو PNG أو WebP."))
        response, data = self.call("DELETE", "/account/avatar", token=self.token)
        self.assertEqual((response.status_code, data["avatar"]), (200, None))
        self.assertEqual(self.call("DELETE", "/account/avatar")[0].status_code, 401)

    def test_wishlist_put_get_delete_are_idempotent(self):
        store = Storefront(self.env)
        products = store.Template.search(store.base_domain(), limit=2)
        if len(products) < 2:
            categ = self.env.ref("leaders_marketplace.leaders_categ_generators")
            products = self.env["product.template"].create([{
                "name": "Wish Gen %s" % n, "public_categ_ids": [(6, 0, categ.ids)], "list_price": 1000,
                "leaders_condition": "new", "marketplace_state": "published", "is_published": True,
            } for n in (1, 2)])
            self.env.flush_all()
        first, second = products[0], products[1]
        for product in (first, second, first):
            self.assertEqual(self.call("PUT", "/wishlist/%s" % product.id, token=self.token)[0].status_code, 204)
        data = self.call("GET", "/wishlist", token=self.token)[1]
        self.assertEqual([p["id"] for p in data], [str(second.id), str(first.id)])  # newest first
        self.assertEqual(self.call("PUT", "/wishlist/999999", token=self.token)[0].status_code, 404)
        for _n in range(2):
            self.assertEqual(self.call("DELETE", "/wishlist/%s" % first.id, token=self.token)[0].status_code, 204)
        self.assertEqual([p["id"] for p in self.call("GET", "/wishlist", token=self.token)[1]], [str(second.id)])
        self.assertEqual(self.call("GET", "/wishlist")[0].status_code, 401)

    # ------------------------------------------------------------- Google sign-in
    def test_google_sign_in(self):
        response, data = self.call("POST", "/auth/google", {"idToken": "x"}, lang="ar")
        self.assertEqual((response.status_code, data["code"], data["message"]),
                         (503, "google_disabled", "تسجيل الدخول بجوجل غير متاح."))
        self.params.set_param("leaders_marketplace.google_client_id", "client-1.apps.googleusercontent.com")

        class Answer:
            def __init__(self, claims):
                self.status_code, self._claims = 200, claims

            def json(self):
                return self._claims

        claims = {"aud": "client-1.apps.googleusercontent.com", "iss": "https://accounts.google.com",
                  "email": "New.Google@Example.com", "email_verified": "true", "name": "Nour Google",
                  "sub": "sub-nour"}
        with patch("odoo.addons.leaders_marketplace.services.auth_service.requests.get",
                   return_value=Answer(dict(claims, aud="someone-else"))):
            self.assertEqual(self.call("POST", "/auth/google", {"idToken": "t"})[1]["code"], "invalid_credentials")
        with patch("odoo.addons.leaders_marketplace.services.auth_service.requests.get", return_value=Answer(claims)):
            response, data = self.call("POST", "/auth/google", {"idToken": "t", "remember": True, "phone": "+201007770011"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(data), {"accessToken", "tokenType", "expiresIn", "user"})  # the usual SessionResponse
            self.assertEqual(data["user"]["email"], "new.google@example.com")
            self.assertIn("refresh_token=", response.headers.get("Set-Cookie", ""))
            again = self.call("POST", "/auth/google", {"idToken": "t"})[1]
            self.assertEqual(again["user"]["id"], data["user"]["id"])  # the same account, not a second one
        self.assertEqual(self.env["res.users"].search_count([("login", "=", "new.google@example.com")]), 1)

    # ------------------------------------------------------ errors and specs in Arabic
    def test_errors_follow_the_request_language_with_the_same_shape(self):
        response, data = self.call("POST", "/auth/login", {"email": "x@x.io", "password": "x"}, lang="ar")
        self.assertEqual((response.status_code, data), (401, {"code": "invalid_credentials",
                                                             "message": "البريد الإلكتروني أو كلمة المرور غير صحيحة."}))
        data = self.call("POST", "/auth/login", {"email": "x", "password": ""}, lang="ar")[1]
        self.assertEqual(data["fields"], {"email": "البريد الإلكتروني غير صحيح.", "password": "كلمة المرور مطلوبة."})
        data = self.call("POST", "/auth/register", {"name": "A", "email": "a@b.co", "phone": "1", "password": "x"}, lang="ar")[1]
        self.assertEqual(data["fields"]["phone"], "أدخل الهاتف بالصيغة الدولية، مثل +201001234567.")
        self.assertEqual(self.call("GET", "/products/nope", lang="ar")[1]["message"], "المنتج غير موجود.")
        self.assertEqual(self.call("GET", "/account/profile", lang="ar")[1]["message"], "انتهت الجلسة. سجّل الدخول مرة أخرى.")
        data = self.call("GET", "/products?kind=x&pageSize=500", lang="ar")[1]
        self.assertEqual(data["fields"]["pageSize"], "يجب أن يكون pageSize بين 1 و48.")  # a template keeps its number
        self.assertEqual(self.call("GET", "/products/nope")[1]["message"], "Product not found.")  # English unchanged

    def test_specs_come_in_arabic_on_the_arabic_site(self):
        categ = self.env.ref("leaders_marketplace.leaders_categ_generators")
        tmpl = self.env["product.template"].create({
            "name": "Spec Loader", "public_categ_ids": [(6, 0, categ.ids)], "leaders_condition": "new",
            "leaders_capacity": 2, "leaders_capacity_unit": "ton", "leaders_warranty_months": 6,
            "leaders_specs": "change interval: every 500 h"})
        english = Storefront(self.env(context=dict(self.env.context, lang="en_US")))._specs(tmpl)
        arabic = Storefront(self.env(context=dict(self.env.context, lang="ar_001")))._specs(tmpl)
        self.assertEqual((english["power"], english["warranty"], english["changeInterval"]),
                         ("2 ton", "6 months", "every 500 h"))
        self.assertEqual((arabic["power"], arabic["warranty"], arabic["changeInterval"]),
                         ("2 طن", "6 أشهر", "كل 500 ساعة"))
        tmpl.leaders_warranty_months = 12
        self.assertEqual(Storefront(self.env(context=dict(self.env.context, lang="ar_001")))._specs(tmpl)["warranty"], "12 شهر")


@tagged("post_install", "-at_install")
class TestPartnerLogos(HttpCase):
    """GET /partners: the partner and client logos of the home page strip."""

    PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    SVG = "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMCIgaGVpZ2h0PSIxMCIvPg=="

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.storefront_rate_limit_exempt_ips", "127.0.0.1")
        self.Partner = self.env["leaders.site.partner"]
        self.Partner.with_context(active_test=False).search([]).unlink()

    def get(self, path, lang="en"):
        return self.opener.get(self.base_url() + path, headers={"Accept-Language": lang}, timeout=30)

    def test_none_is_an_empty_list(self):
        r = self.get("/partners")
        self.assertEqual((r.status_code, r.json()), (200, []))

    def test_active_partners_in_the_admin_order_with_public_logos(self):
        cummins = self.Partner.create({"name": "Cummins Egypt", "logo": self.PNG, "website": "https://www.cummins.com",
                                       "sequence": 2})
        cummins.with_context(lang="ar_001").name = "كمنز مصر"
        cat = self.Partner.create({"name": "Caterpillar", "logo": self.SVG, "sequence": 1})
        self.Partner.create({"name": "Hidden Co", "logo": self.PNG, "active": False})
        self.env.flush_all()
        data = self.get("/partners").json()
        self.assertEqual([p["id"] for p in data], [str(cat.id), str(cummins.id)])
        self.assertEqual(data[0], {"id": str(cat.id), "name": "Caterpillar", "website": None,
                                   "logoUrl": data[0]["logoUrl"]})
        self.assertEqual(data[1]["website"], "https://www.cummins.com")
        self.assertRegex(data[1]["logoUrl"], r"^https?://.+/partners/%s/logo\?unique=" % cummins.id)
        self.opener.cookies.clear()
        logo = self.opener.get(self.base_url() + data[0]["logoUrl"][data[0]["logoUrl"].index("/partners/"):], timeout=30)
        self.assertEqual(logo.status_code, 200)
        self.assertIn("svg", logo.headers["Content-Type"])
        self.assertEqual(self.get("/partners", "ar").json()[1]["name"], "كمنز مصر")

    def test_a_logo_must_be_a_picture_and_the_website_a_link(self):
        from odoo.exceptions import ValidationError
        import base64
        with self.assertRaises(ValidationError):
            self.Partner.create({"name": "Bad", "logo": base64.b64encode(b"%PDF-1.4 not a picture")})
        with self.assertRaises(ValidationError):
            self.Partner.create({"name": "Bad link", "logo": self.PNG, "website": "javascript:alert(1)"})
