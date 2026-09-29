"""B3: variants, extra images and specifications in the vendor portal product form."""
import re

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD
from .test_vendor_product_content import PNG_1PX

ENABLED = "leaders_marketplace.vendor_variants_enabled"
NEW_ATTRS = "leaders_marketplace.allow_vendor_attributes"
MAX_IMAGES = "leaders_marketplace.vendor_max_extra_images"


class VariantsCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.param = cls.env["ir.config_parameter"].sudo()
        cls.param.set_param(ENABLED, "True")
        cls.param.set_param(NEW_ATTRS, False)
        cls.param.set_param(MAX_IMAGES, "3")
        cls.Offer = cls.env["leaders.marketplace.offer"]
        Attr = cls.env["product.attribute"]
        cls.size = Attr.create({"name": "B3 Size", "create_variant": "always", "value_ids": [
            Command.create({"name": "S"}), Command.create({"name": "M"}), Command.create({"name": "L"})]})
        cls.color = Attr.create({"name": "B3 Color", "create_variant": "always", "value_ids": [
            Command.create({"name": "Red"}), Command.create({"name": "Blue"})]})
        cls.material = Attr.create({"name": "B3 Material", "create_variant": "no_variant", "value_ids": [
            Command.create({"name": "Cotton"})]})
        cls.dynamic = Attr.create({"name": "B3 Dyn", "create_variant": "dynamic", "value_ids": [
            Command.create({"name": "D1"}), Command.create({"name": "D2"})]})
        cls.tier = cls.env["leaders.vendor.tier"].create(
            {"name": "B3 Tier", "code": "b3-tier", "product_limit": 0})
        cls.vendor.vendor_tier_id = cls.tier

    def _tmpl(self, vendor=None, name="B3 Shirt"):
        return self.env["product.template"].create({
            "name": name, "type": "consu", "is_storable": True, "sale_ok": True,
            "marketplace_state": "draft", "marketplace_owner_vendor_id": (vendor or self.vendor).id})

    def _sel(self, size=(), color=(), material=()):
        selection = {}
        for attribute, names in ((self.size, size), (self.color, color), (self.material, material)):
            if names:
                selection[attribute.id] = attribute.value_ids.filtered(lambda v: v.name in names).ids
        return selection

    def _rows(self, tmpl, prefix="SKU", **kw):
        return [dict({"product": p, "sku": "%s-%s" % (prefix, i), "price": 10.0 + i, "stock": 0.0}, **kw)
                for i, p in enumerate(tmpl.product_variant_ids)]

    def _create_offers(self, tmpl, rows, fulfillment="fbm"):
        return self.Offer._leaders_create_variant_offers(
            self.vendor, tmpl, rows, fulfillment, self.env.company.currency_id, self.env["website"])


