"""What the mobile app asked for before the store release: delete the account, the legal pages, seller logos, the
rent listing, and ``sort`` / facets with ``featured=true``."""
import base64
import json

from odoo.tests import tagged
from odoo.tests.common import HttpCase

# a 1x1 PNG
PNG = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="))


@tagged("post_install", "-at_install")
class TestAppStoreReadiness(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        for key in ("rent_margin_percent", "rent_margin_min"):
            env["ir.config_parameter"].sudo().set_param("leaders_marketplace.%s" % key, "0")
        env["ir.config_parameter"].sudo().set_param("leaders_marketplace.platform_sells_products", "1")
        env["ir.config_parameter"].sudo().set_param("leaders_marketplace.auth_cookie_secure", "False")
        env["product.template"].search([("leaders_kind", "!=", False)]).write({"is_published": False})
        cls.gen = env.ref("leaders_marketplace.leaders_categ_generators")
        cls.brand_a = env["leaders.brand"].create({"name": "BrandA"})
        cls.brand_b = env["leaders.brand"].create({"name": "BrandB"})

        def product(name, price, brand, **extra):
            vals = {"name": name, "public_categ_ids": [(6, 0, cls.gen.ids)], "leaders_brand_id": brand.id,
                    "leaders_source_id": env.ref("leaders_marketplace.leaders_source_original").id,
                    "leaders_condition": "new", "leaders_capacity": 100, "leaders_capacity_unit": "kva",
                    "list_price": price, "marketplace_state": "published", "is_published": True}
            vals.update(extra)
            return env["product.template"].create(vals)
        cls.dear = product("Dear Gen", 5000, cls.brand_a, leaders_featured=True)
        cls.cheap = product("Cheap Gen", 100, cls.brand_a, leaders_featured=True)
        cls.plain = product("Plain Gen", 300, cls.brand_b)
        cls.rentable = product("Rent Gen", 900, cls.brand_b, leaders_rentable=True, leaders_rent_price_day=50)
        cls.user = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Del User", "login": "del@example.com", "email": "del@example.com", "password": "Secret123",
            "groups_id": [(6, 0, [env.ref("base.group_portal").id])]})
        cls.user.partner_id.write({"phone": "+201005550199", "street": "1 Test St"})
        env.flush_all()

    def call(self, method, path, token=None):
        headers = {"Authorization": "Bearer %s" % token} if token else {}
        r = self.opener.request(method, self.base_url() + path, headers=headers, timeout=30)
        return r, (json.loads(r.content) if r.content and "json" in r.headers.get("Content-Type", "") else None)

    def token(self, user=None):
        from odoo.addons.leaders_marketplace.tools.auth_tokens import encode_access_token
        return encode_access_token(self.env, user or self.user)

    # ------------------------------------------------------------------ delete the account
    def test_delete_account_erases_the_person_and_signs_them_out(self):
        env = self.env
        env["leaders.wishlist.item"].create({"user_id": self.user.id, "product_tmpl_id": self.cheap.id})
        env.flush_all()
        token = self.token()
        r, _body = self.call("DELETE", "/account", token)
        self.assertEqual(r.status_code, 204)
        user = env["res.users"].with_context(active_test=False).browse(self.user.id)
        self.assertFalse(user.active)
        self.assertEqual(user.login, "deleted-%s@deleted.invalid" % user.id)
        partner = user.partner_id
        self.assertEqual((partner.name, partner.phone, partner.email, partner.street),
                         ("Deleted user", False, False, False))
        self.assertFalse(env["leaders.wishlist.item"].search([("user_id", "=", user.id)]))
        r, _body = self.call("GET", "/account/profile", token)  # the old token is no good any more
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.call("DELETE", "/account")[0].status_code, 401)  # signed in is required

    def test_open_requests_keep_the_account(self):
        env = self.env
        env["leaders.customer.request"].create({
            "partner_id": self.user.partner_id.id, "kind": "quote", "title": "Gen", "state": "pending"})
        r, body = self.call("DELETE", "/account", self.token())
        self.assertEqual((r.status_code, body["code"], body["openCount"]), (409, "open_requests", 1))
        self.assertTrue(self.user.exists().active)
        self.assertEqual(self.user.partner_id.name, "Del User")

    def test_vendor_and_staff_accounts_are_not_deleted_from_the_app(self):
        env = self.env
        staff = env["res.users"].create({"name": "Staff", "login": "staff.del@example.com"})
        r, body = self.call("DELETE", "/account", self.token(staff))
        self.assertEqual((r.status_code, body["code"]), (409, "staff_account"))
        vendor_user = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Vend", "login": "vend.del@example.com", "email": "vend.del@example.com",
            "groups_id": [(6, 0, [env.ref("base.group_portal").id])]})
        vendor_user.partner_id.write({"is_marketplace_vendor": True, "vendor_state": "approved"})
        r, body = self.call("DELETE", "/account", self.token(vendor_user))
        self.assertEqual((r.status_code, body["code"]), (409, "vendor_account"))

    # ------------------------------------------------------------------ pages
    def test_the_legal_pages_are_seeded_published_and_never_overwritten(self):
        Page = self.env["leaders.site.page"].sudo()
        Page.search([("slug", "in", ["privacy", "terms", "rent-contract"])]).write({"content": False, "published": False})
        Page.search([("slug", "=", "terms")]).write({"content": "<p>The admin wrote this.</p>"})
        Page._leaders_seed_pages()
        self.env.flush_all()
        for slug in ("privacy", "rent-contract"):
            r, body = self.call("GET", "/pages/%s" % slug)
            self.assertEqual(r.status_code, 200, slug)
            self.assertTrue(body["title"] and len(body["content"]) > 500, slug)
        contract = self.call("GET", "/pages/rent-contract")[1]["content"]
        self.assertTrue("Lessor" in contract or "المؤجِّر" in contract)  # whichever language the site answers in
        terms = Page.search([("slug", "=", "terms")])
        self.assertEqual(terms.content, "<p>The admin wrote this.</p>")  # untouched
        arabic = self.env["res.lang"].search([("code", "like", "ar%"), ("active", "=", True)], limit=1)
        if arabic:
            page = Page.search([("slug", "=", "rent-contract")]).with_context(lang=arabic.code)
            self.assertIn("المؤجِّر", page.content)

    # ------------------------------------------------------------------ seller logos
    def test_the_platform_seller_logo_rides_with_the_rent_price(self):
        company = self.env.company
        company.logo = False
        self.env.flush_all()
        self.assertNotIn("rentSellerLogo", self.call("GET", "/products/%s" % self.rentable.leaders_slug)[1])
        company.logo = PNG
        self.env.flush_all()
        body = self.call("GET", "/products/%s" % self.rentable.leaders_slug)[1]
        self.assertTrue(body["rentSellerLogo"].startswith(self.base_url() + "/sellers/platform/logo?unique="))
        r, _b = self.call("GET", "/sellers/platform/logo")
        self.assertEqual((r.status_code, r.headers["Content-Type"].split(";")[0]), (200, "image/png"))

    def test_a_vendors_logo_is_served_only_for_a_visible_vendor(self):
        from odoo.addons.leaders_marketplace.services.storefront import Storefront
        env = self.env
        vendor = env["res.partner"].create({"name": "Logo Vendor", "is_marketplace_vendor": True,
                                            "vendor_state": "approved", "image_1920": PNG})
        env.flush_all()
        url = Storefront(env).seller_logo(vendor)
        self.assertIn("/sellers/%s/logo?unique=" % vendor.id, url)
        expected = 404 if vendor._leaders_name_hidden() else 200  # a commission vendor sells under the platform's name
        self.assertEqual(self.call("GET", "/sellers/%s/logo" % vendor.id)[0].status_code, expected)
        self.assertEqual(self.call("GET", "/sellers/%s/logo" % self.user.partner_id.id)[0].status_code, 404)
        self.assertIsNone(Storefront(env).seller_logo(env["res.partner"].create({"name": "No Logo"})))

    # ------------------------------------------------------------------ rent listing and featured
    def test_a_product_for_rent_says_listing_rent(self):
        body = self.call("GET", "/products/%s" % self.rentable.leaders_slug)[1]
        self.assertEqual(body["listing"], "rent")
        self.assertEqual(body["availableFor"], ["buy", "rent"])  # the category still sells too
        self.assertEqual(self.call("GET", "/products/%s" % self.plain.leaders_slug)[1]["listing"], "buy")

    def test_featured_honours_sort_and_the_facets_follow_featured(self):
        ids = lambda body: [i["id"] for i in (body if isinstance(body, list) else body["items"])]
        _r, asc = self.call("GET", "/products?featured=true&limit=8&sort=priceAsc")
        _r, desc = self.call("GET", "/products?featured=true&limit=8&sort=priceDesc")
        self.assertEqual(ids(asc), [str(self.cheap.id), str(self.dear.id)])
        self.assertEqual(ids(desc), [str(self.dear.id), str(self.cheap.id)])
        _r, paged = self.call("GET", "/products?featured=true&pageSize=8&sort=priceAsc")
        self.assertEqual(ids(paged), [str(self.cheap.id), str(self.dear.id)])
        _r, all_facets = self.call("GET", "/products/facets?kind=power")
        _r, featured_facets = self.call("GET", "/products/facets?kind=power&featured=true")
        brands = lambda f: {b["value"]: b["count"] for b in f["brands"]}
        self.assertEqual(brands(all_facets), {"BrandA": 2, "BrandB": 2})
        self.assertEqual(brands(featured_facets), {"BrandA": 2})  # only what the featured list shows
