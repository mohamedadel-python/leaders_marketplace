"""Equipment / spare-parts catalogue: fields, rules, the two import sheets, the public API."""
import base64
import json
from datetime import timedelta
from urllib.parse import urlencode
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import HttpCase

from .test_vendor_import import FETCH, PNG, ImportCommon, make_csv

EQUIP_HEADERS = [
    "اسم المنتج (عربي) *", "اسم المنتج (إنجليزي)", "الفئة الفرعية *", "الماركة *", "الموديل *", "المصدر *",
    "الحالة *", "القدرة أو السعة *", "وحدة القدرة *", "مصدر الطاقة", "السعر *", "سنة الصنع", "ساعات التشغيل",
    "الرقم التسلسلي", "مدة الضمان (شهور) *", "متاح للإيجار *", "سعر الإيجار لليوم", "رابط فيديو التشغيل *",
    "روابط الصور *", "الوصف (عربي) *", "الوصف (إنجليزي)"]
PART_HEADERS = [
    "اسم القطعة (عربي) *", "اسم القطعة (إنجليزي)", "الفئة الفرعية *", "التاجز (Tags) *", "المعدات المتوافقة *",
    "الماركة *", "المصدر *", "الحالة *", "السعر *", "مدة الضمان (شهور)", "روابط الصور *", "الوصف (عربي) *",
    "الوصف (إنجليزي)", "رقم القطعة"]
IMGS = "https://img.test/1.jpg, https://img.test/2.jpg, https://img.test/3.jpg"


def equip(**kw):
    row = {
        "اسم المنتج (عربي) *": "مولد ديزل 100", "اسم المنتج (إنجليزي)": "Diesel Generator 100",
        "الفئة الفرعية *": "مولدات كهربائية", "الماركة *": "Perkins", "الموديل *": "P110-5",
        "المصدر *": "أوريجنال", "الحالة *": "جديد", "القدرة أو السعة *": "100", "وحدة القدرة *": "KVA",
        "مصدر الطاقة": "ديزل", "السعر *": "850000", "مدة الضمان (شهور) *": "24", "متاح للإيجار *": "نعم",
        "سعر الإيجار لليوم": "4500", "رابط فيديو التشغيل *": "https://v.test/a.mp4", "روابط الصور *": IMGS,
        "الوصف (عربي) *": "وصف", "الوصف (إنجليزي)": "desc"}
    row.update(kw)
    return [row.get(h, "") for h in EQUIP_HEADERS]


def part(**kw):
    row = {
        "اسم القطعة (عربي) *": "فلتر زيت", "اسم القطعة (إنجليزي)": "Oil Filter", "الفئة الفرعية *": "فلاتر",
        "التاجز (Tags) *": "فلاتر زيت, زيت", "المعدات المتوافقة *": "P110-5, P150-3", "الماركة *": "Fleetguard",
        "المصدر *": "أوريجنال", "الحالة *": "جديد", "السعر *": "320", "روابط الصور *": "https://img.test/1.jpg",
        "الوصف (عربي) *": "وصف", "رقم القطعة": "LF9009"}
    row.update(kw)
    return [row.get(h, "") for h in PART_HEADERS]