@tagged("post_install", "-at_install")
class TestVendorVariantsModel(VariantsCommon):

    def test_switch_and_defaults(self):
        Template = self.env["product.template"]
        self.assertTrue(Template._leaders_variants_enabled())
        self.param.set_param(ENABLED, False)
        self.assertFalse(Template._leaders_variants_enabled())
        self.assertFalse(Template._leaders_allow_vendor_attributes())
        self.param.set_param(MAX_IMAGES, False)
        self.assertEqual(Template._leaders_max_extra_images(), 8)

    def test_attribute_selection_generates_the_variants(self):
        tmpl = self._tmpl()
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M"), color=("Red", "Blue"), material=("Cotton",)))
        self.assertEqual(len(tmpl.product_variant_ids), 4)
        self.assertEqual(len(tmpl.attribute_line_ids), 3)

    def test_dynamic_attributes_and_unknown_values_are_refused(self):
        tmpl = self._tmpl()
        with self.assertRaises(UserError):
            tmpl._leaders_apply_attribute_selection({self.dynamic.id: self.dynamic.value_ids.ids})
        with self.assertRaises(UserError):
            tmpl._leaders_apply_attribute_selection({self.size.id: self.color.value_ids.ids})
        self.assertFalse(tmpl.attribute_line_ids)

    def test_too_many_variants_are_refused(self):
        tmpl = self._tmpl()
        many = self.env["product.attribute"].create({"name": "B3 Many", "create_variant": "always", "value_ids": [
            Command.create({"name": "v%s" % i}) for i in range(101)]})
        with self.assertRaises(UserError):
            tmpl._leaders_apply_attribute_selection({many.id: many.value_ids.ids})

    def test_one_offer_per_variant_with_own_sku_price_stock_and_discount(self):
        self.param.set_param("leaders_marketplace.vendor_discounts_enabled", "True")
        tmpl = self._tmpl()
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M"), color=("Red",)))
        rows = self._rows(tmpl)
        rows[0]["stock"] = 5.0
        rows[1]["stock"] = 2.0
        rows[1]["discount_percent"] = "20"
        offers = self._create_offers(tmpl, rows)
        self.assertEqual(len(offers), 2)
        self.assertEqual(offers.mapped("product_id"), tmpl.product_variant_ids)
        self.assertEqual(offers.mapped("seller_sku"), ["SKU-0", "SKU-1"])
        self.assertEqual(offers[0].price, 10.0)
        self.assertEqual(offers[1].price, 11.0)
        self.assertEqual(offers[0].available_qty(), 5.0)
        self.assertEqual(offers[1].available_qty(), 2.0)
        self.assertEqual(offers[1].discount_state, "active")
        self.assertAlmostEqual(offers[1].effective_price, 8.8)
        self.assertEqual(set(offers.mapped("state")), {"draft"})
        self.assertFalse(tmpl._leaders_variants_without_offer(self.vendor))

    def test_discount_needs_the_discount_switch(self):
        self.param.set_param("leaders_marketplace.vendor_discounts_enabled", False)
        tmpl = self._tmpl()
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M")))
        with self.assertRaises(UserError):
            self._create_offers(tmpl, self._rows(tmpl, discount_percent="10"))

    def test_sku_errors_write_nothing(self):
        tmpl = self._tmpl()
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M")))
        rows = self._rows(tmpl)
        rows[1]["sku"] = rows[0]["sku"]
        with self.assertRaises(UserError):
            self._create_offers(tmpl, rows)
        rows = self._rows(tmpl)
        rows[0]["sku"] = self.fbm_offer.seller_sku
        with self.assertRaises(UserError):
            self._create_offers(tmpl, rows)
        rows = self._rows(tmpl)
        rows[0]["sku"] = " "
        with self.assertRaises(UserError):
            self._create_offers(tmpl, rows)
        self.assertFalse(self.Offer.search([("product_tmpl_id", "=", tmpl.id)]))

    def test_product_limit_counts_one_per_variant_offer(self):
        current = self.Offer.search_count([("vendor_id", "=", self.vendor.id), ("active", "=", True)])
        self.tier.product_limit = current + 2
        tmpl = self._tmpl()
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M", "L")))
        with self.assertRaises(UserError):
            self._create_offers(tmpl, self._rows(tmpl))
        self.assertEqual(len(self._create_offers(tmpl, self._rows(tmpl)[:2])), 2)

    def test_fbl_refused_when_disabled_and_no_fbl_stock(self):
        tmpl = self._tmpl()
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M")))
        self.param.set_param("leaders_marketplace.fbl_enabled", "False")
        with self.assertRaises(UserError):
            self._create_offers(tmpl, self._rows(tmpl), fulfillment="fbl")
        self.param.set_param("leaders_marketplace.fbl_enabled", "True")
        rows = self._rows(tmpl)
        rows[0]["stock"] = 3.0
        with self.assertRaises(UserError):
            self._create_offers(tmpl, rows, fulfillment="fbl")

    def test_ownership_rules(self):
        tmpl = self._tmpl()
        other = self.env["res.partner"].create({"name": "B3 Other", "is_marketplace_vendor": True, "vendor_state": "approved"})
        other._marketplace_ensure_vendor_stock()
        self.assertTrue(tmpl._leaders_vendor_owns_template(self.vendor))
        self.assertFalse(tmpl._leaders_vendor_owns_template(other))
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M")))
        product = tmpl.product_variant_ids[0]
        self.Offer.create({
            "product_tmpl_id": tmpl.id, "product_id": product.id, "vendor_id": other.id,
            "seller_sku": "B3-OTHER", "price": 5, "fulfillment_type": "fbm",
            "stock_location_id": other._marketplace_stock_location("fbm").id})
        self.assertFalse(tmpl._leaders_vendor_owns_template(self.vendor))

    def test_adding_a_value_makes_a_variant_without_offer_and_removal_is_refused(self):
        tmpl = self._tmpl()
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M")))
        self._create_offers(tmpl, self._rows(tmpl))
        # removal refused while offers exist
        with self.assertRaises(UserError):
            tmpl._leaders_apply_attribute_selection(self._sel(size=("S",)))
        with self.assertRaises(UserError):
            tmpl._leaders_apply_attribute_selection({})
        # a new option would archive the offered variants: refused too
        with self.assertRaises(UserError):
            tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M"), color=("Red",)))
        self.assertEqual(len(tmpl.product_variant_ids), 2)
        # a new value of an existing option is fine
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M", "L")))
        missing = tmpl._leaders_variants_without_offer(self.vendor)
        self.assertEqual(len(missing), 1)
        self.assertEqual(len(self._create_offers(tmpl, [{"product": missing, "sku": "B3-L", "price": 9.0}])), 1)

    def test_removal_is_allowed_before_any_offer_exists(self):
        tmpl = self._tmpl()
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M", "L")))
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S",)))
        self.assertEqual(len(tmpl.product_variant_ids), 1)

    def test_new_values_and_attributes_only_when_allowed(self):
        Template = self.env["product.template"]
        with self.assertRaises(UserError):
            Template._leaders_create_vendor_attribute_values(self.size, ["XL"])
        with self.assertRaises(UserError):
            Template._leaders_create_vendor_attribute("B3 Brand New", ["a"])
        self.param.set_param(NEW_ATTRS, "True")
        values = Template._leaders_create_vendor_attribute_values(self.size, ["XL", "xl", " M "])
        self.assertEqual(sorted(values.mapped("name")), ["M", "XL"])
        self.assertEqual(len(self.size.value_ids.filtered(lambda v: v.name == "M")), 1)
        attribute, values = Template._leaders_create_vendor_attribute("B3 Brand New", ["a", "b"])
        self.assertEqual(attribute.create_variant, "always")
        self.assertEqual(len(values), 2)
        attribute, _values = Template._leaders_create_vendor_attribute("B3 Spec", ["x"], as_spec=True)
        self.assertEqual(attribute.create_variant, "no_variant")
        with self.assertRaises(UserError):
            Template._leaders_create_vendor_attribute("n" * 65, ["a"])
        with self.assertRaises(UserError):
            Template._leaders_create_vendor_attribute_values(self.size, ["v%s" % i for i in range(21)])

    def test_specs_are_cleaned_bounded_and_parsed(self):
        Template = self.env["product.template"]
        self.assertEqual(Template._leaders_clean_specs("  Material:   Cotton \n\n Weight: 2 kg "),
                         "Material: Cotton\nWeight: 2 kg")
        with self.assertRaises(UserError):
            Template._leaders_clean_specs("\n".join("a: %s" % i for i in range(51)))
        with self.assertRaises(UserError):
            Template._leaders_clean_specs("a: " + "x" * 4001)
        tmpl = self._tmpl()
        tmpl.leaders_specs = "Material: Cotton\nNo colon here"
        self.assertEqual(tmpl._leaders_specs_rows(), [("Material", "Cotton"), ("", "No colon here")])

    def test_extra_images_limit_and_removal(self):
        tmpl = self._tmpl()
        image = self.env["product.image"]
        import base64
        b64 = base64.b64encode(PNG_1PX)
        tmpl._leaders_check_extra_images(3)
        with self.assertRaises(UserError):
            tmpl._leaders_check_extra_images(4)
        for i in range(3):
            tmpl._leaders_add_extra_image(b64, name="i%s" % i)
        self.assertEqual(len(tmpl.product_template_image_ids), 3)
        with self.assertRaises(UserError):
            tmpl._leaders_check_extra_images(1)
        first = tmpl.product_template_image_ids[0]
        tmpl._leaders_check_extra_images(1, remove_ids=[first.id])
        # another template's image cannot be removed through this one
        foreign = image.create({"name": "f", "image_1920": b64, "product_tmpl_id": self._tmpl(name="B3 Other").id})
        tmpl._leaders_remove_extra_images([first.id, foreign.id])
        self.assertFalse(first.exists())
        self.assertTrue(foreign.exists())


