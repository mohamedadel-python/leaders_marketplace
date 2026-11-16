"""The vendor product form carries the catalog data the review needs (brand, condition, equipment
specs, used-equipment proof), so a vendor can submit a used machine without the back office."""
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD
from .test_vendor_product_content import PNG_1PX

URL = "/my/vendor/products/new"


@tagged("post_install", "-at_install")
class TestVendorProductSpec(HttpCase, MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.gen = env.ref("leaders_marketplace.leaders_categ_generators")
        cls.brand = env["leaders.brand"].create({"name": "SpecBrand"})
        cls.model = env["leaders.equipment.model"].create({"name": "SPEC-1", "brand_id": cls.brand.id})
        cls.source = env["leaders.source"].search([], limit=1)

    def setUp(self):
        super().setUp()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)

    def _form(self, sku, **spec):
        data = dict(name="Spec Generator", seller_sku=sku, price="5000", fulfillment_type="fbm",
                    currency_id=str(self.env.company.currency_id.id), public_category_id=str(self.gen.id),
                    lm_spec_form="1", leaders_brand_id=str(self.brand.id), leaders_source_id=str(self.source.id),
                    leaders_condition="used", leaders_model_id=str(self.model.id), leaders_capacity="250",
                    leaders_capacity_unit="kva", leaders_warranty_months="6", leaders_warranty_provider="vendor",
                    leaders_year="2019", leaders_hours="4200", leaders_video_url="https://youtu.be/abc123")
        data.update(spec)
        return data

    def _photos(self):
        return {"leaders_meter_photo": ("meter.png", PNG_1PX, "image/png"),
                "leaders_plate_photo": ("plate.png", PNG_1PX, "image/png")}

    def _tmpl(self, sku):
        return self.env["leaders.marketplace.offer"].search([("seller_sku", "=", sku)]).product_tmpl_id

    def test_the_form_shows_the_catalog_section(self):
        page = self.url_open(URL).text
        self.assertIn('id="lm_spec_card"', page)
        self.assertIn('name="leaders_meter_photo"', page)
        self.assertIn('data-field-set="equipment"', page)

    def test_a_used_machine_from_the_portal_can_be_submitted_and_approved(self):
        self._post(URL, URL, files=self._photos(), **self._form("SPEC-USED"))
        tmpl = self._tmpl("SPEC-USED")
        self.assertEqual((tmpl.leaders_brand_id, tmpl.leaders_model_id, tmpl.leaders_condition, tmpl.leaders_year,
                          tmpl.leaders_hours, tmpl.leaders_capacity, tmpl.leaders_capacity_unit),
                         (self.brand, self.model, "used", 2019, 4200, 250.0, "kva"))
        self.assertTrue(tmpl.leaders_meter_photo and tmpl.leaders_plate_photo)
        self.assertEqual(tmpl._leaders_catalog_problems(), [])
        self.assertEqual(tmpl._leaders_listing_problems(), [])

    def test_editing_keeps_saved_photos_and_clears_emptied_fields(self):
        self._post(URL, URL, files=self._photos(), **self._form("SPEC-EDIT"))
        tmpl = self._tmpl("SPEC-EDIT")
        offer = self.env["leaders.marketplace.offer"].search([("seller_sku", "=", "SPEC-EDIT")])
        url = "/my/vendor/products/%s/edit" % offer.id
        self._post(url, url, **self._form("SPEC-EDIT", leaders_video_url="", leaders_hours="5000"))
        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.leaders_hours, 5000)
        self.assertFalse(tmpl.leaders_video_url)
        self.assertTrue(tmpl.leaders_meter_photo, "an empty file input keeps the saved photo")

    def test_invalid_values_are_refused_with_a_message(self):
        page = self._post(URL, URL, **self._form("SPEC-BAD-YEAR", leaders_year="1800")).text
        self.assertIn("year of manufacture", page.lower())
        page = self._post(URL, URL, **self._form("SPEC-BAD-VIDEO", leaders_video_url="javascript:alert(1)")).text
        self.assertIn("http://", page)
        self.assertFalse(self._tmpl("SPEC-BAD-YEAR") or self._tmpl("SPEC-BAD-VIDEO"))

    def test_a_form_without_the_section_leaves_the_catalog_fields_alone(self):
        data = self._form("SPEC-NONE")
        for key in [k for k in data if k.startswith("leaders_") or k == "lm_spec_form"]:
            data.pop(key)
        self._post(URL, URL, **data)
        tmpl = self._tmpl("SPEC-NONE")
        self.assertTrue(tmpl)
        self.assertFalse(tmpl.leaders_brand_id or tmpl.leaders_condition)

    def _offer(self, sku):
        return self.env["leaders.marketplace.offer"].search([("seller_sku", "=", sku)])

    def test_an_incomplete_product_cannot_be_submitted_and_says_what_is_missing(self):
        self._post(URL, URL, **self._form("SPEC-HALF", leaders_year="", leaders_video_url=""))
        offer = self._offer("SPEC-HALF")
        listing = self.url_open("/my/vendor/products").text
        self.assertIn("/my/vendor/products/%s/edit#lm_submit_check" % offer.id, listing)
        self.assertNotIn("/my/vendor/products/%s/submit" % offer.id, listing)
        page = self.url_open("/my/vendor/products/%s/edit" % offer.id).text
        self.assertIn("Year of manufacture is required for used equipment.", page)
        self.assertNotIn('action="/my/vendor/products/%s/submit"' % offer.id, page)
        # posting the submit anyway lands back on the form, not on an error page
        token = self._csrf("/my/vendor/products/%s/edit" % offer.id)
        r = self.url_open("/my/vendor/products/%s/submit" % offer.id, data={"csrf_token": token}, allow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/my/vendor/products/%s/edit?incomplete=1" % offer.id, r.headers["Location"])
        self.assertEqual(offer.product_tmpl_id.marketplace_state, "draft")
        refused = self.url_open(r.headers["Location"]).text
        self.assertIn("The product was not sent for review", refused)

    def test_a_complete_product_offers_the_submit_button_and_goes_to_review(self):
        self._post(URL, URL, files=self._photos(), **self._form("SPEC-FULL"))
        offer = self._offer("SPEC-FULL")
        page = self.url_open("/my/vendor/products/%s/edit" % offer.id).text
        self.assertIn('action="/my/vendor/products/%s/submit"' % offer.id, page)
        self.assertIn("ready for review", page)
        url = "/my/vendor/products/%s/edit" % offer.id
        token = self._csrf(url)
        r = self.url_open("/my/vendor/products/%s/submit" % offer.id, data={"csrf_token": token}, allow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["Location"].endswith("/my/vendor/products"))
        self.assertEqual(offer.product_tmpl_id.marketplace_state, "pending")


@tagged("post_install", "-at_install")
class TestVendorProductFormReview(TestVendorProductSpec):
    """The product form after the tester's review (BUG-00864): clearer choices, fewer fields, more data."""

    def test_the_form_has_the_new_fields_and_not_the_removed_ones(self):
        page = self.url_open(URL).text
        for present in ('name="leaders_brand_other"', 'name="leaders_model_other"', 'value="__other__"',
                        'data-brand="%s"' % self.brand.id, 'name="leaders_serial"', 'name="leaders_listing_type"',
                        'name="leaders_rent_price_day"', 'name="lm_stock_qty"', 'name="gallery_images"',
                        'id="lm_used_notice"', 'Choose a brand'):
            self.assertIn(present, page)
        for gone in ('name="leaders_warranty_provider"', 'name="market_ids"', 'id="lm_pf_currency"', ">Select<"):
            self.assertNotIn(gone, page)
        self.assertIn('name="currency_id" value="%s"' % self.env.company.currency_id.id, page)

    def test_other_brand_and_model_are_created_from_the_typed_names(self):
        self._post(URL, URL, files=self._photos(), **self._form(
            "SPEC-OTHER", leaders_brand_id="__other__", leaders_brand_other="  Brand Nouvelle ",
            leaders_model_id="__other__", leaders_model_other="BN-500"))
        tmpl = self._tmpl("SPEC-OTHER")
        self.assertEqual((tmpl.leaders_brand_id.name, tmpl.leaders_model_id.name), ("Brand Nouvelle", "BN-500"))
        self.assertEqual(tmpl.leaders_model_id.brand_id, tmpl.leaders_brand_id)
        # typing an existing brand reuses it
        self._post(URL, URL, files=self._photos(), **self._form(
            "SPEC-OTHER2", leaders_brand_id="__other__", leaders_brand_other="specbrand"))
        self.assertEqual(self._tmpl("SPEC-OTHER2").leaders_brand_id, self.brand)
        page = self._post(URL, URL, **self._form("SPEC-OTHER3", leaders_brand_id="__other__", leaders_brand_other="")).text
        self.assertIn("Type the brand name", page)

    def test_a_warranty_without_a_provider_is_the_vendors(self):
        data = self._form("SPEC-WARR")
        data.pop("leaders_warranty_provider")
        self._post(URL, URL, files=self._photos(), **data)
        tmpl = self._tmpl("SPEC-WARR")
        self.assertEqual(tmpl.leaders_warranty_provider, "vendor")
        self.assertEqual(tmpl._leaders_listing_problems(), [])
        # a provider recorded by the platform is kept on edit
        tmpl.sudo().leaders_warranty_provider = "manufacturer"
        offer = self._offer("SPEC-WARR")
        url = "/my/vendor/products/%s/edit" % offer.id
        self._post(url, url, **data)
        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.leaders_warranty_provider, "manufacturer")

    def test_serial_rent_and_quantity(self):
        data = self._form("SPEC-RENT", leaders_condition="new", leaders_serial=" SN-0001 ",
                          leaders_listing_type="rent", leaders_rent_price_day="750", lm_stock_qty="4")
        self._post(URL, URL, **data)
        tmpl = self._tmpl("SPEC-RENT")
        offer = self._offer("SPEC-RENT")
        self.assertEqual((tmpl.leaders_serial, tmpl.leaders_listing_type, tmpl.leaders_rent_price_day),
                         ("SN-0001", "rent", 750.0))
        self.assertEqual(offer.available_qty(), 4)
        page = self._post(URL, URL, **self._form("SPEC-RENT-2", leaders_condition="new", leaders_serial="SN-0001")).text
        self.assertIn("already used by another product", page)
        page = self._post(URL, URL, **self._form("SPEC-RENT-3", leaders_condition="new",
                                                 leaders_listing_type="rent", leaders_rent_price_day="")).text
        self.assertIn("rent price per day", page)
        # the quantity is corrected from the form
        url = "/my/vendor/products/%s/edit" % offer.id
        self._post(url, url, **dict(data, lm_stock_qty="9"))
        self.env.invalidate_all()
        self.assertEqual(offer.available_qty(), 9)

    def test_a_used_item_is_one_piece(self):
        self._post(URL, URL, files=self._photos(), **self._form("SPEC-ONE", lm_stock_qty="7"))
        self.assertEqual(self._offer("SPEC-ONE").available_qty(), 1)

    def test_several_photos_at_once(self):
        files = [("gallery_images", ("a.png", PNG_1PX, "image/png")), ("gallery_images", ("b.png", PNG_1PX, "image/png"))]
        self._post(URL, URL, files=files, **self._form("SPEC-GAL", leaders_condition="new"))
        tmpl = self._tmpl("SPEC-GAL")
        self.assertEqual(len(tmpl.product_template_image_ids), 2)
        offer = self._offer("SPEC-GAL")
        url = "/my/vendor/products/%s/edit" % offer.id
        self._post(url, url, **dict(self._form("SPEC-GAL", leaders_condition="new"),
                                    gallery_remove_ids=str(tmpl.product_template_image_ids[0].id)))
        tmpl.invalidate_recordset()
        self.assertEqual(len(tmpl.product_template_image_ids), 1)

    def test_the_edit_form_shows_the_saved_image_and_the_website_price(self):
        """BUG-00887: after saving, the vendor saw "No file chosen" and thought the image was lost."""
        files = {"image_1920": ("main.png", PNG_1PX, "image/png")}
        self._post(URL, URL, files=files, **self._form("SPEC-IMG", leaders_condition="new"))
        offer = self._offer("SPEC-IMG")
        offer.product_tmpl_id.sudo().leaders_inspection_cost = 50
        page = self.url_open("/my/vendor/products/%s/edit" % offer.id).text
        self.assertIn('id="lm_pf_image_current"', page)
        self.assertIn('src="data:image/', page)  # shown from the record: works before the product is published
        self.assertIn('id="lm_pf_price_note"', page)