@tagged("post_install", "-at_install")
class TestCatalogSpec(ImportCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        Brand = env["leaders.brand"]
        cls.brand = Brand.search([("name", "=", "Perkins")], limit=1) or Brand.create({"name": "Perkins"})
        if not Brand.search([("name", "=", "Fleetguard")], limit=1):   # the demo data already ships both
            Brand.create({"name": "Fleetguard"})
        env["leaders.product.tag"].create([{"name": "فلاتر زيت"}, {"name": "زيت"}])
        cls.gen = env.ref("leaders_marketplace.leaders_categ_generators")
        cls.filters = env.ref("leaders_marketplace.leaders_categ_filters")
        cls.gen.write({"name": "مولدات كهربائية"})
        cls.filters.write({"name": "فلاتر"})

    def _apply(self, kind, headers, rows):
        job = self._job(headers, rows, kind=kind)
        self.assertFalse(self._errors(job), self._errors(job))
        with patch(FETCH, return_value=PNG):
            self._run(job)
        return job

    def test_category_tree_seeded_with_settings(self):
        oils = self.env.ref("leaders_marketplace.leaders_categ_oils")
        self.assertFalse(oils.leaders_accepts_used)
        self.assertEqual(oils.leaders_field_set, "part")
        self.assertTrue(self.gen.leaders_can_rent)
        self.assertTrue(self.env.ref("leaders_marketplace.leaders_categ_solar").leaders_is_service)
        with self.assertRaises(ValidationError):
            self.gen.leaders_is_service = True

    def test_brand_and_category_deletion_guards(self):
        tmpl = self.env["product.template"].create({
            "name": "X", "leaders_brand_id": self.brand.id, "public_categ_ids": [(6, 0, self.gen.ids)]})
        with self.assertRaises(UserError):
            self.brand.unlink()
        with self.assertRaises(UserError):
            self.gen.unlink()
        self.assertTrue(tmpl.leaders_field_set == "equipment")

    def test_equipment_sheet_creates_product_with_fields(self):
        job = self._apply("equipment", EQUIP_HEADERS, [equip(**{"الرقم التسلسلي": "SER-1"})])
        self.assertEqual(job.applied_rows, 1)
        tmpl = self.env["product.template"].search([("leaders_serial", "=", "SER-1")])
        self.assertEqual((tmpl.leaders_brand_id, tmpl.leaders_source_id.code, tmpl.leaders_capacity_unit, tmpl.leaders_energy_id.code),
                         (self.brand, "original", "kva", "diesel"))
        self.assertEqual(tmpl.leaders_model_id.name, "P110-5")
        self.assertTrue(tmpl.leaders_rentable)
        self.assertEqual(len(tmpl.product_template_image_ids), 2)
        self.assertEqual(tmpl.leaders_field_set, "equipment")
        self.assertFalse(tmpl.leaders_requires_inspection)

    def test_equipment_rules(self):
        rows = [
            equip(**{"الحالة *": "مستعمل"}),                                   # used without year/hours
            equip(**{"الماركة *": "Unknown"}),                                  # brand not approved
            equip(**{"سعر الإيجار لليوم": ""}),                                 # rentable without price
            equip(**{"روابط الصور *": "https://img.test/1.jpg"}),               # fewer than 3 images
            equip(**{"الفئة الفرعية *": "فلاتر"}),                              # wrong sheet
            equip(**{"الرقم التسلسلي": "DUP"}), equip(**{"الرقم التسلسلي": "DUP"}),
        ]
        job = self._job(EQUIP_HEADERS, rows, kind="equipment")
        self.assertEqual((job.valid_rows, job.invalid_rows), (1, 6))
        columns = {e[1] for e in self._errors(job)}
        self.assertTrue({"year", "brand", "rent_price_day", "image_urls", "category", "serial"} <= columns,
                        columns)

    def test_used_equipment_requires_inspection_and_year(self):
        job = self._apply("equipment", EQUIP_HEADERS, [equip(**{
            "الحالة *": "مستعمل", "سنة الصنع": "2019", "ساعات التشغيل": "6400"})])
        tmpl = job.line_ids.offer_id.product_tmpl_id
        self.assertTrue(tmpl.leaders_requires_inspection)
        tmpl.sudo().leaders_inspection_exempt = True
        self.assertFalse(tmpl.leaders_requires_inspection)
        self.assertEqual(tmpl.leaders_inspection_exempt_by, self.env.user)

    def test_oils_and_filters_refuse_used(self):
        job = self._job(PART_HEADERS, [part(**{"الحالة *": "مستعمل"})], kind="spare_parts")
        self.assertEqual(job.invalid_rows, 1)

    def test_parts_sheet_links_compatible_models_and_tags(self):
        job = self._apply("spare_parts", PART_HEADERS, [part()])
        tmpl = job.line_ids.offer_id.product_tmpl_id
        self.assertEqual(sorted(tmpl.leaders_compatible_model_ids.mapped("name")), ["P110-5", "P150-3"])
        self.assertEqual(len(tmpl.leaders_tag_ids), 2)
        # same model typed differently resolves to the same record
        job2 = self._apply("spare_parts", PART_HEADERS, [part(**{"المعدات المتوافقة *": "p110 5"})])
        self.assertEqual(self.env["leaders.equipment.model"].search_count([("key", "=", "p1105")]), 1)

    def test_unknown_tag_refused(self):
        job = self._job(PART_HEADERS, [part(**{"التاجز (Tags) *": "Totally unknown tag"})], kind="spare_parts")
        self.assertEqual(job.invalid_rows, 1)

    def test_one_sheet_for_every_product_as_in_the_project_guide(self):
        headers = ["اسم المنتج (عربي) *", "اسم المنتج (إنجليزي) *", "الفئة الفرعية *", "الماركة *", "الموديل (العائلة) *",
                   "التاجز", "المصدر *", "الحالة *", "القدرة أو السعة", "وحدة القدرة", "مصدر الطاقة", "السعر",
                   "الرقم التسلسلي", "سنة الصنع", "ساعات التشغيل", "مدة الضمان (شهور)", "للبيع أم للإيجار *",
                   "سعر الإيجار لليوم", "رابط فيديو التشغيل", "صور المنتج *", "الوصف (عربي) *", "الوصف (إنجليزي) *"]
        two = "https://img.test/1.jpg,https://img.test/2.jpg"

        def row(**kw):
            base = {"اسم المنتج (عربي) *": "مولد", "اسم المنتج (إنجليزي) *": "Generator", "الفئة الفرعية *": "مولدات كهربائية",
                    "الماركة *": "Perkins", "الموديل (العائلة) *": "UNI-110", "المصدر *": "أوريجنال", "الحالة *": "جديد",
                    "القدرة أو السعة": "100", "وحدة القدرة": "KVA", "مصدر الطاقة": "ديزل", "السعر": "850000",
                    "للبيع أم للإيجار *": "للبيع", "صور المنتج *": two, "الوصف (عربي) *": "وصف", "الوصف (إنجليزي) *": "desc"}
            base.update(kw)
            return [base.get(h, "") for h in headers]
        rows = [row(),
                row(**{"اسم المنتج (عربي) *": "فلتر", "اسم المنتج (إنجليزي) *": "Filter", "الفئة الفرعية *": "فلاتر",
                       "الماركة *": "Fleetguard", "الموديل (العائلة) *": "UNI-110, UNI-150", "القدرة أو السعة": "",
                       "وحدة القدرة": "", "مصدر الطاقة": "", "السعر": "320"}),
                row(**{"صور المنتج *": "https://img.test/1.jpg"}),                       # one image only
                row(**{"للبيع أم للإيجار *": "maybe"})]                                  # unknown listing
        job = self._job(headers, rows, kind="products")
        self.assertEqual((job.valid_rows, job.invalid_rows), (2, 2), self._errors(job))
        columns = {e[1] for e in self._errors(job)}
        self.assertTrue({"image_urls", "listing_type"} <= columns, columns)
        with patch(FETCH, return_value=PNG):
            self._run(job)
        templates = job.line_ids.offer_id.product_tmpl_id
        gen = templates.filtered(lambda t: t.leaders_kind == "power")
        part = templates.filtered(lambda t: t.leaders_kind == "part")
        self.assertEqual(gen.leaders_model_id.name, "UNI-110")
        self.assertEqual(sorted(part.leaders_compatible_model_ids.mapped("name")), ["UNI-110", "UNI-150"])
        self.assertFalse(part._leaders_catalog_problems())  # no part number nor tags needed

    def test_operation_video_is_required_for_used_equipment_only(self):
        job = self._job(EQUIP_HEADERS, [equip(**{"رابط فيديو التشغيل *": ""})], kind="equipment")
        self.assertEqual(job.invalid_rows, 0, self._errors(job))
        used = equip(**{"رابط فيديو التشغيل *": "", "الحالة *": "مستعمل", "سنة الصنع": "2019", "ساعات التشغيل": "3000"})
        job = self._job(EQUIP_HEADERS, [used], kind="equipment")
        self.assertEqual(job.invalid_rows, 1)
        tmpl = self.env["product.template"].create({"name": "Video rule", "public_categ_ids": [(6, 0, self.gen.ids)],
                                                     "leaders_condition": "new"})
        video = "%s is required." % "Operation video"
        self.assertNotIn(video, tmpl._leaders_catalog_problems())
        tmpl.write({"leaders_condition": "used"})
        self.assertIn(video, tmpl._leaders_catalog_problems())

    def test_submit_checks_catalog_completeness(self):
        tmpl = self.env["product.template"].create({
            "name": "Incomplete", "public_categ_ids": [(6, 0, self.gen.ids)]})
        with self.assertRaises(UserError):
            tmpl.action_marketplace_submit()


@tagged("post_install", "-at_install")
class TestStorefrontApi(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for key in ("rent_margin_percent", "rent_margin_min"):  # no platform rent margin unless a test sets one
            cls.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.%s" % key, "0")
        # these fixtures sell catalogue products without a vendor offer; TestUnsoldProducts covers the default
        cls.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.platform_sells_products", "1")
        env = cls.env
        # the demo catalogue (when loaded) must not change the counts these tests assert
        env["product.template"].search([("leaders_kind", "!=", False)]).write({"is_published": False})
        cls.brand = env["leaders.brand"].create({"name": "ApiBrand"})
        cls.model = env["leaders.equipment.model"].create({"name": "API-1"})
        cls.gen = env.ref("leaders_marketplace.leaders_categ_generators")
        cls.filters = env.ref("leaders_marketplace.leaders_categ_filters")
        cls.eq = env["product.template"].create({
            "name": "Api Generator", "public_categ_ids": [(6, 0, cls.gen.ids)], "leaders_brand_id": cls.brand.id,
            "leaders_source_id": cls.env.ref("leaders_marketplace.leaders_source_china").id, "leaders_condition": "used", "leaders_capacity": 150,
            "leaders_capacity_unit": "kva", "leaders_model_id": cls.model.id, "leaders_energy_id": cls.env.ref("leaders_marketplace.leaders_energy_diesel").id,
            "leaders_hours": 3000, "leaders_year": 2019, "leaders_engine_size": 8.8, "leaders_city": "cairo",
            "leaders_rentable": True, "leaders_rent_price_day": 2000, "list_price": 1850000,
            "leaders_inspected": True,  # used generator: shown only once inspected
            "marketplace_state": "published", "is_published": True})
        cls.quote_only = env["product.template"].create({
            "name": "Api Quote Gen", "public_categ_ids": [(6, 0, cls.gen.ids)], "leaders_brand_id": cls.brand.id,
            "leaders_source_id": cls.env.ref("leaders_marketplace.leaders_source_original").id, "leaders_condition": "new", "leaders_capacity": 600,
            "leaders_capacity_unit": "kva", "leaders_model_id": cls.model.id, "list_price": 0,
            "marketplace_state": "published", "is_published": True})
        cls.part = env["product.template"].create({
            "name": "Api Filter", "public_categ_ids": [(6, 0, cls.filters.ids)], "leaders_brand_id": cls.brand.id,
            "leaders_source_id": cls.env.ref("leaders_marketplace.leaders_source_turkey").id, "leaders_condition": "new", "leaders_part_number": "ZZ1",
            "leaders_compatible_model_ids": [(6, 0, cls.model.ids)], "list_price": 320,
            "marketplace_state": "published", "is_published": True})
        env["ir.config_parameter"].sudo().set_param("leaders_marketplace.auth_cookie_secure", "False")
        cls.user = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Sf User", "login": "sf@example.com", "email": "sf@example.com", "password": "Secret123",
            "groups_id": [(6, 0, [env.ref("base.group_portal").id])]})
        cls.user.partner_id.phone = "+201005550123"
        env.flush_all()

    def get(self, path, token=None):
        headers = {"Authorization": "Bearer %s" % token} if token else {}
        r = self.url_open(path, headers=headers)
        return r.status_code, (json.loads(r.content) if r.content else None)

    def send(self, method, path, body=None, token=None, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        if token:
            h["Authorization"] = "Bearer %s" % token
        r = self.opener.request(method, self.base_url() + path, data=json.dumps(body) if body is not None else None, headers=h)
        return r, (json.loads(r.content) if r.content else None)

    def token(self):
        from odoo.addons.leaders_marketplace.tools.auth_tokens import encode_access_token
        return encode_access_token(self.env, self.user)

    # ---- catalog
    def test_product_shape_and_price_in_piastres(self):
        status, body = self.get("/products/%s" % self.eq.leaders_slug)
        self.assertEqual(status, 200)
        self.assertEqual((body["kind"], body["subcategory"], body["price"], body["rentPrice"], body["brand"], body["model"]),
                         ("power", "generators", 185000000, 200000, "ApiBrand", "API-1"))
        self.assertEqual(body["availableFor"], ["buy", "rent"])
        self.assertEqual(body["name"], self.eq.name)
        _s, listed = self.get("/products?kind=power")
        self.assertEqual({i["id"]: i["name"] for i in listed["items"]}[str(self.eq.id)], self.eq.name)
        ar = self.env["res.lang"].search([("code", "like", "ar%"), ("active", "=", True)], limit=1)
        if ar:
            self.eq.with_context(lang=ar.code).name = "مولد تجريبي"
            self.env.flush_all()
            r = self.url_open("/products/%s" % self.eq.leaders_slug, headers={"Accept-Language": "ar"})
            self.assertEqual(r.json()["name"], "مولد تجريبي")
            self.assertEqual(self.get("/products/%s" % self.eq.leaders_slug)[1]["name"], self.eq.with_context(lang="en_US").name)
        self.assertEqual(body["specs"]["power"], "150 kVA")
        self.assertEqual(body["specs"]["kva"], 150)
        self.assertEqual(body["specs"]["engineSize"], "8.8 L")
        self.assertEqual(body["specs"]["runningHours"], "3,000")
        _s, quote = self.get("/products/%s" % self.quote_only.leaders_slug)
        self.assertNotIn("price", quote)
        _s, part = self.get("/products/%s" % self.part.leaders_slug)
        self.assertEqual((part["kind"], part["sources"], part["specs"]["origin"], part["sku"]),
                         ("part", ["turkish"], "aftermarket", "ZZ1"))
        self.assertIn("ApiBrand API-1", part["specs"]["compatibility"])
        self.assertEqual(self.get("/products/nope")[0], 404)

    def test_listing_filters_paging_and_validation(self):
        _s, body = self.get("/products?kind=power&subcategory=generators&kvaMin=100&kvaMax=250&powerSource=diesel&page=1&pageSize=6")
        self.assertEqual([i["id"] for i in body["items"]], [str(self.eq.id)])
        self.assertEqual((body["totalCount"], body["page"], body["pageSize"]), (1, 1, 6))
        _s, body = self.get("/products?kind=power&power=above500")
        self.assertEqual([i["id"] for i in body["items"]], [str(self.quote_only.id)])
        _s, body = self.get("/products?kind=power&priceMin=1000000&condition=used&brand=apibrand&hours=from1000&location=cairo&listing=rent")
        self.assertEqual(body["totalCount"], 1)
        _s, body = self.get("/products?kind=part&source=turkish,egyptian")
        self.assertEqual(body["totalCount"], 1)
        _s, body = self.get("/products?kind=power&sort=priceDesc")
        self.assertEqual([i["id"] for i in body["items"]], [str(self.eq.id), str(self.quote_only.id)])
        status, err = self.get("/products?kind=boat")
        self.assertEqual((status, err["code"]), (422, "validation_error"))
        self.assertEqual(self.get("/products?kind=power&pageSize=49")[0], 422)
        self.assertEqual(self.get("/products?kind=power&sort=bad")[0], 422)
        self.assertEqual(self.get("/products")[0], 422)
        _s, landing = self.get("/products?kind=part&limit=50")
        self.assertIsInstance(landing, list)
        _s, featured = self.get("/products?featured=true&limit=8")
        self.assertIsInstance(featured, list)

    def test_facets_ignore_sidebar_picks(self):
        _s, f = self.get("/products/facets?kind=power")
        self.assertEqual(f["brands"], [{"value": "ApiBrand", "count": 2}])
        self.assertEqual(f["ranges"]["price"], {"min": 1850000, "max": 1850000})
        self.assertEqual(f["ranges"]["kva"], {"min": 150, "max": 600})
        self.assertEqual([x["value"] for x in f["hours"]], ["from1000"])
        _s, f = self.get("/products/facets?kind=part")
        self.assertEqual(f["sources"], [{"value": "turkish", "count": 1}])
        self.assertNotIn("kva", f["ranges"])

    def test_categories_related_and_bought_together(self):
        _s, tiles = self.get("/categories")
        by = {t["slug"]: t for t in tiles}
        self.assertEqual(by["generators"]["href"], "/power-solutions/generators")
        self.assertEqual(by["generators"]["parentSlug"], "power-solutions")
        self.assertIsNone(by["power-solutions"]["parentSlug"])
        self.assertEqual(by["generators"]["productCount"], 2)
        self.assertEqual(by["power-solutions"]["productCount"], 2)
        self.assertEqual(by["maintenance"]["href"], "/services/maintenance")
        self.assertEqual(by["maintenance"]["group"], "services")
        _s, rel = self.get("/products/%s/related?limit=4" % self.eq.leaders_slug)
        self.assertEqual([i["id"] for i in rel], [str(self.quote_only.id)])
        _s, together = self.get("/products/%s/bought-together" % self.eq.leaders_slug)
        self.assertEqual([i["id"] for i in together], [str(self.part.id)])

    # ---- cart
    def test_guest_cart_cookie_merge_and_options(self):
        r, cart = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 2, "options": {"listing": "buy", "source": "turkish"}})
        self.assertEqual(r.status_code, 200, cart)
        self.assertIn("spark_cart", r.cookies)
        r, cart = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 1, "options": {"source": "turkish"}})
        self.assertEqual((len(cart["lines"]), cart["lines"][0]["quantity"], cart["subtotal"]), (1, 3, 96000))
        # equipment (rented or bought, priced or not) is ordered with a request, never the cart
        r, refused = self.send("POST", "/cart/items", {"productId": str(self.eq.id), "options": {"listing": "rent", "rentPeriod": "week"}})
        self.assertEqual((r.status_code, refused["code"]), (409, "equipment_request_only"))
        self.assertEqual(self.send("POST", "/cart/items", {"productId": str(self.quote_only.id)})[0].status_code, 409)
        buy_id = cart["lines"][0]["lineId"]
        self.assertEqual(self.send("POST", "/cart/items", {"productId": "999999"})[0].status_code, 404)
        r, cart = self.send("PATCH", "/cart/items/%s" % buy_id, {"quantity": 5})
        self.assertEqual([l["quantity"] for l in cart["lines"] if l["lineId"] == buy_id], [5])
        # login then merge into a user cart that already holds the same line
        token = self.token()
        self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 1, "options": {"source": "turkish"}}, token=token)
        r, merged = self.send("POST", "/cart/merge", token=token)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([l["quantity"] for l in merged["lines"] if l["product"]["id"] == str(self.part.id)], [6])
        self.assertEqual(self.send("POST", "/cart/merge")[0].status_code, 401)
        r, cart = self.send("DELETE", "/cart/items/%s" % buy_id, token=token)
        self.assertEqual(len(cart["lines"]), 0)
        r, cart = self.send("DELETE", "/cart/items/%s" % buy_id, token=token)
        self.assertEqual((r.status_code, len(cart["lines"])), (200, 0))  # deleting it again is not an error
        self.assertEqual(self.send("DELETE", "/cart", token=token)[0].status_code, 204)
        self.assertEqual(self.send("GET", "/cart", token=token)[1]["lines"], [])

    # ---- account
    def test_profile_get_patch_and_conflicts(self):
        token = self.token()
        self.assertEqual(self.get("/account/profile")[0], 401)
        _s, p = self.get("/account/profile", token)
        self.assertEqual((p["email"], p["phone"], p["avatar"]), ("sf@example.com", "+201005550123", None))
        r, p = self.send("PATCH", "/account/profile", {"name": "New Name", "email": "SF@example.com", "phone": "+201005550123", "company": "Acme", "city": "cairo"}, token=token)
        self.assertEqual((r.status_code, p["name"], p["company"], p["city"]), (200, "New Name", "Acme", "cairo"))
        r, p = self.send("PATCH", "/account/profile", {"name": "x", "email": "bad", "phone": "1"}, token=token)
        self.assertEqual((r.status_code, sorted(p["fields"])), (422, ["email", "name", "phone"]))
        self.env["res.users"].with_context(no_reset_password=True).create({"name": "Other", "login": "other@example.com", "email": "other@example.com"}).partner_id.phone = "+201111111111"
        self.env.flush_all()
        r, p = self.send("PATCH", "/account/profile", {"name": "New Name", "email": "other@example.com", "phone": "+201005550123"}, token=token)
        self.assertEqual((r.status_code, p["code"]), (409, "email_taken"))
        r, p = self.send("PATCH", "/account/profile", {"name": "New Name", "email": "sf@example.com", "phone": "+201111111111"}, token=token)
        self.assertEqual((r.status_code, p["code"]), (409, "phone_taken"))

    def test_orders_notifications_and_password(self):
        token = self.token()
        partner = self.user.partner_id
        self.env["leaders.customer.request"].create([
            {"kind": "quote", "partner_id": partner.id, "title": "Generator 250", "quantity": 1},
            {"kind": "service", "partner_id": partner.id, "title": "Maintenance", "quantity": 2, "state": "completed"}])
        self.env.flush_all()
        _s, q = self.get("/account/orders?kind=quote", token)
        self.assertEqual((q["totalCount"], q["items"][0]["status"], q["items"][0]["reference"][:3]), (1, "pending", "SQ-"))
        _s, q = self.get("/account/orders?kind=service&q=maint", token)
        self.assertEqual(q["items"][0]["status"], "completed")
        _s, q = self.get("/account/orders?kind=purchase", token)
        self.assertEqual((q["items"], q["pageSize"]), ([], 5))
        self.assertEqual(self.get("/account/orders?kind=x", token)[0], 422)
        _s, prefs = self.get("/account/notifications", token)
        self.assertEqual(prefs, {"orderUpdates": True, "quoteReplies": True, "offers": False, "newsletter": False,
                                 "sms": False, "whatsapp": False, "email": True})  # topic flags and channels
        full = {"orderUpdates": False, "quoteReplies": True, "offers": True, "newsletter": True}
        _r, saved = self.send("PUT", "/account/notifications", full, token=token)
        self.assertEqual({k: saved[k] for k in full}, full)
        self.assertEqual(self.send("PUT", "/account/notifications", {"offers": True}, token=token)[0].status_code, 422)
        r, p = self.send("POST", "/account/password", {"currentPassword": "bad", "newPassword": "Newpass123!"}, token=token)
        self.assertEqual((r.status_code, p["code"]), (400, "invalid_password"))
        r, p = self.send("POST", "/account/password", {"currentPassword": "Secret123", "newPassword": "short"}, token=token)
        self.assertEqual(r.status_code, 422)
        r, p = self.send("POST", "/account/password", {"currentPassword": "Secret123", "newPassword": "Newpass123"}, token=token)
        self.assertEqual((r.status_code, list(p["fields"])), (422, ["newPassword"]))  # no special character
        r, _p = self.send("POST", "/account/password", {"currentPassword": "Secret123", "newPassword": "Newpass123!"}, token=token)
        self.assertEqual(r.status_code, 204)
        self.assertIn("refresh_token", r.cookies)

    def test_contract_edges_sources_stock_reference_cache(self):
        token = self.token()
        self.part.leaders_source_id = self.env.ref("leaders_marketplace.leaders_source_china")
        self.part.leaders_source_ids = [(6, 0, [self.env.ref("leaders_marketplace.leaders_source_turkey").id])]
        self.env.flush_all()
        _s, part = self.get("/products/%s" % self.part.leaders_slug)
        self.assertEqual(part["sources"], ["chinese"])  # one source per product (guide p.10, p.14)
        _s, found = self.get("/products?kind=part&source=turkish")
        self.assertEqual(found["totalCount"], 0)  # the hidden "order sources" field is not used any more
        _s, found = self.get("/products?kind=part&source=chinese")
        self.assertEqual(found["totalCount"], 1)
        r, err = self.send("POST", "/cart/items", {"productId": str(self.part.id), "options": {"source": "egyptian"}})
        self.assertEqual(r.status_code, 422)
        # out of stock: flagged in the product, refused in the cart
        self.part.leaders_out_of_stock = True
        self.env.flush_all()
        _s, part = self.get("/products/%s" % self.part.leaders_slug)
        self.assertIs(part["inStock"], False)
        r, err = self.send("POST", "/cart/items", {"productId": str(self.part.id)})
        self.assertEqual((r.status_code, err["code"]), (409, "out_of_stock"))
        # purchases carry an SF- reference
        order = self.env["sale.order"].create({"partner_id": self.user.partner_id.id, "order_line": [
            (0, 0, {"product_id": self.part.product_variant_id.id, "product_uom_qty": 2})]})
        self.env.flush_all()
        _s, orders = self.get("/account/orders?kind=purchase", token)
        self.assertRegex(orders["items"][0]["reference"], r"^SF-\d{4}$")
        self.assertEqual((orders["items"][0]["quantity"], orders["items"][0]["status"]), (2, "pending"))
        r = self.opener.get(self.base_url() + "/categories")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")  # development switch, on after install
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.api_no_cache", "0")
        r = self.opener.get(self.base_url() + "/categories")
        self.assertIn("max-age=300", r.headers.get("Cache-Control", ""))

    def test_protected_endpoints_refuse_missing_or_forged_tokens(self):
        protected = [("GET", "/account/profile"), ("PATCH", "/account/profile"), ("GET", "/account/orders?kind=quote"),
                     ("GET", "/account/notifications"), ("PUT", "/account/notifications"),
                     ("POST", "/account/password"), ("POST", "/cart/merge")]
        for method, path in protected:
            for token in (None, "garbage", self.token() + "x"):
                r, body = self.send(method, path, {}, token=token)
                self.assertEqual((r.status_code, body["code"]), (401, "session_expired"), (method, path, token))
        # a stale token is refused even where a bearer is optional
        r, body = self.send("GET", "/cart", token="garbage")
        self.assertEqual(r.status_code, 401)
        # one user never sees another user's requests
        other = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Other2", "login": "other2@example.com", "email": "other2@example.com"})
        self.env["leaders.customer.request"].create({"kind": "quote", "partner_id": other.partner_id.id, "title": "Secret"})
        self.env.flush_all()
        _s, mine = self.get("/account/orders?kind=quote", self.token())
        self.assertNotIn("Secret", [i["title"] for i in mine["items"]])

    def test_source_and_energy_are_managed_lists(self):
        Source = self.env["leaders.source"]
        india = Source.create({"name": "India", "code": "india", "api_code": "indian", "aliases": "indian,هندي,الهند"})
        self.part.leaders_source_id = india
        self.env.flush_all()
        # a new source needs no code: filter, facet and cart accept it at once
        _s, found = self.get("/products?kind=part&source=indian")
        self.assertEqual(found["totalCount"], 1)
        self.assertIn({"value": "indian", "count": 1}, self.get("/products/facets?kind=part")[1]["sources"])
        self.assertEqual(self.get("/products?kind=part&source=martian")[0], 422)
        # the importer resolves any spelling through code, alias or name
        self.assertEqual(Source.leaders_find("الهند"), india)
        self.assertEqual(Source.leaders_find(" Turkish "), self.env.ref("leaders_marketplace.leaders_source_turkey"))
        self.assertFalse(Source.leaders_find("Mars"))
        # used entries cannot be deleted
        with self.assertRaises(UserError):
            self.env.ref("leaders_marketplace.leaders_source_china").unlink()
        diesel = self.env.ref("leaders_marketplace.leaders_energy_diesel")
        self.assertEqual(self.env["leaders.energy.source"].leaders_find("ديزل"), diesel)
        self.assertEqual(self.get("/products?kind=power&powerSource=steam")[0], 422)

    def test_shipped_categories_get_arabic_names_when_the_language_is_added(self):
        self.env["res.lang"]._activate_lang("ar_001")
        generators = self.env.ref("leaders_marketplace.leaders_categ_generators")
        diesel = self.env.ref("leaders_marketplace.leaders_energy_diesel")
        self.assertEqual(generators.with_context(lang="ar_001").name, generators.with_context(lang="en_US").name)
        self.env["leaders.catalog.seed"].load_arabic_names()
        self.assertEqual(generators.with_context(lang="ar_001").name, "مولدات كهربائية")
        self.assertEqual(diesel.with_context(lang="ar_001").name, "ديزل")
        # an admin rename is never overwritten
        generators.with_context(lang="ar_001").name = "مولدات"
        self.env["leaders.catalog.seed"].load_arabic_names()
        self.assertEqual(generators.with_context(lang="ar_001").name, "مولدات")

    def test_shipped_tree_is_complete(self):
        refs = {"parts": ["spare", "oils", "filters"], "power": ["generators", "light_towers", "load_banks"],
                "equipment": ["loaders", "forklifts", "welders"], "services": ["maintenance", "inspection", "solar"]}
        for top, subs in refs.items():
            parent = self.env.ref("leaders_marketplace.leaders_categ_" + top)
            self.assertFalse(parent.parent_id)
            for sub in subs:
                self.assertEqual(self.env.ref("leaders_marketplace.leaders_categ_" + sub).parent_id, parent)
        self.assertEqual(self.env["leaders.source"].search_count([]), 4)
        self.assertEqual(self.env["leaders.energy.source"].search_count([]), 5)