@tagged("post_install", "-at_install")
class TestVendorVariantsPortal(HttpCase, VariantsCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor_b = cls.env["res.partner"].create({
            "name": "B3 Second Seller", "is_marketplace_vendor": True, "vendor_state": "approved"})
        cls.vendor_b._marketplace_ensure_vendor_stock()
        cls.owner_b = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "B3 Owner B", "login": "b3.owner.b@example.com", "email": "b3.owner.b@example.com",
            "password": PORTAL_PASSWORD,
            "partner_id": cls.env["res.partner"].create({
                "name": "B3 Owner B", "email": "b3.owner.b@example.com",
                "parent_id": cls.vendor_b.id, "type": "contact"}).id,
            "groups_id": [Command.set([cls.env.ref("leaders_marketplace.group_vendor_owner").id])]})

    def setUp(self):
        super().setUp()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self.currency = str(self.env.company.currency_id.id)

    def _new(self, name="B3 Portal Shirt", sku="B3P", files=None, **extra):
        data = {"name": name, "seller_sku": sku, "price": "20", "fulfillment_type": "fbm",
                "currency_id": self.currency, "lm_variants_form": "1"}
        data.update(extra)
        return self._post("/my/vendor/products/new", "/my/vendor/products/new", files=files, **data)

    def _template(self, name="B3 Portal Shirt"):
        return self.env["product.template"].search([("name", "=", name)])

    def _variants_url(self, tmpl):
        return "/my/vendor/templates/%s/variants" % tmpl.id

    def test_form_shows_the_options_card_only_when_enabled(self):
        page = self.url_open("/my/vendor/products/new")
        self.assertIn("lm_variants_card", page.text)
        self.assertIn("lm_extra_images_card", page.text)
        self.assertIn("lm_specs_card", page.text)
        self.assertIn("B3 Size", page.text)
        self.assertNotIn("new_attr_name", page.text)
        self.param.set_param(ENABLED, False)
        page = self.url_open("/my/vendor/products/new")
        self.assertNotIn("lm_variants_card", page.text)
        self.assertNotIn("lm_specs_card", page.text)
        self.assertIn('name="seller_sku"', page.text)

    def test_new_attribute_inputs_need_the_setting(self):
        self.param.set_param(NEW_ATTRS, "True")
        self.assertIn("new_attr_name", self.url_open("/my/vendor/products/new").text)

    def test_create_with_variants_then_offers_then_submit(self):
        sizes = self.size.value_ids.filtered(lambda v: v.name in ("S", "M")).ids
        colors = self.color.value_ids.filtered(lambda v: v.name == "Red").ids
        response = self._new(**{"attr_values_%s" % self.size.id: sizes,
                                "attr_values_%s" % self.color.id: colors,
                                "leaders_specs": "Material: Cotton"})
        tmpl = self._template()
        self.assertEqual(len(tmpl.product_variant_ids), 2)
        self.assertEqual(tmpl.leaders_specs, "Material: Cotton")
        self.assertEqual(tmpl.marketplace_owner_vendor_id, self.vendor)
        self.assertFalse(tmpl.marketplace_offer_ids)
        self.assertIn("Variants of", response.text)
        self.assertIn("lm_variants_without_offer", response.text)
        self.assertIn("B3P-1", response.text)  # prefilled from the base SKU
        p1, p2 = tmpl.product_variant_ids
        self._post(self._variants_url(tmpl), self._variants_url(tmpl),
                   fulfillment_type="fbm", currency_id=self.currency,
                   **{"create_%s" % p1.id: "1", "sku_%s" % p1.id: "B3P-S", "price_%s" % p1.id: "20",
                      "stock_%s" % p1.id: "4",
                      "create_%s" % p2.id: "1", "sku_%s" % p2.id: "B3P-M", "price_%s" % p2.id: "25",
                      "stock_%s" % p2.id: "0"})
        offers = tmpl.marketplace_offer_ids
        self.assertEqual(sorted(offers.mapped("seller_sku")), ["B3P-M", "B3P-S"])
        self.assertEqual(offers.filtered(lambda o: o.seller_sku == "B3P-S").available_qty(), 4.0)
        self.assertEqual(offers.filtered(lambda o: o.seller_sku == "B3P-M").price, 25.0)
        self._post("/my/vendor/templates/%s/submit" % tmpl.id, self._variants_url(tmpl))
        self.assertEqual(set(offers.mapped("state")), {"pending"})
        self.assertEqual(tmpl.marketplace_state, "pending")

    def test_duplicate_sku_shows_an_error_and_creates_nothing(self):
        self._new(**{"attr_values_%s" % self.size.id: self.size.value_ids.ids[:2]})
        tmpl = self._template()
        p1, p2 = tmpl.product_variant_ids
        response = self._post(self._variants_url(tmpl), self._variants_url(tmpl),
                              fulfillment_type="fbm", currency_id=self.currency,
                              **{"create_%s" % p1.id: "1", "sku_%s" % p1.id: self.fbm_offer.seller_sku,
                                 "price_%s" % p1.id: "20",
                                 "create_%s" % p2.id: "1", "sku_%s" % p2.id: "OK-2", "price_%s" % p2.id: "20"})
        self.assertIn("already used", response.text)
        self.assertFalse(tmpl.marketplace_offer_ids)

    def test_single_variant_flow_is_unchanged(self):
        response = self._new(name="B3 Single", sku="B3-SINGLE", leaders_specs="Weight: 1 kg")
        offer = self.Offer.search([("seller_sku", "=", "B3-SINGLE")])
        self.assertEqual(len(offer), 1)
        self.assertEqual(len(offer.product_tmpl_id.product_variant_ids), 1)
        self.assertEqual(offer.state, "draft")
        self.assertEqual(offer.product_tmpl_id.leaders_specs, "Weight: 1 kg")
        self.assertIn("Edit Product", response.text)

    def test_switch_off_ignores_the_options_and_refuses_the_pages(self):
        self.param.set_param(ENABLED, False)
        self._new(name="B3 Off", sku="B3-OFF", leaders_specs="A: b",
                  **{"attr_values_%s" % self.size.id: self.size.value_ids.ids[:2]})
        offer = self.Offer.search([("seller_sku", "=", "B3-OFF")])
        self.assertEqual(len(offer), 1)
        self.assertFalse(offer.product_tmpl_id.attribute_line_ids)
        self.assertFalse(offer.product_tmpl_id.leaders_specs)
        self.assertEqual(self.url_open(self._variants_url(offer.product_tmpl_id)).status_code, 404)

    def test_another_vendor_cannot_open_or_change_the_template(self):
        self._new(**{"attr_values_%s" % self.size.id: self.size.value_ids.ids[:2]})
        tmpl = self._template()
        self.authenticate(self.owner_b.login, PORTAL_PASSWORD)
        self.assertEqual(self.url_open(self._variants_url(tmpl)).status_code, 403)
        page = self.url_open("/my/vendor/products/new")
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page.text).group(1)
        for url in (self._variants_url(tmpl), "/my/vendor/templates/%s/submit" % tmpl.id):
            response = self.url_open(url, data={"csrf_token": token})
            self.assertEqual(response.status_code, 403, url)
        self.assertFalse(tmpl.marketplace_offer_ids)

    def test_a_template_shared_with_another_vendor_is_refused(self):
        tmpl = self._tmpl()
        tmpl._leaders_apply_attribute_selection(self._sel(size=("S", "M")))
        product = tmpl.product_variant_ids[0]
        self.Offer.create({
            "product_tmpl_id": tmpl.id, "product_id": product.id, "vendor_id": self.vendor_b.id,
            "seller_sku": "B3-SHARED", "price": 5, "fulfillment_type": "fbm",
            "stock_location_id": self.vendor_b._marketplace_stock_location("fbm").id})
        self.assertEqual(self.url_open(self._variants_url(tmpl)).status_code, 403)

    def test_edit_adds_specs_and_extra_images_within_the_limit(self):
        self._new(name="B3 Edit", sku="B3-EDIT")
        offer = self.Offer.search([("seller_sku", "=", "B3-EDIT")])
        tmpl = offer.product_tmpl_id
        url = "/my/vendor/products/%s/edit" % offer.id
        base = {"name": "B3 Edit", "seller_sku": "B3-EDIT", "price": "20", "fulfillment_type": "fbm",
                "currency_id": self.currency, "lm_variants_form": "1"}
        pngs = [("extra_images", ("a%s.png" % i, PNG_1PX, "image/png")) for i in range(2)]
        self._post(url, url, files=pngs, leaders_specs="Material: Cotton\nWeight: 1 kg", **base)
        self.assertEqual(len(tmpl.product_template_image_ids), 2)
        self.assertEqual(tmpl.leaders_specs, "Material: Cotton\nWeight: 1 kg")
        # a 4th image is over the limit of 3: refused, nothing stored
        over = [("extra_images", ("b%s.png" % i, PNG_1PX, "image/png")) for i in range(2)]
        response = self._post(url, url, files=over, **base)
        self.assertIn("at most 3 extra images", response.text)
        self.assertEqual(len(tmpl.product_template_image_ids), 2)
        # not an image
        response = self._post(url, url, files=[("extra_images", ("x.png", b"<?php ?>" * 9, "image/png"))], **base)
        self.assertIn("Only image files", response.text)
        # removing one and adding two fits
        first = tmpl.product_template_image_ids[0]
        self._post(url, url, files=over, remove_image_ids=str(first.id), **base)
        self.assertEqual(len(tmpl.product_template_image_ids), 3)
        self.assertFalse(first.exists())

    def test_edit_of_a_shared_template_ignores_the_b3_fields(self):
        self._new(name="B3 Shared", sku="B3-SH-A")
        offer = self.Offer.search([("seller_sku", "=", "B3-SH-A")])
        tmpl = offer.product_tmpl_id
        self.Offer.create({
            "product_tmpl_id": tmpl.id, "product_id": tmpl.product_variant_id.id, "vendor_id": self.vendor_b.id,
            "seller_sku": "B3-SH-B", "price": 5, "fulfillment_type": "fbm",
            "stock_location_id": self.vendor_b._marketplace_stock_location("fbm").id})
        url = "/my/vendor/products/%s/edit" % offer.id
        self.assertNotIn("lm_variants_card", self.url_open(url).text)
        self._post(url, url, name="B3 Shared", seller_sku="B3-SH-A", price="21", fulfillment_type="fbm",
                   currency_id=self.currency, lm_variants_form="1", leaders_specs="Hack: yes")
        self.assertFalse(tmpl.leaders_specs)
        self.assertEqual(offer.price, 21.0)

    def test_specs_and_extra_images_render_on_the_product_page(self):
        tmpl = self._tmpl(name="B3 Website Shirt")
        tmpl.write({"is_published": True, "marketplace_state": "published",
                    "leaders_specs": "Material: Cotton\nOrigin: <b>Egypt</b>"})
        tmpl._leaders_add_extra_image(__import__("base64").b64encode(PNG_1PX), name="gallery")
        self.authenticate(None, None)
        response = self.url_open("/shop/%s" % tmpl.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn("lm_product_specs", response.text)
        self.assertIn("Material", response.text)
        self.assertIn("Cotton", response.text)
        self.assertNotIn("<b>Egypt</b>", response.text)
        self.assertIn("/web/image/product.image/%s/" % tmpl.product_template_image_ids.id, response.text)
        tmpl.leaders_specs = False
        self.assertNotIn("lm_product_specs", self.url_open("/shop/%s" % tmpl.id).text)