class TestStorefrontBuyListingAndPayment(TestStorefrontApi):
    """``listing=buy`` skips rent-only categories; ``paymentUrl`` returns to the storefront pages."""

    def test_listing_buy_excludes_rent_only_categories(self):
        self.gen.leaders_can_buy = False
        self.env.flush_all()
        _s, bought = self.get("/products?kind=power&listing=buy")
        self.assertNotIn(str(self.eq.id), [i["id"] for i in bought["items"]])
        _s, rented = self.get("/products?kind=power&listing=rent")
        self.assertIn(str(self.eq.id), [i["id"] for i in rented["items"]])

    def test_payment_url_and_return_pages(self):
        cairo = self.env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = self.env["leaders.vendor.delivery.zone"]
        zone = Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)])
        if not zone:
            zone = Zone.create({"is_platform": True, "state_id": cairo.id, "price": 50})
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "city": "nasrCity", "address": "1 St"}
        item = {"productId": str(self.part.id), "quantity": 1, "options": {}, "unitPrice": 32000}
        token = self.token()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.online_payments", "on")
        r, placed = self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": "card"}, token=token,
                              headers={"Accept-Language": "ar"})
        self.assertEqual(r.status_code, 201, placed)
        self.assertIn("/orders/%s/payment?locale=ar" % placed["reference"], placed["paymentUrl"])
        r = self.opener.get(self.base_url() + "/orders/%s/payment/return?locale=ar" % placed["reference"], allow_redirects=False)
        self.assertEqual(r.headers["Location"], "https://spark-fu.com/ar/checkout/payment")  # not paid yet
        order = self.env["sale.order"].search([("leaders_public_ref", "=", placed["reference"])])
        self.assertEqual(order.amount_total, 320.0 + zone.price)  # goods + delivery, no tax: what the API shows
        self.assertTrue(order.require_payment)
        _s, details = self.get("/orders/%s" % placed["reference"], token=token)
        self.assertEqual(details["totals"], {"subtotal": 32000, "shipping": int(zone.price * 100), "discount": 0, "total": 32000 + int(zone.price * 100)})
        self.assertEqual(len(details["lines"]), 1)  # the delivery line is the shipping total, not a product line
        tx = self.env["payment.transaction"].create({
            "provider_id": self.env["payment.provider"].search([], limit=1).id,
            "payment_method_id": (self.env["payment.method"].search([], limit=1)
                                  or self.env["payment.method"].create({"name": "Test", "code": "testm"})).id, "amount": order.amount_total,
            "currency_id": order.currency_id.id, "partner_id": order.partner_id.id, "reference": "T-" + placed["reference"],
            "sale_order_ids": [(6, 0, order.ids)], "state": "done"})
        r = self.opener.get(self.base_url() + "/orders/%s/payment/return?locale=en" % placed["reference"], allow_redirects=False)
        self.assertEqual(r.headers["Location"], "https://spark-fu.com/en/orders/%s/confirmed" % placed["reference"])
        self.assertEqual(tx.landing_route, "/orders/%s/payment/return?locale=ar" % placed["reference"])

    def test_order_keeps_quote_lines_in_the_cart(self):
        cairo = self.env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = self.env["leaders.vendor.delivery.zone"]
        if not Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]):
            Zone.create({"is_platform": True, "state_id": cairo.id, "price": 50})
        token = self.token()
        unpriced = self.part.copy({"name": "Unpriced part", "list_price": 0, "marketplace_state": "published",
                                   "is_published": True})
        for product in (self.part, unpriced):
            r, _b = self.send("POST", "/cart/items", {"productId": str(product.id), "quantity": 1}, token=token)
            self.assertEqual(r.status_code, 200)
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "city": "nasrCity", "address": "1 St"}
        item = {"productId": str(self.part.id), "quantity": 1, "options": {}, "unitPrice": 32000}
        r, placed = self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": "cod"}, token=token)
        self.assertEqual(r.status_code, 201, placed)
        _s, cart = self.get("/cart", token=token)
        self.assertEqual([line["product"]["id"] for line in cart["lines"]], [str(unpriced.id)])

    def test_delivery_free_threshold_minimum_order_and_subcategory_surcharge(self):
        cairo = self.env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = self.env["leaders.vendor.delivery.zone"]
        zone = Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]) or Zone.create(
            {"is_platform": True, "state_id": cairo.id, "price": 50})
        zone.write({"price": 50, "free_shipping_threshold": 1000, "minimum_order": 400})
        self.filters.leaders_delivery_surcharge = 30
        regions = {r["governorate"]: r for r in self.get("/delivery/regions")[1]}
        self.assertEqual((regions["cairo"]["fee"], regions["cairo"]["freeShippingThreshold"], regions["cairo"]["minimumOrder"]),
                         (5000, 100000, 40000))
        tiles = {t["slug"]: t for t in self.get("/categories")[1]}
        self.assertEqual(tiles["filters"]["deliverySurcharge"], 3000)
        self.assertNotIn("deliverySurcharge", tiles["oils"])
        token = self.token()
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "city": "nasrCity", "address": "1 St"}

        def order(qty):
            item = {"productId": str(self.part.id), "quantity": qty, "options": {}, "unitPrice": 32000}
            return self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": "cod"}, token=token)
        r, body = order(1)  # 320 < minimum 400
        self.assertEqual((r.status_code, body["code"]), (422, "validation_error"))
        self.assertIn("400", body["fields"]["items"])
        r, placed = order(2)  # 640: governorate 50 + filters surcharge 30
        self.assertEqual(r.status_code, 201, placed)
        sale = self.env["sale.order"].search([], order="id desc", limit=1)
        self.assertEqual(sale.leaders_sf_delivery_fee, 80)
        r, placed = order(4)  # 1280 >= 1000: free delivery
        self.assertEqual(self.env["sale.order"].search([], order="id desc", limit=1).leaders_sf_delivery_fee, 0)

    def test_one_card_per_seller_with_its_quantity_and_other_sellers_when_short(self):
        env = self.env
        self.part.is_storable = True
        product = self.part.product_variant_id

        def seller(name, price, qty):
            vendor = env["res.partner"].create({"name": name, "is_marketplace_vendor": True, "vendor_state": "approved",
                                                "vendor_store_name": name + " Store"})
            vendor._marketplace_ensure_vendor_stock()
            offer = env["leaders.marketplace.offer"].create({
                "product_tmpl_id": self.part.id, "product_id": product.id, "vendor_id": vendor.id,
                "seller_sku": name, "price": price, "fulfillment_type": "fbm",
                "stock_location_id": vendor._marketplace_stock_location("fbm").id})
            env["stock.quant"]._leaders_adjust_vendor_stock(product, vendor.vendor_location_id, vendor, qty)
            offer.action_approve()
            return offer
        a, b = seller("SellerA", 300, 3), seller("SellerB", 310, 7)
        env.flush_all()
        _s, detail = self.get("/products/%s" % self.part.leaders_slug)
        cards = {c["seller"]: c for c in detail["offers"]}
        self.assertEqual((cards["SellerA Store"]["unitPrice"], cards["SellerA Store"]["available"]), (30000, 3))
        self.assertEqual(cards["SellerB Store"]["available"], 7)
        token = self.token()
        r, body = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 5,
                                                    "options": {"offerId": str(a.id)}}, token=token)
        self.assertEqual((r.status_code, body["code"], body["available"]), (409, "insufficient_stock", 3))
        self.assertEqual([(o["id"], o["available"]) for o in body["alternatives"]], [(str(b.id), 7)])
        r, cart = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 2,
                                                    "options": {"offerId": str(a.id)}}, token=token)
        self.assertEqual(r.status_code, 200, cart)
        line = cart["lines"][0]
        self.assertEqual((line["unitPrice"], line["options"]["offerId"]), (30000, str(a.id)))
        cairo = env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = env["leaders.vendor.delivery.zone"]
        if not Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]):
            Zone.create({"is_platform": True, "state_id": cairo.id, "price": 50})
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "city": "nasrCity", "address": "1 St"}
        item = {"productId": str(self.part.id), "quantity": 2, "options": {"offerId": str(a.id)}, "unitPrice": 30000}
        r, placed = self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": "cod"}, token=token)
        self.assertEqual(r.status_code, 201, placed)
        sale = env["sale.order"].search([], order="id desc", limit=1)
        self.assertEqual(sale.order_line.filtered(lambda l: not l.is_delivery).marketplace_offer_id, a)
        self.assertFalse(self.get("/cart", token=token)[1]["lines"])  # the ordered line left the cart
        item["quantity"] = 4
        r, body = self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": "cod"}, token=token)
        self.assertEqual((r.status_code, body["code"]), (409, "insufficient_stock"))
        self.assertIn("items.0.quantity", body["fields"])

    def test_spare_part_is_returned_when_it_does_not_match_and_was_not_installed(self):
        env = self.env
        self.part.is_storable = True
        product = self.part.product_variant_id
        vendor = env["res.partner"].create({"name": "RetSeller", "is_marketplace_vendor": True, "vendor_state": "approved"})
        vendor._marketplace_ensure_vendor_stock()
        offer = env["leaders.marketplace.offer"].create({
            "product_tmpl_id": self.part.id, "product_id": product.id, "vendor_id": vendor.id, "seller_sku": "RET-1",
            "price": 300, "fulfillment_type": "fbm", "stock_location_id": vendor._marketplace_stock_location("fbm").id})
        env["stock.quant"]._leaders_adjust_vendor_stock(product, vendor.vendor_location_id, vendor, 5)
        offer.action_approve()
        cairo = env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = env["leaders.vendor.delivery.zone"]
        if not Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]):
            Zone.create({"is_platform": True, "state_id": cairo.id, "price": 50})
        token = self.token()
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "city": "nasrCity", "address": "1 St"}
        item = {"productId": str(self.part.id), "quantity": 2, "options": {"offerId": str(offer.id)}, "unitPrice": 30000}
        r, placed = self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": "cod"}, token=token)
        self.assertEqual(r.status_code, 201, placed)
        sale = env["sale.order"].search([("leaders_public_ref", "=", placed["reference"])])
        sale.action_confirm()
        self.assertNotIn("returnableUntil", self.get("/orders/%s" % placed["reference"], token)[1])  # not shipped yet
        for group in sale.vendor_order_ids.fulfillment_group_ids:
            group.tracking_ref = "TRK"
            group.action_ship()
        self.env.flush_all()
        shipped = max(sale.vendor_order_ids.mapped("date_shipped"))
        _s, detail = self.get("/orders/%s" % placed["reference"], token)
        self.assertEqual(detail["returnableUntil"], (shipped + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"))

        _s, reasons = self.get("/returns/reasons")
        self.assertNotIn(str(env.ref("leaders_marketplace.return_reason_changed_mind").id), [x["id"] for x in reasons])
        damaged = str(env.ref("leaders_marketplace.return_reason_damaged").id)
        path = "/orders/%s/returns" % placed["reference"]
        photo = {"name": "p.png", "data": base64.b64encode(PNG).decode()}
        good = {"items": [{"productId": str(self.part.id), "quantity": 1}], "reasonId": damaged,
                "notInstalled": True, "photos": [photo], "comment": "Cracked housing"}
        r, body = self.send("POST", path, dict(good, notInstalled=False), token=token)
        self.assertEqual((r.status_code, set(body["fields"])), (422, {"notInstalled"}))
        mind = str(env.ref("leaders_marketplace.return_reason_changed_mind").id)
        r, body = self.send("POST", path, dict(good, reasonId=mind), token=token)
        self.assertIn("reasonId", body["fields"])
        r, body = self.send("POST", path, dict(good, items=[{"productId": str(self.eq.id), "quantity": 1}]), token=token)
        self.assertIn("items.0", body["fields"])  # equipment is not returned on the storefront
        r, body = self.send("POST", path, good, token=token)
        self.assertEqual(r.status_code, 201, body)
        ret = env["leaders.vendor.return"].search([("name", "=", body["returns"][0]["reference"])])
        self.assertEqual((ret.leaders_not_installed, len(ret.photo_ids), ret.line_ids.quantity), (True, 1, 1))
        _s, listed = self.get(path, token=token)
        self.assertEqual([x["reference"] for x in listed], [ret.name])

    def test_vendor_edit_of_a_live_listing_waits_for_approval_and_stays_live(self):
        env = self.env
        env.user.groups_id |= env.ref("leaders_marketplace.group_marketplace_reviewer")
        product = self.part.product_variant_id
        vendor = env["res.partner"].create({"name": "EditSeller", "is_marketplace_vendor": True, "vendor_state": "approved"})
        vendor._marketplace_ensure_vendor_stock()
        offer = env["leaders.marketplace.offer"].create({
            "product_tmpl_id": self.part.id, "product_id": product.id, "vendor_id": vendor.id, "seller_sku": "ED-1",
            "price": 300, "fulfillment_type": "fbm", "stock_location_id": vendor._marketplace_stock_location("fbm").id})
        offer.action_approve()
        Change = env["leaders.change.request"]
        old_name = self.part.name
        req = Change.leaders_propose(offer, {"price": 350, "seller_sku": "ED-1"}, {"name": "Renamed filter"})
        self.assertEqual(req.state, "pending")
        self.assertIn("300", req.summary)
        self.assertIn("350", req.summary)
        self.assertNotIn("Seller SKU", req.summary)  # unchanged values are not proposed
        self.assertEqual((offer.price, self.part.name, self.part.marketplace_state), (300, old_name, "published"))
        _s, detail = self.get("/products/%s" % self.part.leaders_slug)
        self.assertEqual({o["unitPrice"] for o in detail["offers"] if o["id"] == str(offer.id)}, {30000})  # still live
        self.assertFalse(Change.leaders_propose(offer, {"price": 300}, {}))  # nothing changes: no request
        with self.assertRaises(UserError):
            req.action_reject()  # a reason is required
        req.action_approve()
        self.assertEqual((offer.price, self.part.name, self.part.marketplace_state, req.state),
                         (350, "Renamed filter", "published", "approved"))

    def test_rent_in_the_platform_name_with_margin_contract_and_asset_state(self):
        env = self.env
        params = env["ir.config_parameter"].sudo()
        params.set_param("leaders_marketplace.rent_margin_percent", "50")
        params.set_param("leaders_marketplace.rent_margin_min", "0")
        self.assertEqual(self.eq.leaders_rent_price_day, 2000)
        self.assertEqual(self.eq._leaders_rent_customer_price(), 3000)  # vendor 2000 + 50 %
        self.gen.leaders_rent_margin_percent = 20                         # the subcategory overrides the platform
        self.assertEqual(self.eq._leaders_rent_customer_price(), 2400)
        _s, detail = self.get("/products/%s" % self.eq.leaders_slug)
        self.assertEqual((detail["rentPrice"], detail["rentSeller"]), (240000, env.company.name))
        # long rentals: used machines leave the results
        _s, page = self.get("/products?kind=power&listing=rent&rentMonths=30&page=1&pageSize=48")
        self.assertNotIn(str(self.eq.id), [i["id"] for i in page["items"]])  # self.eq is used
        # the rental contract must be accepted once it is published
        page = env["leaders.site.page"].sudo().search([("slug", "=", "rent-contract")]) or \
            env["leaders.site.page"].sudo().create({"slug": "rent-contract", "title": "Rental contract",
                                                    "content": "<p>Fuel, hours, late return...</p>"})
        page.published = True
        cairo = env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = env["leaders.vendor.delivery.zone"]
        if not Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]):
            Zone.create({"is_platform": True, "state_id": cairo.id, "price": 50})
        token = self.token()
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "address": "1 St"}
        book = {"productId": str(self.eq.id), "listing": "rent", "rentPeriod": "week", "rentUnits": 1, "shipping": ship}
        r, body = self.send("POST", "/equipment-requests", book, token=token)
        self.assertEqual((r.status_code, set(body.get("fields", {}))), (422, {"contractAccepted"}))
        r, placed = self.send("POST", "/equipment-requests", dict(book, contractAccepted=True), token=token)
        self.assertEqual((r.status_code, placed["kind"], placed["status"]), (201, "rent", "requested"), placed)
        self.assertNotIn("paymentUrl", placed)  # no delivery asked: nothing to pay at booking
        self.assertEqual(placed["rental"]["pricePerDay"], 240000)
        req = env["leaders.customer.request"].search([("name", "=", placed["reference"])])
        rental = req.rental_id
        self.assertEqual((rental.supplier_price_day, rental.customer_price_day, rental.state), (2000, 2400, "reserved"))
        self.assertTrue(rental.contract_accepted_on)
        self.assertEqual(self.eq.leaders_asset_state, "reserved")
        self.eq.leaders_service_ids = env["product.template"].search([("leaders_service_page", "=", True),
                                                                      ("leaders_slug", "=", "delivery")])
        later = str(fields.Date.today() + timedelta(days=30))
        r, second = self.send("POST", "/equipment-requests", dict(book, contractAccepted=True, startDate=later,
                                                                  services=["delivery"]), token=token)
        self.assertEqual((r.status_code, second["status"], second["amountDue"]), (201, "awaitingPayment", 5000), second)
        self.assertIn("/orders/", second["paymentUrl"])
        r, clash = self.send("POST", "/equipment-requests", dict(book, contractAccepted=True), token=token)
        self.assertEqual(r.status_code, 422)  # already rented over these dates
        env.user.groups_id |= env.ref("leaders_marketplace.group_marketplace_manager")
        rental.handover_photo_ids = [(0, 0, {"name": "h.png", "datas": base64.b64encode(PNG)})]
        rental.action_hand_over()
        self.assertEqual(self.eq.leaders_asset_state, "rented")
        self.assertEqual(self.get("/products/%s" % self.eq.leaders_slug)[0], 404)  # rented: off the storefront
        self.assertEqual(rental._leaders_vendor_view()[0]["area"], cairo.name)
        self.assertNotIn("renter", str(rental._leaders_vendor_view()))
        rental.return_photo_ids = [(0, 0, {"name": "r.png", "datas": base64.b64encode(PNG)})]
        rental.action_return()
        self.assertEqual(self.eq.leaders_asset_state, "available")
        self.assertEqual(self.get("/products/%s" % self.eq.leaders_slug)[0], 200)
        self.assertIn("rented", self.eq.leaders_asset_move_ids.mapped("state"))

    def test_equipment_is_ordered_with_a_request_not_the_cart(self):
        env = self.env
        token = self.token()
        r, body = self.send("POST", "/cart/items", {"productId": str(self.eq.id), "quantity": 1}, token=token)
        self.assertEqual((r.status_code, body["code"]), (409, "equipment_request_only"))
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo"}
        # priced, no service: a purchase request, nothing to pay
        r, plain = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": ship}, token=token)
        self.assertEqual((r.status_code, plain["kind"], plain["status"], plain["unitPrice"]), (201, "purchase", "requested", 185000000))
        self.assertNotIn("paymentUrl", plain)
        # priced, with services: only the services chosen on the product are accepted
        services = env["product.template"].search([("leaders_service_page", "=", True),
                                                   ("leaders_slug", "in", ("installation", "delivery"))])
        r, body = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": ship,
                                                             "services": ["installation"]}, token=token)
        self.assertIn("services", body["fields"])  # none chosen on the product yet
        self.eq.leaders_service_ids = services
        _s, detail = self.get("/products/%s" % self.eq.leaders_slug)
        self.assertEqual({s["slug"] for s in detail["services"]}, {"installation", "delivery"})
        r, serv = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": ship,
                                                             "services": ["installation", "delivery"]}, token=token)
        self.assertEqual(serv["status"], "requested")
        req = env["leaders.customer.request"].search([("name", "=", serv["reference"])])
        req.services_amount = 7500
        req.action_send_service_prices()
        _s, serv = self.get("/equipment-requests/%s" % serv["reference"], token=token)
        self.assertEqual((serv["status"], serv["servicesPrice"], serv["amountDue"]), ("awaitingPayment", 750000, 750000))
        req.payment_order_id.action_confirm()
        self.assertEqual(self.get("/equipment-requests/%s" % serv["reference"], token=token)[1]["status"], "confirmed")
        # no price: a quote request
        r, quote = self.send("POST", "/equipment-requests", {"productId": str(self.quote_only.id), "shipping": ship}, token=token)
        self.assertEqual((r.status_code, quote["kind"]), (201, "quote"))
        # spare parts keep the cart
        r, body = self.send("POST", "/equipment-requests", {"productId": str(self.part.id), "shipping": ship}, token=token)
        self.assertEqual(r.status_code, 422)

    def test_inspection_cost_is_in_the_price_and_included_maintenance_is_free(self):
        env = self.env
        before = self.eq.leaders_display_price
        self.eq.sudo().leaders_inspection_cost = 1500
        self.assertEqual(self.eq.leaders_display_price, before + 1500)
        _s, detail = self.get("/products/%s" % self.eq.leaders_slug)
        self.assertEqual(detail["price"], int(round((before + 1500) * 100)))
        self.assertNotIn("inspection", json.dumps(detail).lower().replace("inspected", ""))  # never shown as a fee
        # a renter whose contract includes maintenance asks for maintenance: free
        renter = self.user.partner_id.commercial_partner_id
        rental = env["leaders.rental"].create({
            "product_tmpl_id": self.eq.id, "partner_id": renter.id, "date_start": fields.Date.today(),
            "date_end": fields.Date.today() + timedelta(days=30), "maintenance_included": True})
        rental.state = "with_customer"
        req = env["leaders.customer.request"].create({"kind": "service", "service": "maintenance", "title": "Maintenance",
                                                      "partner_id": renter.id})
        req._leaders_link_included_maintenance()
        self.assertEqual(req.included_rental_id, rental)

    def test_category_tiles_carry_name_description_picture_and_icon(self):
        seed = self.env["leaders.catalog.seed"]
        seed._load_demo_categories()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.api_no_cache", "0")
        self.env.flush_all()
        r = self.opener.get(self.base_url() + "/categories")
        self.assertEqual(r.headers["Cache-Control"], "public, max-age=300")
        tiles = {t["slug"]: t for t in json.loads(r.content)}
        generators = tiles["generators"]
        self.assertEqual(generators["name"], "Electric Generators")
        self.assertTrue(generators["description"])
        self.assertIn("/web/image/product.public.category/%s/leaders_icon" % generators["id"], generators["icon"])
        self.assertIn("/web/image/product.public.category/%s/image_1024" % generators["id"], generators["image"])
        self.assertEqual(self.opener.get(generators["icon"].replace("https://odooapp.mdarj.org", self.base_url())).status_code, 200)

    def test_custom_rent_period_is_quote_only(self):
        token = self.token()
        r, cart = self.send("POST", "/cart/items", {"productId": str(self.eq.id), "quantity": 1,
                                                    "options": {"listing": "rent", "rentPeriod": "custom"}}, token=token)
        self.assertEqual(r.status_code, 409, cart)  # equipment (bought or rented) is ordered with a request
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "city": "nasrCity", "address": "1 St"}
        item = {"productId": str(self.eq.id), "quantity": 1, "options": {"listing": "rent", "rentPeriod": "custom"}, "unitPrice": 200000}
        r, _b = self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": "cod"}, token=token)
        self.assertEqual(r.status_code, 422)


@tagged("post_install", "-at_install")
class TestStorefrontSearch(HttpCase):
    """``q`` on GET /products and /products/facets: words, Arabic, specs, categories, limit."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env["product.template"].search([("leaders_kind", "!=", False)]).write({"is_published": False})
        env["res.lang"]._activate_lang("ar_001")
        cls.loaders = env.ref("leaders_marketplace.leaders_categ_loaders")
        cls.welders = env.ref("leaders_marketplace.leaders_categ_welders")
        cls.gen = env.ref("leaders_marketplace.leaders_categ_generators")
        for categ, name in ((cls.loaders, "لودرات"), (cls.welders, "ماكينات لحام"), (cls.gen, "مولدات كهربائية")):
            categ.with_context(lang="ar_001").name = name
        lincoln = env["leaders.brand"].create({"name": "SrchLincoln"})
        common = {"leaders_source_id": env.ref("leaders_marketplace.leaders_source_original").id,
                  "leaders_condition": "new", "marketplace_state": "published", "is_published": True}

        def make(name, categ, model, **vals):
            tmpl = env["product.template"].create(dict(common, name=name, public_categ_ids=[(6, 0, categ.ids)],
                                                       leaders_brand_id=lincoln.id,
                                                       leaders_model_id=env["leaders.equipment.model"].create({"name": model}).id,
                                                       **vals))
            return tmpl
        cls.ranger = make("Ranger welder", cls.welders, "Ranger 500")
        cls.bobcat = make("Bobcat welder", cls.welders, "Bobcat 250", leaders_capacity=250, leaders_capacity_unit="amp")
        cls.loader = make("Wheel loader", cls.loaders, "WA320-8", leaders_rentable=True, leaders_rent_price_day=900,
                          description_sale="Front loader for quarries")
        cls.loader.with_context(lang="ar_001").write({"name": "لودر عجل", "description_sale": "لُودر أمامي للمَحاجر"})
        cls.generator = make("Diesel generator", cls.gen, "G-100", leaders_capacity=100, leaders_capacity_unit="kva")
        env.flush_all()

    def search(self, path, **params):
        r = self.url_open(path + "?" + urlencode(params))
        return r.status_code, json.loads(r.content)

    def names(self, **params):
        status, body = self.search("/products", page=1, pageSize=48, **params)
        self.assertEqual(status, 200, body)
        return {item["model"] for item in body["items"]}

    def test_every_word_must_match_in_any_field(self):
        self.assertEqual(self.names(kind="equipment", q="SrchLincoln Ranger"), {"Ranger 500"})
        self.assertEqual(self.names(kind="equipment", q="  Ranger   500 "), {"Ranger 500"})
        self.assertEqual(self.names(kind="equipment", q="wa320"), {"WA320-8"})
        self.assertEqual(self.names(kind="equipment", q="SrchLincoln nothing-like-this"), set())

    def test_q_alone_searches_every_kind(self):
        self.assertEqual(self.names(q="SrchLincoln"), {"Ranger 500", "Bobcat 250", "WA320-8", "G-100"})
        self.assertEqual(self.names(q="SrchLincoln G-100"), {"G-100"})
        self.assertEqual(self.search("/products", q="   ")[0], 422)  # blank q is still a listing without a kind
        self.assertEqual(self.search("/products")[0], 422)

    def test_similar_names_are_searched_but_not_shown(self):
        self.ranger.leaders_similar_names = "ماكينة لحام ديزل, engine welder"
        self.assertEqual(self.names(kind="equipment", q="engine welder"), {"Ranger 500"})
        self.assertEqual(self.names(kind="equipment", q="ماكينه لحام ديزل"), {"Ranger 500"})  # normalised
        status, body = self.search("/products", kind="equipment", q="engine welder", page=1, pageSize=8)
        self.assertNotIn("engine welder", json.dumps(body))

    def test_specs_values_with_their_unit_and_arabic_digits(self):
        self.assertEqual(self.names(kind="equipment", q="250 A"), {"Bobcat 250"})
        self.assertEqual(self.names(kind="equipment", q="٢٥٠"), {"Bobcat 250"})
        self.assertEqual(self.names(kind="power", q="100 kVA"), {"G-100"})

    def test_arabic_names_descriptions_and_categories(self):
        self.assertEqual(self.names(kind="equipment", q="لودر"), {"WA320-8"})
        self.assertEqual(self.names(kind="equipment", q="loaders"), {"WA320-8"})
        self.assertEqual(self.names(kind="equipment", q="مَحاجِر"), {"WA320-8"})  # tashkeel stripped both sides
        self.assertEqual(self.names(kind="equipment", q="quarries"), {"WA320-8"})
        self.assertEqual(self.names(kind="power", q="مولد"), {"G-100"})
        self.assertEqual(self.names(kind="power", q="مُولّد"), {"G-100"})
        self.assertEqual(self.names(kind="equipment", q="لحام"), {"Ranger 500", "Bobcat 250"})

    def test_q_combines_with_the_other_filters(self):
        self.assertEqual(self.names(kind="equipment", q="SrchLincoln", listing="rent"), {"WA320-8"})
        self.assertEqual(self.names(kind="equipment", q="SrchLincoln", subcategory="welding-machine"),
                         {"Ranger 500", "Bobcat 250"})
        self.assertEqual(self.names(kind="equipment", q="لودر", condition="used"), set())

    def test_no_match_is_an_empty_page_not_an_error(self):
        status, body = self.search("/products", kind="equipment", q="zzzz-no-such-thing", page=1, pageSize=8)
        self.assertEqual((status, body["items"], body["totalCount"]), (200, [], 0))

    def test_facets_use_the_same_search(self):
        status, body = self.search("/products/facets", kind="equipment", q="لودر")
        self.assertEqual(status, 200)
        self.assertEqual(body["brands"], [{"value": "SrchLincoln", "count": 1}])
        self.assertEqual(body["sections"], [{"value": "loader", "count": 1}])

    def test_q_longer_than_100_characters_is_refused(self):
        self.assertEqual(self.search("/products", kind="equipment", q="x" * 100)[0], 200)
        status, body = self.search("/products", kind="equipment", q="x" * 101)
        self.assertEqual((status, body["code"]), (422, "validation_error"))
        self.assertIn("q", body["fields"])
        r = self.url_open("/products?" + urlencode({"kind": "equipment", "q": "x" * 101}),
                          headers={"Accept-Language": "ar"})
        self.assertIn("100", json.loads(r.content)["fields"]["q"])
        self.assertNotIn("characters", json.loads(r.content)["fields"]["q"])


@tagged("post_install", "-at_install")
class TestStorefrontFilters(HttpCase):
    """GET /products/filters (ids, labels, counts) and the id filters on GET /products."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env["product.template"].search([("leaders_kind", "!=", False)]).write({"is_published": False})
        env["res.lang"]._activate_lang("ar_001")
        ref = env.ref
        cls.equipment, cls.loaders, cls.welders = (ref("leaders_marketplace.leaders_categ_equipment"),
                                                  ref("leaders_marketplace.leaders_categ_loaders"),
                                                  ref("leaders_marketplace.leaders_categ_welders"))
        cls.gen, cls.spare = ref("leaders_marketplace.leaders_categ_generators"), ref("leaders_marketplace.leaders_categ_spare")
        cls.loaders.with_context(lang="ar_001").name = "لودرات"
        cls.diesel = ref("leaders_marketplace.leaders_energy_diesel")
        cls.turkey, cls.china = ref("leaders_marketplace.leaders_source_turkey"), ref("leaders_marketplace.leaders_source_china")
        cls.jcb = env["leaders.brand"].create({"name": "FltJCB"})
        cls.esab = env["leaders.brand"].create({"name": "FltESAB"})
        cls.old = env["leaders.brand"].create({"name": "FltArchived"})
        common = {"leaders_source_id": ref("leaders_marketplace.leaders_source_original").id,
                  "marketplace_state": "published", "is_published": True}

        def make(model, categ, brand, **vals):
            return env["product.template"].create(dict(
                common, name=model, public_categ_ids=[(6, 0, categ.ids)], leaders_brand_id=brand.id,
                leaders_model_id=env["leaders.equipment.model"].create({"name": model}).id, **vals))
        make("FL-1", cls.loaders, cls.jcb, leaders_condition="used", leaders_hours=6000, leaders_energy_id=cls.diesel.id,
             leaders_inspected=True,
             leaders_rentable=True, leaders_rent_price_day=500)
        make("FL-2", cls.loaders, cls.esab, leaders_condition="new", leaders_energy_id=cls.diesel.id)
        make("FW-1", cls.welders, cls.esab, leaders_condition="used", leaders_hours=1500, leaders_inspected=True)
        make("FG-1", cls.gen, cls.jcb, leaders_condition="new")
        make("FP-1", cls.spare, cls.jcb, leaders_condition="new", leaders_part_number="FP-1",
             leaders_source_id=cls.turkey.id)
        make("FA-1", cls.welders, cls.old, leaders_condition="new")
        cls.old.active = False
        env.flush_all()

    def get(self, path, lang="en", **params):
        r = self.url_open(path + "?" + urlencode(params), headers={"Accept-Language": lang})
        return r.status_code, json.loads(r.content)

    def models(self, **params):
        status, body = self.get("/products", page=1, pageSize=48, **params)
        self.assertEqual(status, 200, body)
        return {item["model"] for item in body["items"]}

    @staticmethod
    def group(body, name):
        return {o["id"]: o["count"] for o in body[name]}

    def test_every_group_is_present_with_ids_and_labels(self):
        status, body = self.get("/products/filters", lang="ar")
        self.assertEqual(status, 200)
        # year / family / warranty (item-request contract) are optional: sent when some product has the data
        self.assertEqual(set(body) - {"year", "family", "warranty"},
                         {"categories", "listings", "conditions", "brands", "powerSources", "sources", "hours", "ranges"})
        cats = {c["id"]: c for c in body["categories"]}
        self.assertNotIn(str(self.env.ref("leaders_marketplace.leaders_categ_services").id), cats)
        loaders = cats[str(self.loaders.id)]
        self.assertEqual((loaders["label"], loaders["slug"], loaders["kind"], loaders["parentId"], loaders["count"]),
                         ("لودرات", "loader", "equipment", str(self.equipment.id), 2))
        self.assertEqual(cats[str(self.equipment.id)]["count"], 4)  # its sub-categories' products
        ids = [c["id"] for c in body["categories"]]
        self.assertLess(ids.index(str(self.equipment.id)), ids.index(str(self.loaders.id)))
        self.assertEqual([o["id"] for o in body["conditions"]], ["new", "used", "refurbished"])
        self.assertEqual(body["conditions"][1]["label"], "مستعمل")
        self.assertEqual([o["id"] for o in body["listings"]], ["buy", "rent"])
        self.assertEqual([o["id"] for o in body["hours"]], ["under1000", "from1000", "over5000"])
        self.assertEqual(self.group(body, "brands"), {str(self.esab.id): 2, str(self.jcb.id): 3})
        self.assertEqual([o["label"] for o in body["brands"]], ["FltESAB", "FltJCB"])  # alphabetical, no archived
        self.assertEqual(self.group(body, "powerSources"), {str(self.diesel.id): 2})
        self.assertEqual(self.group(body, "sources"), {str(self.turkey.id): 1})  # one source per product

    def test_parts_shelf_has_buy_only_and_no_hours(self):
        status, body = self.get("/products/filters", kind="part")
        self.assertEqual([o["id"] for o in body["listings"]], ["buy"])
        self.assertEqual((body["hours"], body["powerSources"]), ([], []))
        self.assertEqual({c["kind"] for c in body["categories"]}, {"part"})

    def test_each_group_ignores_its_own_filter(self):
        status, body = self.get("/products/filters", kind="equipment", brandId=self.jcb.id, condition="used")
        self.assertEqual(status, 200)
        self.assertEqual(self.group(body, "brands"), {str(self.jcb.id): 1, str(self.esab.id): 1})  # used only
        self.assertEqual(self.group(body, "conditions"), {"new": 0, "used": 1, "refurbished": 0})  # JCB only
        self.assertEqual(self.group(body, "hours"), {"under1000": 0, "from1000": 0, "over5000": 1})
        self.assertEqual(self.group(body, "listings"), {"buy": 1, "rent": 1})

    def test_category_id_includes_sub_categories_and_spans_shelves(self):
        self.assertEqual(self.models(categoryId=self.loaders.id), {"FL-1", "FL-2"})
        self.assertEqual(self.models(categoryId=self.equipment.id), {"FL-1", "FL-2", "FW-1", "FA-1"})
        self.assertEqual(self.models(categoryId="%s,%s" % (self.loaders.id, self.gen.id)), {"FL-1", "FL-2", "FG-1"})
        self.assertEqual(self.models(kind="equipment", subcategory="loader"), {"FL-1", "FL-2"})  # old form unchanged

    def test_brand_power_source_and_source_ids(self):
        self.assertEqual(self.models(kind="equipment", brandId=self.jcb.id), {"FL-1"})
        self.assertEqual(self.models(kind="equipment", brandId="%s,999999" % self.jcb.id), {"FL-1"})
        self.assertEqual(self.models(kind="equipment", brandId=self.jcb.id, brand="FltESAB"), {"FL-1", "FL-2", "FW-1"})
        self.assertEqual(self.models(kind="equipment", brandId=self.old.id), set())  # archived matches nothing
        self.assertEqual(self.models(kind="equipment", powerSourceId=self.diesel.id), {"FL-1", "FL-2"})
        self.assertEqual(self.models(kind="part", sourceId=self.turkey.id), {"FP-1"})
        self.assertEqual(self.models(kind="equipment", brandId=self.jcb.id, condition="new"), set())

    def test_unknown_id_is_empty_and_malformed_id_is_422(self):
        status, body = self.get("/products", kind="equipment", brandId=999999, page=1, pageSize=8)
        self.assertEqual((status, body["items"], body["totalCount"]), (200, [], 0))
        for key in ("brandId", "categoryId", "powerSourceId", "sourceId"):
            status, body = self.get("/products", kind="equipment", **{key: "JCB"})
            self.assertEqual((status, body["code"]), (422, "validation_error"))
            self.assertIn(key, body["fields"])
        status, body = self.get("/products/filters", lang="ar", brandId="JCB")
        self.assertEqual(status, 422)
        self.assertNotIn("comma", body["fields"]["brandId"])
        self.assertEqual(self.get("/products", page=1)[0], 422)  # neither kind nor categoryId

    def test_no_store(self):
        r = self.url_open("/products/filters")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_listing_searches_and_filters_are_logged(self):
        Log = self.env["leaders.search.log"]
        before = Log.search_count([])
        self.models(kind="equipment", q="FL-1", brandId="%s,%s" % (self.jcb.id, self.esab.id), condition="used")
        log = Log.search([], limit=1)
        self.assertEqual(Log.search_count([]), before + 1)
        self.assertEqual((log.query, log.kind, log.result_count, log.no_result), ("FL-1", "equipment", 1, False))
        self.assertEqual(sorted(log.filter_ids.mapped("key")), ["brandId", "brandId", "condition"])
        self.get("/products", kind="equipment", q="no-such-thing-xyz", page=1, pageSize=8)
        self.assertTrue(Log.search([], limit=1).no_result)
        self.get("/products", kind="equipment", q="FL", page=2, pageSize=1)  # turning pages is not a new search
        self.get("/products", kind="equipment", page=1, pageSize=8)          # no search word and no filter
        self.assertEqual(Log.search_count([]), before + 2)
        # a logged search answers its id (optional key); a plain listing does not
        status, body = self.get("/products", kind="equipment", q="FL", page=1, pageSize=8)
        self.assertEqual(set(body), {"items", "totalCount", "page", "pageSize", "searchId"})
        self.assertNotIn("searchId", self.get("/products", kind="equipment", page=1, pageSize=8)[1])
        self.assertNotIn("searchId", self.get("/products", kind="equipment", q="FL", page=2, pageSize=1)[1])
        # the customer opens a result
        product_id = body["items"][0]["id"]
        r = self.url_open("/products/search-click", data=json.dumps({"searchId": body["searchId"], "productId": product_id}),
                          headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 204)
        log = Log.browse(int(body["searchId"]))
        self.assertEqual((log.clicked, log.click_rate, log.click_count), (True, 100.0, 1))
        self.assertEqual(log.clicked_product_ids.ids, [int(product_id)])
        unknown = self.url_open("/products/search-click", data=json.dumps({"searchId": "999999", "productId": product_id}),
                                headers={"Content-Type": "application/json"})
        self.assertEqual(unknown.status_code, 204)  # an old or unknown search is ignored, never an error
        bad = self.url_open("/products/search-click", data=json.dumps({"searchId": "x"}),
                            headers={"Content-Type": "application/json"})
        self.assertEqual(bad.status_code, 422)

    def test_approve_selected_products_at_once(self):
        Tmpl = self.env["product.template"]
        pending = Tmpl.create([{"name": "Bulk %s" % n, "public_categ_ids": [(6, 0, self.loaders.ids)],
                                "leaders_warranty_months": 12, "leaders_warranty_provider": "vendor",
                                "marketplace_state": "pending"} for n in range(3)])
        draft = Tmpl.create({"name": "Bulk draft", "marketplace_state": "draft"})
        self.env.user.groups_id |= self.env.ref("leaders_marketplace.group_marketplace_reviewer")
        action = self.env.ref("leaders_marketplace.action_server_marketplace_approve_selected")
        action.with_context(active_model="product.template", active_ids=(pending | draft).ids).run()
        self.assertEqual(set(pending.mapped("marketplace_state")), {"published"})
        self.assertTrue(all(pending.mapped("is_published")))
        self.assertEqual(draft.marketplace_state, "draft")

    def test_used_product_waits_for_its_inspection_when_the_subcategory_asks(self):
        self.assertTrue(self.loaders.leaders_inspection_required)
        Tmpl = self.env["product.template"]
        used = Tmpl.create({"name": "Used loader", "public_categ_ids": [(6, 0, self.loaders.ids)],
                            "leaders_condition": "used", "leaders_brand_id": self.jcb.id,
                            "leaders_model_id": self.env["leaders.equipment.model"].create({"name": "INSP-1"}).id,
                            "marketplace_state": "pending"})
        self.assertTrue(used.leaders_requires_inspection)
        self.env.user.groups_id |= self.env.ref("leaders_marketplace.group_marketplace_reviewer")
        with self.assertRaises(UserError):
            used.action_marketplace_approve()
        # published before the rule (or by a direct write): hidden from the storefront until inspected
        used.write({"marketplace_state": "published", "is_published": True})
        self.assertNotIn("INSP-1", self.models(kind="equipment"))
        self.assertEqual(self.get("/products/%s" % used.leaders_slug)[0], 404)
        used.leaders_inspected = True
        self.assertIn("INSP-1", self.models(kind="equipment"))
        # a new product, or a subcategory without the setting, never waits
        new = Tmpl.create({"name": "New loader", "public_categ_ids": [(6, 0, self.loaders.ids)], "leaders_condition": "new"})
        self.assertFalse(new.leaders_requires_inspection)
        self.loaders.leaders_inspection_required = False
        other = Tmpl.create({"name": "Used, no rule", "public_categ_ids": [(6, 0, self.loaders.ids)], "leaders_condition": "used"})
        self.assertFalse(other.leaders_requires_inspection)

    def test_installation_and_delivery_services_are_listed_and_requestable(self):
        status, services = self.get("/services")
        slugs = [s["slug"] for s in services]
        self.assertTrue({"installation", "delivery", "inspection", "maintenance", "solar"} <= set(slugs), slugs)
        status, page = self.get("/services/installation", lang="ar")
        self.assertEqual(status, 200)
        self.assertEqual(page["name"], "التركيب والتشغيل")
        self.assertEqual(len(page["checklist"]), 6)
        tiles = {c["slug"]: c for c in self.get("/categories", lang="ar")[1]}
        self.assertEqual((tiles["delivery"]["name"], tiles["delivery"]["parentSlug"]), ("التوصيل والنقل", "services"))
        self.assertTrue(set(tiles["inspection"]) <= set(tiles["installation"]))  # the same keys as the other tiles

    def test_hiding_a_category_hides_its_products_and_warns_with_their_number(self):
        both = self.env["product.template"].create({
            "name": "Both shelves", "public_categ_ids": [(6, 0, (self.loaders | self.welders).ids)],
            "leaders_condition": "new", "leaders_brand_id": self.jcb.id, "marketplace_state": "published",
            "is_published": True, "leaders_model_id": self.env["leaders.equipment.model"].create({"name": "BOTH-1"}).id})
        self.assertEqual(len(self.loaders._leaders_products_hidden_by()), 2)  # FL-1, FL-2; "Both shelves" stays
        self.env.user.groups_id |= self.env.ref("leaders_marketplace.group_marketplace_manager")
        with self.assertLogs("odoo.tests.form.onchange", level="WARNING") as logs, Form(self.loaders) as form:
            form.leaders_hidden = True
        self.assertIn("2 published product(s)", "\n".join(logs.output))
        self.assertNotIn("FL-1", self.models(kind="equipment"))
        self.assertIn("BOTH-1", self.models(kind="equipment"))
        self.assertEqual(self.get("/products", categoryId=self.loaders.id, page=1, pageSize=8)[1]["totalCount"], 1)
        self.loaders.leaders_hidden = False
        self.assertIn("FL-1", self.models(kind="equipment"))

    def test_inspection_that_does_not_match_rejects_and_charges_the_vendor(self):
        self.env.user.groups_id |= self.env.ref("leaders_marketplace.group_marketplace_manager")
        vendor = self.env["res.partner"].create({"name": "Insp Vendor", "is_marketplace_vendor": True})
        Tmpl = self.env["product.template"]

        def used(name):
            return Tmpl.create({"name": name, "public_categ_ids": [(6, 0, self.loaders.ids)], "leaders_condition": "used",
                                "leaders_warranty_months": 6, "leaders_warranty_provider": "vendor",
                                "leaders_meter_photo": base64.b64encode(PNG), "leaders_plate_photo": base64.b64encode(PNG),
                                "marketplace_owner_vendor_id": vendor.id, "marketplace_state": "pending"})
        ok = used("Insp OK")
        ok.action_leaders_inspection_passed()
        self.assertEqual((ok.leaders_inspected, ok.leaders_inspection_result), (True, "passed"))
        ok.action_marketplace_approve()  # inspected: may be published
        self.assertEqual(ok.marketplace_state, "published")

        params = self.env["ir.config_parameter"].sudo()
        params.set_param("leaders_marketplace.inspection_fee_mode", "deduct")
        params.set_param("leaders_marketplace.inspection_fee_amount", "750")
        bad = used("Insp bad")
        action = bad.action_leaders_inspection_mismatch()
        wizard = self.env[action["res_model"]].with_context(action["context"]).create({"reason": "Hours are 9,800"})
        self.assertEqual((wizard.mode, wizard.amount, wizard.vendor_id), ("deduct", 750, vendor))
        wizard.action_confirm()
        self.assertEqual((bad.marketplace_state, bad.leaders_inspection_result), ("rejected", "mismatch"))
        self.assertEqual(bad.marketplace_reject_reason, "Hours are 9,800")
        tx = self.env["leaders.wallet.transaction"].search([("vendor_id", "=", vendor.id), ("type", "=", "inspection_fee")])
        self.assertEqual(tx.amount, -750)

        other = used("Insp bad 2")
        wizard = self.env["leaders.inspection.mismatch.wizard"].create({
            "product_tmpl_id": other.id, "reason": "Wrong model", "mode": "invoice", "amount": 500})
        wizard.action_confirm()
        invoice = self.env["account.move"].search([("partner_id", "=", vendor.id), ("move_type", "=", "out_invoice")])
        self.assertEqual((invoice.state, invoice.amount_untaxed), ("posted", 500))
        self.assertEqual(self.env["leaders.wallet.transaction"].search_count(
            [("vendor_id", "=", vendor.id), ("type", "=", "inspection_fee")]), 1)  # the invoice way leaves the wallet alone

    def test_equipment_needs_warranty_and_used_proof_photos_and_duplicates_are_flagged(self):
        self.env.user.groups_id |= self.env.ref("leaders_marketplace.group_marketplace_reviewer")
        Tmpl = self.env["product.template"]
        model = self.env["leaders.equipment.model"].create({"name": "DUP-9"})
        vals = {"public_categ_ids": [(6, 0, self.loaders.ids)], "leaders_condition": "used", "leaders_model_id": model.id,
                "leaders_capacity": 3, "leaders_year": 2018, "leaders_hours": 7000, "marketplace_state": "pending"}
        first = Tmpl.create(dict(vals, name="Dup A"))
        second = Tmpl.create(dict(vals, name="Dup B"))
        self.assertEqual(second.leaders_duplicate_ids, first)
        problems = second._leaders_listing_problems()
        self.assertEqual(len(problems), 2)  # warranty, proof photos
        with self.assertRaises(UserError):
            second.action_marketplace_approve()
        second.write({"leaders_warranty_months": 6, "leaders_warranty_provider": "manufacturer",
                      "leaders_meter_photo": base64.b64encode(PNG), "leaders_plate_photo": base64.b64encode(PNG),
                      "leaders_inspected": True})
        second.action_marketplace_approve()
        self.assertEqual(second.marketplace_state, "published")

    def test_broken_operation_video_links_are_flagged(self):
        tmpl = self.env["product.template"].search([("name", "=", "Both shelves")], limit=1) or self.env["product.template"].create(
            {"name": "Video check", "public_categ_ids": [(6, 0, self.loaders.ids)], "is_published": True})
        tmpl.write({"is_published": True, "leaders_video_url": "https://video.invalid/x"})
        with patch("odoo.addons.leaders_marketplace.models.equipment_extra.requests.get",
                   side_effect=__import__("requests").RequestException("down")):
            self.env["product.template"].cron_check_video_links()
        self.assertTrue(tmpl.leaders_video_broken)

    def test_join_as_a_vendor_compares_the_two_models(self):
        status, body = self.get("/vendor-join")
        self.assertEqual(status, 200)
        by = {m["code"]: m for m in body["models"]}
        self.assertEqual(set(by), {"subscription", "commission"})
        self.assertTrue(by["subscription"]["nameShown"])
        self.assertFalse(by["commission"]["nameShown"])
        self.assertEqual(body["freePeriodMonths"], 3)
        page = self.url_open("/vendor/join")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Who talks to the buyer", page.text)



@tagged("post_install", "-at_install")
class TestStorefrontProductDescription(TestStorefrontApi):
    """The product carries its description (optional), in the language asked for."""

    def test_description_is_sent_only_when_filled(self):
        status, body = self.get("/products/%s" % self.eq.leaders_slug)
        self.assertEqual(status, 200)
        self.assertNotIn("description", body)
        self.eq.description_sale = "Heavy duty generator, full service history."
        self.env.flush_all()
        status, body = self.get("/products/%s" % self.eq.leaders_slug)
        self.assertEqual(body["description"], "Heavy duty generator, full service history.")
        status, page = self.get("/products?kind=power")
        self.assertEqual([p.get("description") for p in page["items"] if p["id"] == str(self.eq.id)],
                         ["Heavy duty generator, full service history."])


@tagged("post_install", "-at_install")
class TestAccountOrdersEquipmentRequests(TestStorefrontApi):
    """Equipment requests show in the account's orders: buy requests in the purchase tab, rent requests in a rent tab."""

    def test_equipment_requests_in_the_orders_tabs(self):
        token = self.token()
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo"}
        r, placed = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": ship}, token=token)
        self.assertEqual(r.status_code, 201, placed)
        partner = self.user.partner_id
        rent = self.env["leaders.customer.request"].create({"kind": "rent", "partner_id": partner.id, "title": "Rent gen",
                                                            "quantity": 1, "product_tmpl_id": self.eq.id})
        self.env["leaders.customer.request"].create({"kind": "quote", "partner_id": partner.id, "title": "Q", "quantity": 1})
        self.env.flush_all()
        _s, buy = self.get("/account/orders?kind=purchase", token)
        row = buy["items"][0]
        self.assertEqual((buy["totalCount"], row["reference"], row["kind"], row["status"], row["source"]),
                         (1, placed["reference"], "purchase", "requested", "equipmentRequest"))
        self.assertEqual(self.get("/equipment-requests/%s" % row["reference"], token)[0], 200)
        _s, rents = self.get("/account/orders?kind=rent", token)
        self.assertEqual([(x["reference"], x["kind"], x["status"]) for x in rents["items"]],
                         [(rent.name, "rent", "requested")])
        _s, quotes = self.get("/account/orders?kind=quote", token)  # the quote tab keeps its shape
        self.assertEqual((quotes["totalCount"], quotes["items"][0]["status"]), (1, "pending"))
        self.assertNotIn("source", quotes["items"][0])


@tagged("post_install", "-at_install")
class TestEquipmentRequestCancel(TestStorefrontApi):
    """The customer cancels an equipment request before it is confirmed; the payment order and the reservation go too."""

    def _place(self, token, **extra):
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo"}
        r, body = self.send("POST", "/equipment-requests", dict({"productId": str(self.eq.id), "shipping": ship}, **extra),
                            token=token)
        self.assertEqual(r.status_code, 201, body)
        return self.env["leaders.customer.request"].search([("name", "=", body["reference"])])

    def test_cancel_a_purchase_awaiting_payment(self):
        token = self.token()
        rec = self._place(token)
        rec.payment_order_id = rec._leaders_payment_order("Services", 500)
        rec.state = "offered"
        self.env.flush_all()
        r, body = self.send("POST", "/equipment-requests/%s/cancel" % rec.name, {}, token=token)
        self.assertEqual((r.status_code, body["status"]), (200, "cancelled"))
        self.assertEqual(rec.payment_order_id.state, "cancel")
        r, body = self.send("POST", "/equipment-requests/%s/cancel" % rec.name, {}, token=token)
        self.assertEqual((r.status_code, body["code"]), (409, "invalid_stage"))

    def test_cancel_a_rent_frees_the_machine(self):
        token = self.token()
        rec = self._place(token, listing="rent", rentPeriod="day", rentUnits=2, contractAccepted=True)
        self.assertEqual(rec.rental_id.state, "reserved")
        r, body = self.send("POST", "/equipment-requests/%s/cancel" % rec.name, {}, token=token)
        self.assertEqual((r.status_code, body["status"]), (200, "cancelled"))
        self.assertEqual(rec.rental_id.state, "cancelled")

    def test_confirmed_or_someone_elses_request_cannot_be_cancelled(self):
        token = self.token()
        rec = self._place(token)
        rec.state = "inProgress"
        self.env.flush_all()
        r, body = self.send("POST", "/equipment-requests/%s/cancel" % rec.name, {}, token=token)
        self.assertEqual((r.status_code, body["code"]), (409, "invalid_stage"))
        self.assertEqual(self.send("POST", "/equipment-requests/%s/cancel" % rec.name, {})[0].status_code, 401)
        other = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Other", "login": "other-cancel@example.com", "password": "Secret123",
            "groups_id": [(6, 0, [self.env.ref("base.group_portal").id])]})
        from odoo.addons.leaders_marketplace.tools.auth_tokens import encode_access_token
        r, body = self.send("POST", "/equipment-requests/%s/cancel" % rec.name, {}, token=encode_access_token(self.env, other))
        self.assertEqual(r.status_code, 404)


@tagged("post_install", "-at_install")
class TestEquipmentRequestReturnUrl(TestStorefrontApi):
    """After paying, the customer goes back to the storefront page given as returnUrl, with ?payment=success|failed."""

    SHIP = {"name": "A B", "phone": "+201005550123", "governorate": "cairo"}

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.auth_cors_origin",
                                                         "http://localhost:3000,https://spark-fu.com")

    def test_only_our_own_pages_are_accepted(self):
        token = self.token()
        for bad in ("https://evil.example/ar/x", "javascript:alert(1)", "https://spark-fu.com.evil.example/x", 12):
            r, body = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": self.SHIP,
                                                                 "returnUrl": bad}, token=token)
            self.assertEqual((r.status_code, "returnUrl" in body.get("fields", {})), (422, True), bad)

    def test_return_goes_back_with_the_payment_result(self):
        token = self.token()
        url = "https://spark-fu.com/ar/equipment-requests/{reference}?tab=pay"
        r, body = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": self.SHIP,
                                                             "returnUrl": url}, token=token)
        self.assertEqual(r.status_code, 201, body)
        rec = self.env["leaders.customer.request"].search([("name", "=", body["reference"])])
        self.assertEqual(rec.return_url, url)
        rec.payment_order_id = rec._leaders_payment_order("Services", 500)
        self.env.flush_all()
        r = self.opener.get(self.base_url() + "/equipment-requests/%s/payment/return" % rec.name, allow_redirects=False)
        self.assertEqual(r.headers["Location"],
                         "https://spark-fu.com/ar/equipment-requests/%s?tab=pay&payment=failed" % rec.name)
        # no returnUrl given: nothing changes for the request
        r, plain = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": self.SHIP}, token=token)
        self.assertFalse(self.env["leaders.customer.request"].search([("name", "=", plain["reference"])]).return_url)


@tagged("post_install", "-at_install")
class TestEquipmentRequestStages(TestStorefrontApi):
    """confirmed -> inProgress (being carried out) -> completed; the customer confirms receipt from the website."""

    def test_stages_and_received(self):
        token = self.token()
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo"}
        r, body = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": ship}, token=token)
        rec = self.env["leaders.customer.request"].search([("name", "=", body["reference"])])
        ref = rec.name
        r, b = self.send("POST", "/equipment-requests/%s/received" % ref, {}, token=token)
        self.assertEqual((r.status_code, b["code"]), (409, "invalid_stage"))  # not confirmed yet
        rec.action_equipment_confirm()
        self.assertEqual(self.get("/equipment-requests/%s" % ref, token)[1]["status"], "confirmed")
        rec.action_equipment_execute()
        self.env.flush_all()
        self.assertEqual(self.get("/equipment-requests/%s" % ref, token)[1]["status"], "inProgress")
        _s, tab = self.get("/account/orders?kind=purchase", token)
        self.assertEqual(tab["items"][0]["status"], "inProgress")
        r, b = self.send("POST", "/equipment-requests/%s/received" % ref, {}, token=token)
        self.assertEqual((r.status_code, b["status"]), (200, "completed"))
        r, b = self.send("POST", "/equipment-requests/%s/received" % ref, {}, token=token)
        self.assertEqual(r.status_code, 409)

    def test_admin_completes_and_quotes_have_no_equipment_steps(self):
        partner = self.user.partner_id
        rec = self.env["leaders.customer.request"].create({"kind": "rent", "partner_id": partner.id, "title": "R"})
        rec.action_equipment_confirm()
        rec.action_equipment_complete()
        self.assertEqual(rec.state, "completed")
        quote = self.env["leaders.customer.request"].create({"kind": "quote", "partner_id": partner.id, "title": "Q"})
        with self.assertRaises(UserError):
            quote.action_equipment_confirm()


PNG_PIXEL = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
    b"\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
    b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@tagged("post_install", "-at_install")
class TestItemRequestsAndNewFilters(TestStorefrontApi):
    """POST /item-requests, the year / family / warranty filters and the brand logos (item-request contract)."""

    FORM = {"name": "Omar Saleh", "phone": "+201001234567", "productName": "Perkins 1506A injector",
            "productType": "part", "brand": "ApiBrand", "model": "1506A", "quantity": 2,
            "address": "Nasr City, Cairo", "notes": "Needed this week", "locale": "ar"}

    def test_guest_request_lands_in_odoo_with_a_reference_and_a_sales_todo(self):
        group = self.env.ref("leaders_marketplace.group_equipment_listing_sales")
        sales = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Sales One", "login": "sales.one@example.com", "groups_id": [(6, 0, [group.id, self.env.ref("base.group_user").id])]})
        r, body = self.send("POST", "/item-requests", self.FORM)
        self.assertEqual(r.status_code, 201, body)
        self.assertEqual(set(body), {"reference"})
        self.assertTrue(body["reference"].startswith("IR-"))
        rec = self.env["leaders.customer.request"].search([("name", "=", body["reference"])])
        self.assertEqual((rec.kind, rec.title, rec.item_type, rec.item_brand_id, rec.item_model, rec.quantity,
                          rec.shipping_address, rec.request_locale, rec.phone, rec.contact_name),
                         ("item", "Perkins 1506A injector", "part", self.brand, "1506A", 2, "Nasr City, Cairo", "ar",
                          "+201001234567", "Omar Saleh"))
        self.assertTrue(rec.activity_ids.filtered(lambda a: a.user_id == sales))

    def test_a_form_with_an_email_gets_a_confirmation_in_its_language(self):
        Mail = self.env["mail.mail"].sudo()
        r, body = self.send("POST", "/item-requests", dict(self.FORM, email="omar@example.com"))
        self.assertEqual(r.status_code, 201, body)
        mail = Mail.search([("email_to", "=", "omar@example.com")], order="id desc", limit=1)
        self.assertIn(body["reference"], mail.subject)
        self.assertIn("استلمنا طلبك", mail.subject)
        self.assertIn("Perkins 1506A injector", mail.body_html)
        r, body = self.send("POST", "/item-requests", dict(self.FORM, email="sara@example.com", locale="en"))
        mail = Mail.search([("email_to", "=", "sara@example.com")], order="id desc", limit=1)
        self.assertIn("We received your product request %s" % body["reference"], mail.subject)
        r, body = self.send("POST", "/item-requests", dict(self.FORM, phone="+201009998877"))  # no email: no mail
        self.assertFalse(Mail.search([("subject", "=", "استلمنا طلبك " + body["reference"])]))

    def test_signed_in_request_goes_on_the_account(self):
        r, body = self.send("POST", "/item-requests", dict(self.FORM, brand="Unknown Maker"), token=self.token())
        self.assertEqual(r.status_code, 201, body)
        rec = self.env["leaders.customer.request"].search([("name", "=", body["reference"])])
        self.assertEqual(rec.partner_id, self.user.partner_id.commercial_partner_id)
        self.assertEqual((rec.item_brand, rec.item_brand_id.id), ("Unknown Maker", False))

    def test_validation_reports_every_field(self):
        r, body = self.send("POST", "/item-requests", {"name": "", "phone": "0100", "productType": "boat",
                                                       "quantity": -1, "email": "x", "locale": "fr",
                                                       "notes": "n" * 1001})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(body["code"], "validation_error")
        self.assertEqual(set(body["fields"]), {"name", "phone", "productName", "productType", "quantity", "email",
                                               "locale", "notes"})

    def test_year_family_and_warranty_filters(self):
        self.eq.leaders_warranty_months = 6
        self.quote_only.write({"leaders_year": 2023, "leaders_warranty_months": 18})
        self.env.flush_all()
        _s, f = self.get("/products/filters?kind=power")
        self.assertEqual(f["year"], {"min": 2019, "max": 2023})
        self.assertEqual(f["family"], [{"value": self.model.key, "label": "API-1", "count": 2}])
        self.assertEqual({w["value"]: w["count"] for w in f["warranty"]}, {"none": 0, "lt6m": 0, "6to12m": 1, "gt12m": 1})

        def ids(query):
            status, body = self.get("/products?kind=power&" + query)
            self.assertEqual(status, 200, body)
            return {i["id"] for i in body["items"]}
        self.assertEqual(ids("yearMin=2020"), {str(self.quote_only.id)})
        self.assertEqual(ids("yearMin=2010&yearMax=2020"), {str(self.eq.id)})
        self.assertEqual(ids("warranty=6to12m"), {str(self.eq.id)})
        self.assertEqual(ids("warranty=none,gt12m"), {str(self.quote_only.id)})
        self.assertEqual(ids("family=api-1"), {str(self.eq.id), str(self.quote_only.id)})
        self.assertEqual(ids("family=nothing"), set())
        _s, parts = self.get("/products?kind=part&family=API-1")
        self.assertEqual([i["id"] for i in parts["items"]], [str(self.part.id)])  # a part fits the model
        self.assertEqual(self.get("/products?kind=power&warranty=forever")[0], 422)
        self.assertEqual(self.get("/products?kind=power&yearMin=abc")[0], 422)

    def test_brand_logo_is_offered_when_set(self):
        _s, f = self.get("/products/filters?kind=power")
        self.assertNotIn("logo", f["brands"][0])
        self.brand.logo = base64.b64encode(PNG_PIXEL)
        self.env.flush_all()
        _s, f = self.get("/products/filters?kind=power")
        logo = [b for b in f["brands"] if b["id"] == str(self.brand.id)][0]["logo"]
        self.assertTrue(logo.endswith("/brands/%s/logo" % self.brand.id))
        r = self.url_open("/brands/%s/logo" % self.brand.id)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("image/"))
        self.assertEqual(self.url_open("/brands/999999/logo").status_code, 404)



@tagged("post_install", "-at_install")
class TestProfilePagesContract(TestStorefrontApi):
    """Profile pages contract: address and business details, notification channels, order totals and filters."""

    def test_a_vendor_profile_shows_its_application_business_details(self):
        token = self.token()
        company = self.env["res.partner"].create({"name": "Vendor Co", "is_company": True, "company_registry": "862682",
                                                  "is_marketplace_vendor": True, "vendor_state": "approved"})
        self.user.partner_id.parent_id = company
        importer = self.env.ref("leaders_marketplace.vendor_business_type_importer")
        application = self.env["leaders.vendor.application"].sudo().create({
            "business_name": "Vendor Co", "owner_name": "Owner", "owner_email": "owner.vp@example.com",
            "owner_phone": "+201001234567", "business_type_id": importer.id, "partner_id": company.id,
            "commercial_registration_number": "862682"})
        application.write({"state": "approved"})
        self.env.flush_all()
        _s, profile = self.get("/account/profile", token)
        self.assertEqual((profile["type"], profile["activityType"], profile["commercialRegister"]),
                         ("vendor", importer.name, "862682"))  # no activity written: the business type
        application.business_activity = "  Import and sale of diesel generators  "
        self.env.flush_all()
        self.assertEqual(self.get("/account/profile", token)[1]["activityType"], "Import and sale of diesel generators")
        # what the vendor types on the storefront wins
        r, _p = self.send("PATCH", "/account/business", {"activityType": "Generators trade"}, token=token)
        self.assertEqual(r.status_code, 200, _p)
        self.assertEqual(self.get("/account/profile", token)[1]["activityType"], "Generators trade")

    def test_profile_address_and_business_details(self):
        token = self.token()
        _s, before = self.get("/account/profile", token)
        self.assertFalse({"address", "activityType", "commercialRegister"} & set(before))  # omitted when empty
        body = {"name": "Sf User", "email": "sf@example.com", "phone": "+201005550123", "address": "12 Tahrir St, Cairo"}
        r, p = self.send("PATCH", "/account/profile", body, token=token)
        self.assertEqual((r.status_code, p["address"]), (200, "12 Tahrir St, Cairo"))
        r, p = self.send("PATCH", "/account/profile", dict(body, address="x"), token=token)
        self.assertEqual((r.status_code, list(p["fields"])), (422, ["address"]))
        r, p = self.send("PATCH", "/account/business", {"company": "Nile Power", "activityType": "Contracting",
                                                       "commercialRegister": "CR-12345"}, token=token)
        self.assertEqual(r.status_code, 200, p)
        self.assertEqual((p["company"], p["activityType"], p["commercialRegister"], p["address"]),
                         ("Nile Power", "Contracting", "CR-12345", "12 Tahrir St, Cairo"))
        r, p = self.send("PATCH", "/account/business", {"activityType": ""}, token=token)
        self.assertNotIn("activityType", p)
        self.assertEqual(p["company"], "Nile Power")  # a key left out is kept
        r, p = self.send("PATCH", "/account/business", {"commercialRegister": "9" * 61}, token=token)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.send("PATCH", "/account/business", {})[0].status_code, 401)

    def test_notification_channels(self):
        token = self.token()
        channels = {"sms": True, "whatsapp": False, "email": False, "offers": True}
        r, saved = self.send("PUT", "/account/notifications", channels, token=token)
        self.assertEqual(r.status_code, 200)
        self.assertEqual({k: saved[k] for k in channels}, channels)
        partner = self.user.partner_id.commercial_partner_id
        self.assertFalse(partner._leaders_customer_accepts("shipping"))  # email off: no optional email
        self.assertTrue(partner._leaders_customer_accepts("orders"))  # transactional: always
        r, err = self.send("PUT", "/account/notifications", {"sms": True}, token=token)
        self.assertEqual((r.status_code, set(err["fields"])), (422, {"whatsapp", "email", "offers"}))

    def test_order_totals_and_filters(self):
        token = self.token()
        partner = self.user.partner_id
        order = self.env["sale.order"].create({"partner_id": partner.id, "order_line": [
            (0, 0, {"product_id": self.part.product_variant_id.id, "product_uom_qty": 2, "price_unit": 320})]})
        order.action_cancel()
        self.env.flush_all()
        _s, q = self.get("/account/orders?kind=purchase", token)
        row = [i for i in q["items"] if i["id"] == str(order.id)][0]
        self.assertEqual((row["total"], row["currency"], row["status"]), (int(round(order.amount_total * 100)), order.currency_id.name, "cancelled"))
        import pytz
        today = pytz.utc.localize(order.date_order).astimezone(pytz.timezone("Africa/Cairo")).strftime("%Y-%m-%d")
        _s, q = self.get("/account/orders?kind=purchase&status=cancelled&date=%s" % today, token)
        self.assertEqual([i["id"] for i in q["items"]], [str(order.id)])
        _s, q = self.get("/account/orders?kind=purchase&status=completed", token)
        self.assertEqual(q["items"], [])
        _s, q = self.get("/account/orders?kind=purchase&date=2001-01-01", token)
        self.assertEqual(q["items"], [])
        self.assertEqual(self.get("/account/orders?kind=purchase&status=lost", token)[0], 422)
        self.assertEqual(self.get("/account/orders?kind=purchase&date=12-03-2026", token)[0], 422)


@tagged("post_install", "-at_install")
class TestQuoteOfferContract(TestStorefrontApi):
    """Quote offer & payment contract: the offer is an Odoo quotation; accept, pay what is due now, receive."""

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.auth_cors_origin", "https://spark-fu.com")
        Product = self.env["product.product"]
        self.delivery = Product.create({"name": "Delivery service", "type": "service", "list_price": 200})
        self.inspection = Product.create({"name": "Inspection service", "type": "service", "list_price": 300})
        self.token_ = self.token()

    def _quote(self, **extra):
        body = dict({"name": "Sf User", "phone": "+201005550123",
                     "items": [{"productId": str(self.quote_only.id), "quantity": 1}]}, **extra)
        r, out = self.send("POST", "/quotes", body, token=self.token_)
        self.assertEqual(r.status_code, 201, out)
        return self.env["leaders.customer.request"].search([("name", "=", out["reference"])])

    def _offer(self, request, due_now=True):
        request.action_create_offer()
        order = request.quote_order_id
        order.order_line.write({"price_unit": 1000, "tax_id": [(5, 0, 0)], "leaders_offer_note": "Rent: 30 days"})
        order.write({"note": "<p>Delivery within 3 days</p><p>The delivery fee is not refunded</p>",
                     "leaders_due_now_note": "Delivery fee is not refunded once shipped.",
                     "order_line": [(0, 0, {"product_id": self.delivery.id, "price_unit": 200, "tax_id": [(5, 0, 0)],
                                            "leaders_due_now": due_now}),
                                    (0, 0, {"product_id": self.inspection.id, "price_unit": 300, "tax_id": [(5, 0, 0)],
                                            "leaders_optional": True})]})
        order.action_leaders_send_offer()
        self.env.flush_all()
        return order

    def detail(self, request):
        status, body = self.get("/quotes/%s" % request.name, self.token_)
        self.assertEqual(status, 200, body)
        return body

    def test_needed_by_is_stored_and_checked(self):
        r, err = self.send("POST", "/quotes", {"name": "Sf User", "phone": "+201005550123", "neededBy": "01-12-2026",
                                               "items": [{"productId": str(self.quote_only.id), "quantity": 1}]}, token=self.token_)
        self.assertEqual((r.status_code, list(err["fields"])), (422, ["neededBy"]))
        request = self._quote(neededBy="2099-12-01")
        self.assertEqual(self.detail(request)["neededBy"], "2099-12-01")

    def test_offer_shape_accept_without_an_optional_line_then_pay_and_receive(self):
        request = self._quote()
        self.assertNotIn("offer", self.detail(request))  # nothing before the offer is sent
        order = self._offer(request)
        body = self.detail(request)
        self.assertEqual(body["stage"], "offered")
        offer = body["offer"]
        self.assertEqual((offer["currency"], offer["total"], offer["dueNowTotal"], offer["remaining"]),
                         (order.currency_id.name, 150000, 20000, 130000))
        self.assertEqual(offer["terms"], ["Delivery within 3 days", "The delivery fee is not refunded"])
        self.assertEqual(offer["dueNowNote"], "Delivery fee is not refunded once shipped.")
        main, fee, optional = offer["lines"]
        self.assertEqual((main["amount"], main["note"], fee["amount"], optional["optional"]), (100000, "Rent: 30 days", 20000, True))
        self.assertNotIn("optional", main)
        self.assertEqual([l["id"] for l in offer["dueNow"]], [fee["id"]])

        r, err = self.send("POST", "/quotes/%s/accept" % request.name, {"acceptTerms": False}, token=self.token_)
        self.assertEqual(r.status_code, 422)
        r, body = self.send("POST", "/quotes/%s/accept" % request.name,
                            {"acceptTerms": True, "removedLines": [optional["id"], main["id"]]}, token=self.token_)  # main is not optional
        self.assertEqual(r.status_code, 200, body)
        self.assertEqual(body["stage"], "confirming")
        self.assertIn("confirming", body["history"])
        self.assertEqual(([l["id"] for l in body["offer"]["lines"]], body["offer"]["total"], body["offer"]["remaining"]),
                         ([main["id"], fee["id"]], 120000, 100000))
        self.assertTrue(body["offer"]["acceptedAt"])
        self.assertTrue(order.require_payment)
        self.assertAlmostEqual(order.prepayment_percent, 200 / 1200, places=4)
        self.assertEqual(order.leaders_payment_method, "cod")  # the rest is paid to the platform later
        r, err = self.send("POST", "/quotes/%s/accept" % request.name, {"acceptTerms": True}, token=self.token_)
        self.assertEqual((r.status_code, err["code"]), (409, "invalid_stage"))
        _s, orders = self.get("/account/orders?kind=quote", self.token_)
        self.assertEqual([i["status"] for i in orders["items"] if i["reference"] == request.name], ["confirming"])

        r, err = self.send("POST", "/quotes/%s/payment" % request.name, {"returnUrl": "https://evil.example/x"}, token=self.token_)
        self.assertEqual((r.status_code, list(err["fields"])), (422, ["returnUrl"]))
        r, err = self.send("POST", "/quotes/%s/payment" % request.name,
                           {"returnUrl": "https://spark-fu.com/ar/quotes/x", "method": "bitcoin"}, token=self.token_)
        self.assertEqual((r.status_code, list(err["fields"])), (422, ["method"]))
        r, session = self.send("POST", "/quotes/%s/payment" % request.name,
                               {"returnUrl": "https://spark-fu.com/ar/quotes/{reference}", "method": "card"}, token=self.token_)
        self.assertEqual(r.status_code, 200, session)
        self.assertIn("/orders/%s/payment" % order.leaders_public_ref, session["redirectUrl"])
        back = self.url_open("/quotes/%s/payment/return" % request.name, allow_redirects=False)
        self.assertEqual(back.headers["Location"], "https://spark-fu.com/ar/quotes/%s?payment=failed" % request.name)

        self.assertEqual(self.send("POST", "/quotes/%s/received" % request.name, token=self.token_)[0].status_code, 409)
        order.action_confirm()  # what the gateway's confirmation does once the amount due now is paid
        request.offer_paid_at = fields.Datetime.now()
        self.env.flush_all()
        self.assertEqual(self.detail(request)["stage"], "executing")
        r, err = self.send("POST", "/quotes/%s/payment" % request.name,
                           {"returnUrl": "https://spark-fu.com/ar/quotes/x"}, token=self.token_)
        self.assertEqual((r.status_code, err["code"]), (409, "already_paid"))
        _s, orders = self.get("/account/orders?kind=quote", self.token_)
        self.assertEqual([i["status"] for i in orders["items"] if i["reference"] == request.name], ["inProgress"])
        self.assertEqual(self.send("POST", "/quotes/%s/cancel" % request.name, token=self.token_)[0].status_code, 409)
        r, body = self.send("POST", "/quotes/%s/received" % request.name, token=self.token_)
        self.assertEqual((r.status_code, body["stage"]), (200, "completed"))

    def test_a_verified_payment_of_what_is_due_now_sets_paid_at_and_starts_the_work(self):
        from odoo import Command
        from .test_vendor_split import demo_provider
        request = self._quote()
        order = self._offer(request)
        self.send("POST", "/quotes/%s/accept" % request.name, {"acceptTerms": True}, token=self.token_)
        r, _session = self.send("POST", "/quotes/%s/payment" % request.name,
                                {"returnUrl": "https://spark-fu.com/ar/quotes/{reference}"}, token=self.token_)
        self.assertEqual(r.status_code, 200)
        provider = demo_provider(self)
        provider.state = "test"
        tx = self.env["payment.transaction"].create({
            "provider_id": provider.id, "operation": "online_redirect",
            "payment_method_id": provider.payment_method_ids[:1].id, "reference": "LM-QO-%s" % order.id,
            "amount": order._get_prepayment_required_amount(), "currency_id": order.currency_id.id,
            "partner_id": order.partner_id.id, "sale_order_ids": [Command.set(order.ids)]})
        self.assertEqual(tx.landing_route, "/quotes/%s/payment/return" % request.name)
        self.assertFalse(request.offer_paid_at)  # a transaction alone is not a payment
        tx._set_done()
        tx._post_process()
        self.env.flush_all()
        self.assertTrue(request.offer_paid_at)
        self.assertEqual(order.state, "sale")
        body = self.detail(request)
        self.assertEqual(body["stage"], "executing")
        self.assertTrue(body["offer"]["paidAt"])
        back = self.url_open("/quotes/%s/payment/return" % request.name, allow_redirects=False)
        self.assertTrue(back.headers["Location"].endswith("payment=success"))

    def test_nothing_due_now_goes_straight_to_executing_and_unassigned_lines_stay_with_the_platform(self):
        request = self._quote()
        order = self._offer(request, due_now=False)
        r, body = self.send("POST", "/quotes/%s/accept" % request.name, {"acceptTerms": True}, token=self.token_)
        self.assertEqual((r.status_code, body["stage"], body["offer"]["dueNowTotal"]), (200, "executing", 0))
        self.assertEqual(order.state, "sale")
        self.assertFalse(order.vendor_order_ids)  # no seller chosen on any line: all the platform's

    def test_cancel_while_offered_cancels_the_quotation(self):
        request = self._quote()
        order = self._offer(request)
        r = self.opener.post(self.base_url() + "/quotes/%s/cancel" % request.name,
                             headers={"Authorization": "Bearer %s" % self.token_, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200, r.text[:3000])
        self.assertTrue(r.json()["cancelled"])
        self.assertEqual(order.state, "cancel")


@tagged("post_install", "-at_install")
class TestCashPayments(TestQuoteOfferContract):
    """No gateway yet: quotes and equipment requests are paid in cash on delivery; online checkout is refused."""

    def test_quote_paid_in_cash_starts_the_work_without_paid_at(self):
        request = self._quote()
        order = self._offer(request)
        r, err = self.send("POST", "/quotes/%s/payment" % request.name, {"method": "cash"}, token=self.token_)
        self.assertEqual((r.status_code, err["code"]), (409, "invalid_stage"))  # not accepted yet
        self.send("POST", "/quotes/%s/accept" % request.name, {"acceptTerms": True}, token=self.token_)
        r, out = self.send("POST", "/quotes/%s/payment" % request.name, {"method": "cash"}, token=self.token_)
        self.assertEqual((r.status_code, out), (200, {}))
        body = self.detail(request)
        self.assertEqual((body["stage"], body["offer"]["paymentMethod"]), ("executing", "cash"))
        self.assertIn("executing", body["history"])
        self.assertNotIn("paidAt", body["offer"])
        self.assertEqual((order.state, order.leaders_payment_method, order.require_payment), ("sale", "cod", False))
        _s, orders = self.get("/account/orders?kind=quote", self.token_)
        self.assertEqual([i["status"] for i in orders["items"] if i["reference"] == request.name], ["inProgress"])
        r, err = self.send("POST", "/quotes/%s/payment" % request.name, {"method": "cash"}, token=self.token_)
        self.assertEqual((r.status_code, err["code"]), (409, "invalid_stage"))

    def test_equipment_request_paid_on_delivery(self):
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo"}
        self.eq.leaders_service_ids = self.env["product.template"].search(
            [("leaders_service_page", "=", True), ("leaders_slug", "in", ("installation", "delivery"))])
        r, serv = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": ship,
                                                             "services": ["installation"]}, token=self.token_)
        self.assertEqual(r.status_code, 201, serv)
        path = "/equipment-requests/%s/payment" % serv["reference"]
        r, err = self.send("POST", path, {"method": "cash"}, token=self.token_)
        self.assertEqual((r.status_code, err["code"]), (409, "invalid_stage"))  # no price sent yet
        req = self.env["leaders.customer.request"].search([("name", "=", serv["reference"])])
        req.services_amount = 7500
        req.action_send_service_prices()
        self.env.flush_all()
        r, err = self.send("POST", path, {"method": "card"}, token=self.token_)
        self.assertEqual((r.status_code, list(err["fields"])), (422, ["method"]))
        from odoo.addons.leaders_marketplace.tools.auth_tokens import encode_access_token
        stranger = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Other Customer", "login": "other.cash@example.com",
            "groups_id": [(6, 0, [self.env.ref("base.group_portal").id])]})
        self.assertEqual(self.send("POST", path, {"method": "cash"},
                                   token=encode_access_token(self.env, stranger))[0].status_code, 404)
        r, body = self.send("POST", path, {"method": "cash"}, token=self.token_)
        self.assertEqual(r.status_code, 200, body)
        self.assertEqual((body["status"], body["paymentMethod"]), ("confirmed", "cash"))
        self.assertNotIn("paymentUrl", body)
        self.assertEqual((req.payment_order_id.state, req.payment_order_id.leaders_payment_method), ("sale", "cod"))
        r, err = self.send("POST", path, {"method": "cash"}, token=self.token_)
        self.assertEqual((r.status_code, err["code"]), (409, "invalid_stage"))

    def test_equipment_quote_row_is_marked_and_cart_quote_row_is_not(self):
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo"}
        r, eq_quote = self.send("POST", "/equipment-requests", {"productId": str(self.quote_only.id), "shipping": ship},
                                token=self.token_)
        self.assertEqual(r.status_code, 201, eq_quote)
        cart_quote = self._quote()
        _s, orders = self.get("/account/orders?kind=quote&pageSize=48", self.token_)
        source = {i["reference"]: i.get("source") for i in orders["items"]}
        self.assertEqual(source[eq_quote["reference"]], "equipmentRequest")
        self.assertIsNone(source[cart_quote.name])

    def test_online_checkout_is_refused_without_a_gateway(self):
        param = self.env["ir.config_parameter"].sudo()
        param.set_param("leaders_marketplace.online_payments", "off")
        cairo = self.env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = self.env["leaders.vendor.delivery.zone"]
        if not Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]):
            Zone.create({"is_platform": True, "state_id": cairo.id, "price": 50})
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "city": "nasrCity", "address": "1 St"}
        item = {"productId": str(self.part.id), "quantity": 1, "options": {}, "unitPrice": 32000}
        before = self.env["sale.order"].search_count([])
        for method in ("card", "paypal", "applePay"):
            r, err = self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": method}, token=self.token_)
            self.assertEqual((r.status_code, list(err["fields"])), (422, ["payment"]), method)
        self.assertEqual(self.env["sale.order"].search_count([]), before)
        r, placed = self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": "cod"}, token=self.token_)
        self.assertEqual(r.status_code, 201, placed)
        self.assertNotIn("paymentUrl", placed)

    def test_notifications_put_without_sms_keeps_it(self):
        _s, prefs = self.get("/account/notifications", self.token_)
        r, out = self.send("PUT", "/account/notifications", {"whatsapp": True, "email": False, "offers": True},
                           token=self.token_)
        self.assertEqual(r.status_code, 200, out)
        self.assertEqual((out["sms"], out["whatsapp"], out["email"]), (prefs["sms"], True, False))


@tagged("post_install", "-at_install")
class TestEquipmentRequestDetails(TestStorefrontApi):
    """The equipment request answer carries what the track page shows: machine, services, delivery, money."""

    def setUp(self):
        super().setUp()
        cairo = self.env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = self.env["leaders.vendor.delivery.zone"]
        self.zone = Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]) or \
            Zone.create({"is_platform": True, "state_id": cairo.id, "price": 50})
        self.ship = {"name": "Ahmed Mahmoud", "phone": "+201012345678", "governorate": "cairo", "address": "Nasr City 12"}
        self.token_ = self.token()

    def test_a_purchase_shows_machine_services_delivery_and_total_once_priced(self):
        self.eq.leaders_service_ids = self.env["product.template"].search(
            [("leaders_service_page", "=", True), ("leaders_slug", "=", "inspection")])
        r, placed = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": self.ship,
                                                               "services": ["inspection"], "note": "Gate 3"},
                              token=self.token_)
        self.assertEqual(r.status_code, 201, placed)
        self.assertEqual(placed["product"]["id"], str(self.eq.id))
        self.assertEqual((placed["product"]["slug"], placed["product"]["brand"], placed["product"]["model"]),
                         (self.eq.leaders_slug, "ApiBrand", "API-1"))
        self.assertTrue(placed["product"]["name"])
        inspection = self.env["product.template"].search([("leaders_service_page", "=", True),
                                                          ("leaders_slug", "=", "inspection")])
        self.assertEqual(placed["serviceLines"], [{"slug": "inspection", "name": inspection.name}])
        self.assertEqual(placed["services"], ["inspection"])  # kept for older clients
        self.assertEqual(placed["shipping"], {"name": "Ahmed Mahmoud", "phone": "+201012345678", "governorate": "cairo",
                                              "governorateName": "Cairo", "address": "Nasr City 12"})
        self.assertEqual((placed["note"], placed["currency"]), ("Gate 3", self.env.company.currency_id.name))
        self.assertNotIn("total", placed)  # the service is not priced yet
        req = self.env["leaders.customer.request"].search([("name", "=", placed["reference"])])
        req.services_amount = 1500
        req.action_send_service_prices()
        self.env.flush_all()
        _s, body = self.get("/equipment-requests/%s" % req.name, self.token_)
        self.assertEqual(body["serviceLines"][0]["amount"], 150000)
        self.assertEqual(body["total"], body["unitPrice"] + 150000)
        self.assertNotIn("deliveryFee", body)  # on a purchase the delivery is one of the services

    def test_an_older_request_names_the_person_not_the_company(self):
        company = self.env["res.partner"].create({"name": "Company Two", "is_company": True})
        self.user.partner_id.parent_id = company
        r, placed = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": self.ship},
                              token=self.token_)
        req = self.env["leaders.customer.request"].search([("name", "=", placed["reference"])])
        self.assertEqual(req.partner_id, company)
        req.contact_name = False  # placed before the typed name was kept
        self.env.flush_all()
        _s, body = self.get("/equipment-requests/%s" % req.name, self.token_)
        self.assertEqual(body["shipping"]["name"], self.user.name)

    def test_a_rental_shows_its_period_delivery_fee_and_total(self):
        self.eq.leaders_service_ids = self.env["product.template"].search(
            [("leaders_service_page", "=", True), ("leaders_slug", "=", "delivery")])
        book = {"productId": str(self.eq.id), "listing": "rent", "rentPeriod": "week", "rentUnits": 2,
                "shipping": self.ship, "services": ["delivery"], "contractAccepted": True}
        r, placed = self.send("POST", "/equipment-requests", book, token=self.token_)
        self.assertEqual(r.status_code, 201, placed)
        self.assertEqual((placed["rental"]["period"], placed["rental"]["units"]), ("week", 2))
        self.assertEqual(placed["deliveryFee"], int(self.zone.price * 100))
        self.assertEqual(placed["total"], placed["rental"]["pricePerDay"] * 14 + int(self.zone.price * 100))
        self.assertEqual(len(placed["serviceLines"]), 1)
        self.assertNotIn("amount", placed["serviceLines"][0])

    def test_arabic_names(self):
        ar = self.env["res.lang"]._activate_lang("ar_001")
        self.assertTrue(ar)
        r = self.opener.post(self.base_url() + "/equipment-requests", data=json.dumps(
            {"productId": str(self.eq.id), "shipping": self.ship}), headers={
            "Content-Type": "application/json", "Authorization": "Bearer %s" % self.token_, "Accept-Language": "ar"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["shipping"]["governorateName"], "القاهرة")
        self.assertEqual(r.json()["serviceLines"], [])


@tagged("post_install", "-at_install")
class TestUnsoldProducts(TestStorefrontApi):
    """Project guide p.3: the platform holds no stock and sells nothing itself, so a priced product that no vendor
    sells shows as out of stock and is refused by the cart, the checkout and the purchase request."""

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.platform_sells_products", "0")
        self.token_ = self.token()

    def test_a_part_without_a_seller_cannot_be_bought(self):
        _s, part = self.get("/products/%s" % self.part.leaders_slug)
        self.assertIs(part["inStock"], False)
        r, err = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 1}, token=self.token_)
        self.assertEqual((r.status_code, err["code"]), (409, "out_of_stock"))
        cairo = self.env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = self.env["leaders.vendor.delivery.zone"]
        if not Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]):
            Zone.create({"is_platform": True, "state_id": cairo.id, "price": 50})
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "city": "nasrCity", "address": "1 St"}
        item = {"productId": str(self.part.id), "quantity": 1, "options": {}, "unitPrice": 32000}
        r, err = self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": "cod"}, token=self.token_)
        self.assertEqual((r.status_code, err["code"]), (409, "out_of_stock"), err)

    def test_a_machine_without_a_seller_cannot_be_bought_but_can_be_rented_or_quoted(self):
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo"}
        self.eq.leaders_rentable = False
        self.env.flush_all()
        _s, eq = self.get("/products/%s" % self.eq.leaders_slug)
        self.assertIs(eq["inStock"], False)
        r, err = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": ship},
                           token=self.token_)
        self.assertEqual((r.status_code, err["code"]), (409, "out_of_stock"))
        # no price: a quote request, as before
        r, quote = self.send("POST", "/equipment-requests", {"productId": str(self.quote_only.id), "shipping": ship},
                             token=self.token_)
        self.assertEqual((r.status_code, quote["kind"]), (201, "quote"))

    def test_the_platform_sells_its_own_product_from_its_own_stock(self):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.platform_sells_products", "1")
        self.part.is_storable = True
        self.env.flush_all()
        _s, part = self.get("/products/%s" % self.part.leaders_slug)
        self.assertIs(part["inStock"], False)  # tracked, nothing in stock
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
        quant = self.env["stock.quant"].with_context(inventory_mode=True).create({
            "product_id": self.part.product_variant_id.id, "location_id": warehouse.lot_stock_id.id,
            "inventory_quantity": 2})
        quant.action_apply_inventory()
        self.env.flush_all()
        _s, part = self.get("/products/%s" % self.part.leaders_slug)
        self.assertNotIn("inStock", part)
        self.assertEqual(part["stockQuantity"], 2)
        r, err = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 3}, token=self.token_)
        self.assertEqual((r.status_code, err["code"], err["available"]), (409, "insufficient_stock", 2))
        r, _cart = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 2}, token=self.token_)
        self.assertEqual(r.status_code, 200, _cart)

    def _vendor_with(self, tmpl, qty, price):
        vendor = self.env["res.partner"].create({"name": "Seller Co %s" % tmpl.id, "is_company": True,
                                                 "is_marketplace_vendor": True, "vendor_state": "approved"})
        tmpl.is_storable = True
        offer = self.env["leaders.marketplace.offer"].create({
            "product_tmpl_id": tmpl.id, "product_id": tmpl.product_variant_id.id, "vendor_id": vendor.id,
            "seller_sku": "SELL-%s" % tmpl.id, "price": price, "fulfillment_type": "fbm", "state": "active",
            "stock_location_id": vendor._marketplace_stock_location("fbm").id})
        self.env["stock.quant"].sudo()._leaders_adjust_vendor_stock(
            tmpl.product_variant_id, offer.stock_location_id, vendor, qty, reason="test")
        return offer

    def _platform_stock(self, tmpl, qty):
        tmpl.is_storable = True
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
        quant = self.env["stock.quant"].with_context(inventory_mode=True).create({
            "product_id": tmpl.product_variant_id.id, "location_id": warehouse.lot_stock_id.id, "inventory_quantity": qty})
        quant.action_apply_inventory()

    def test_vendor_and_platform_sell_the_same_product_side_by_side(self):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.platform_sells_products", "1")
        offer = self._vendor_with(self.part, 1, 320)
        self._platform_stock(self.part, 2)
        self.env.flush_all()
        _s, part = self.get("/products/%s" % self.part.leaders_slug)
        cards = {o["id"]: o for o in part["offers"]}
        self.assertEqual((cards[str(offer.id)]["available"], cards["platform"]["available"]), (1, 2))
        self.assertEqual(cards["platform"]["seller"], self.env.company.name)
        # the vendor has 1: asking 2 from it points at the platform as the alternative
        r, err = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 2,
                                                   "options": {"offerId": str(offer.id)}}, token=self.token_)
        self.assertEqual((r.status_code, [a["id"] for a in err["alternatives"]]), (409, ["platform"]))
        r, cart = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 2,
                                                    "options": {"offerId": "platform"}}, token=self.token_)
        self.assertEqual(r.status_code, 200, cart)
        self.assertEqual(cart["lines"][0]["options"]["offerId"], "platform")
        r, err = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 1,
                                                   "options": {"offerId": "platform"}}, token=self.token_)
        self.assertEqual((r.status_code, err["available"]), (409, 2))  # 2 already in the cart
        # the order: the platform line is never handed to the vendor
        cairo = self.env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = self.env["leaders.vendor.delivery.zone"]
        if not Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]):
            Zone.create({"is_platform": True, "state_id": cairo.id, "price": 50})
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "city": "nasrCity", "address": "1 St"}
        item = {"productId": str(self.part.id), "quantity": 2, "options": {"offerId": "platform"},
                "unitPrice": cards["platform"]["unitPrice"]}
        r, placed = self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": "cod"}, token=self.token_)
        self.assertEqual(r.status_code, 201, placed)
        order = self.env["sale.order"].search([("leaders_public_ref", "=", placed["reference"])])
        line = order.order_line.filtered(lambda l: l.product_id == self.part.product_variant_id)
        self.assertTrue(line.leaders_platform_sold)
        self.assertFalse(order._marketplace_vendor_lines())
        self.assertFalse(line.marketplace_offer_id)

    def test_a_machine_can_be_requested_from_the_platform_next_to_a_vendor(self):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.platform_sells_products", "1")
        self.eq.leaders_condition = "new"
        offer = self._vendor_with(self.eq, 1, 1850000)
        self._platform_stock(self.eq, 2)
        self.env.flush_all()
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo"}
        r, body = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": ship,
                                                             "quantity": 2, "offerId": "platform"}, token=self.token_)
        self.assertEqual(r.status_code, 201, body)
        req = self.env["leaders.customer.request"].search([("name", "=", body["reference"])])
        self.assertFalse(req.offer_id)
        r, err = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": ship,
                                                            "quantity": 3, "offerId": "platform"}, token=self.token_)
        self.assertEqual((r.status_code, err["code"]), (409, "insufficient_stock"))
        r, body = self.send("POST", "/equipment-requests", {"productId": str(self.eq.id), "shipping": ship,
                                                             "offerId": str(offer.id)}, token=self.token_)
        self.assertEqual(r.status_code, 201, body)

    def test_a_vendor_offer_makes_it_buyable_again(self):
        vendor = self.env["res.partner"].create({"name": "Seller Co", "is_company": True,
                                                 "is_marketplace_vendor": True, "vendor_state": "approved"})
        self.part.is_storable = True
        offer = self.env["leaders.marketplace.offer"].create({
            "product_tmpl_id": self.part.id, "product_id": self.part.product_variant_id.id, "vendor_id": vendor.id,
            "seller_sku": "SELL-1", "price": 320, "fulfillment_type": "fbm", "state": "active",
            "stock_location_id": vendor._marketplace_stock_location("fbm").id})
        self.env["stock.quant"].sudo()._leaders_adjust_vendor_stock(
            self.part.product_variant_id, offer.stock_location_id, vendor, 5, reason="test")
        self.env.flush_all()
        _s, part = self.get("/products/%s" % self.part.leaders_slug)
        self.assertNotIn("inStock", part)
        r, _cart = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 1}, token=self.token_)
        self.assertEqual(r.status_code, 200, _cart)


@tagged("post_install", "-at_install")
class TestPartialStockBackorder(TestStorefrontApi):
    """Partial stock: stockQuantity on products, options.backorder on the cart and the order (a sourcing to-do)."""

    def test_stock_quantity_and_backorder(self):
        env = self.env
        self.part.is_storable = True
        product = self.part.product_variant_id
        vendor = env["res.partner"].create({"name": "BoSeller", "is_marketplace_vendor": True, "vendor_state": "approved"})
        vendor._marketplace_ensure_vendor_stock()
        offer = env["leaders.marketplace.offer"].create({
            "product_tmpl_id": self.part.id, "product_id": product.id, "vendor_id": vendor.id, "seller_sku": "BO-1",
            "price": 300, "fulfillment_type": "fbm", "stock_location_id": vendor._marketplace_stock_location("fbm").id})
        env["stock.quant"]._leaders_adjust_vendor_stock(product, vendor.vendor_location_id, vendor, 3)
        offer.action_approve()
        env.flush_all()
        _s, card = self.get("/products/%s" % self.part.leaders_slug)
        self.assertEqual(card["stockQuantity"], 3)
        token = self.token()
        r, cart = self.send("POST", "/cart/items", {"productId": str(self.part.id), "quantity": 3,
                                                    "options": {"offerId": str(offer.id), "backorder": 4}}, token=token)
        self.assertEqual(r.status_code, 200, cart)
        self.assertEqual(cart["lines"][0]["options"]["backorder"], 4)
        r, err = self.send("POST", "/cart/items", {"productId": str(self.part.id), "options": {"backorder": -1}}, token=token)
        self.assertEqual(r.status_code, 422)
        group = env.ref("leaders_marketplace.group_equipment_listing_sales")
        sales = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Sourcing", "login": "sourcing@example.com", "groups_id": [(6, 0, [group.id, env.ref("base.group_user").id])]})
        cairo = env["res.country.state"].with_context(lang="en_US").search([("name", "=", "Cairo")], limit=1)
        Zone = env["leaders.vendor.delivery.zone"]
        if not Zone.search([("is_platform", "=", True), ("state_id", "=", cairo.id)]):
            Zone.create({"is_platform": True, "state_id": cairo.id, "price": 50})
        ship = {"name": "A B", "phone": "+201005550123", "governorate": "cairo", "city": "nasrCity", "address": "1 St"}
        item = {"productId": str(self.part.id), "quantity": 3, "unitPrice": 30000,
                "options": {"offerId": str(offer.id), "backorder": 4}}
        r, placed = self.send("POST", "/orders", {"items": [item], "shipping": ship, "payment": "cod"}, token=token)
        self.assertEqual(r.status_code, 201, placed)
        sale = env["sale.order"].search([("leaders_public_ref", "=", placed["reference"])])
        self.assertEqual(sale.order_line.filtered(lambda l: not l.is_delivery).product_uom_qty, 3)  # not billed
        todo = sale.activity_ids.filtered(lambda a: a.user_id == sales)
        self.assertIn("Source 4", todo.summary)


@tagged("post_install", "-at_install")
class TestCategorySlugFromName(TestStorefrontApi):
    """A storefront category's slug is made from its English name and cannot be typed on the screen."""

    def test_slug_follows_the_english_name_and_ignores_a_typed_one(self):
        equipment = self.env.ref("leaders_marketplace.leaders_categ_equipment")
        user = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Cat Manager", "login": "cat.manager@example.com",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id,
                                  self.env.ref("leaders_marketplace.group_marketplace_manager").id,
                                  self.env.ref("website.group_website_designer").id,
                                  self.env.ref("sales_team.group_sale_manager").id])]})
        Category = self.env["product.public.category"].with_user(user)
        categ = Category.create({"name": "Air Compressors", "parent_id": equipment.id, "leaders_slug": "ضواغط هوائية"})
        self.assertEqual(categ.leaders_slug, "air-compressors")
        categ.write({"leaders_slug": "hhh"})
        self.assertEqual(categ.leaders_slug, "air-compressors")  # read-only on the screen
        categ.write({"name": "Air Compressors & Pumps"})
        self.assertEqual(categ.leaders_slug, "air-compressors-pumps")
        twin = Category.create({"name": "Air Compressors & Pumps", "parent_id": equipment.id})
        self.assertEqual(twin.leaders_slug, "air-compressors-pumps-2")  # unique
        arabic = Category.create({"name": "ضواغط  هَوائية / 2026", "parent_id": equipment.id})
        self.assertEqual(arabic.leaders_slug, "dwaght-hwaeya-2026")  # Arabic in Latin letters, no space
        symbols = Category.create({"name": "***", "parent_id": equipment.id})
        self.assertEqual(symbols.leaders_slug, "category-%s" % symbols.id)  # nothing usable in the name
        loaders = self.env.ref("leaders_marketplace.leaders_categ_loaders")
        loaders.with_user(user).write({"name": "Wheel Loaders"})
        self.assertEqual(loaders.leaders_slug, "loader")  # shipped: the storefront's routes depend on it
        plain = Category.create({"name": "Website only"})  # not under the storefront's trees: no slug
        self.assertFalse(plain.leaders_slug)



@tagged("post_install", "-at_install")
class TestWarrantyOnTheCard(TestStorefrontApi):
    """Guide p.19: who gives the warranty and what it covers show on the product card (in the request's language)."""

    def test_warranty_provider_and_scope_in_specs(self):
        _s, body = self.get("/products/%s" % self.eq.leaders_slug)
        self.assertNotIn("warrantyBy", body["specs"])  # omitted when empty
        self.eq.write({"leaders_warranty_months": 12, "leaders_warranty_provider": "manufacturer",
                       "leaders_warranty_scope": "Engine and alternator; wear parts excluded"})
        self.env.flush_all()
        _s, body = self.get("/products/%s" % self.eq.leaders_slug)
        self.assertEqual((body["specs"]["warrantyBy"], body["specs"]["warrantyScope"]),
                         ("Manufacturer", "Engine and alternator; wear parts excluded"))
        self.env["res.lang"]._activate_lang("ar_001")
        r = self.url_open("/products/%s" % self.eq.leaders_slug, headers={"Accept-Language": "ar"})
        self.assertEqual(r.json()["specs"]["warrantyBy"], "الشركة المصنّعة")
