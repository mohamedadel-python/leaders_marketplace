"""A vendor may only edit product content they own outright.

``vendor_product_edit`` / ``vendor_product_submit`` write the product template
(name, description, image, category, publication) with sudo after checking
ownership of the *offer*. On a template shared with another seller -- or on a
marketplace catalogue product -- that would let one vendor rename or unpublish
a product for everybody. The template write is now gated on
``product.template._leaders_vendor_owns_content``.
"""
import re

from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD

# Smallest valid PNG: guess_mimetype reads the signature, not the filename.
PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
    b"\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
    b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class SharedTemplateCommon(MarketplacePortalUserCommon):
    """Vendor A owns a portal product; vendor B later lists on the same one."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor_b = cls.env["res.partner"].create({
            "name": "Second Seller", "is_marketplace_vendor": True, "vendor_state": "approved",
        })
        cls.vendor_b._marketplace_ensure_vendor_stock()
        cls.owner_b = cls._create_vendor_user_for(
            cls.vendor_b, "LM Owner B", "lm.owner.b@example.com")

        # A product vendor A created from the portal: sole content owner.
        cls.solo_tmpl = cls.env["product.template"].create({
            "name": "Solo Product", "type": "consu", "is_storable": True,
            "sale_ok": True, "marketplace_state": "published", "is_published": True,
            "marketplace_owner_vendor_id": cls.vendor.id,
        })
        cls.solo_offer = cls._offer_for(cls.vendor, cls.solo_tmpl, "SOLO-A")

        # The same story, until vendor B lists on it too.
        cls.shared_tmpl = cls.env["product.template"].create({
            "name": "Shared Product", "type": "consu", "is_storable": True,
            "sale_ok": True, "marketplace_state": "published", "is_published": True,
            "marketplace_owner_vendor_id": cls.vendor.id,
        })
        cls.shared_offer_a = cls._offer_for(cls.vendor, cls.shared_tmpl, "SHARED-A")
        cls.shared_offer_b = cls._offer_for(cls.vendor_b, cls.shared_tmpl, "SHARED-B")

    @classmethod
    def _create_vendor_user_for(cls, vendor, name, login):
        contact = cls.env["res.partner"].create({
            "name": name, "email": login, "parent_id": vendor.id, "type": "contact",
        })
        return cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": name, "login": login, "email": login, "password": PORTAL_PASSWORD,
            "partner_id": contact.id,
            "groups_id": [Command.set([
                cls.env.ref("leaders_marketplace.group_vendor_owner").id])],
        })

    @classmethod
    def _offer_for(cls, vendor, tmpl, sku):
        return cls.env["leaders.marketplace.offer"].create({
            "product_tmpl_id": tmpl.id,
            "product_id": tmpl.product_variant_id.id,
            "vendor_id": vendor.id,
            "seller_sku": sku,
            "price": 50.0,
            "fulfillment_type": "fbm",
            "stock_location_id": vendor._marketplace_stock_location("fbm").id,
        })


@tagged("post_install", "-at_install")
class TestContentOwnership(SharedTemplateCommon):
    """The model-level rule the controller leans on."""

    def test_sole_portal_owner_owns_the_content(self):
        self.assertTrue(self.solo_tmpl._leaders_vendor_owns_content(self.vendor))

    def test_a_second_seller_takes_the_content_away_from_everyone(self):
        self.assertFalse(self.shared_tmpl._leaders_vendor_owns_content(self.vendor))
        self.assertFalse(self.shared_tmpl._leaders_vendor_owns_content(self.vendor_b))

    def test_an_archived_rival_offer_still_counts(self):
        """Archiving the other offer must not hand the content back."""
        self.shared_offer_b.active = False
        self.assertFalse(self.shared_tmpl._leaders_vendor_owns_content(self.vendor))

    def test_unstamped_template_belongs_to_nobody(self):
        """A catalogue product a vendor merely lists on is not theirs to edit."""
        catalogue = self.env["product.template"].create({
            "name": "Catalogue Product", "type": "consu", "is_storable": True,
        })
        self._offer_for(self.vendor, catalogue, "CAT-A")
        self.assertFalse(catalogue.marketplace_owner_vendor_id)
        self.assertFalse(catalogue._leaders_vendor_owns_content(self.vendor))

    def test_owner_of_a_template_with_no_offer_owns_nothing(self):
        empty = self.env["product.template"].create({
            "name": "Stamped But Empty", "type": "consu",
            "marketplace_owner_vendor_id": self.vendor.id,
        })
        self.assertFalse(empty._leaders_vendor_owns_content(self.vendor))


@tagged("post_install", "-at_install")
class TestVendorProductEditRoutes(HttpCase, SharedTemplateCommon):

    def setUp(self):
        super().setUp()
        self.authenticate(self.owner_b.login, PORTAL_PASSWORD)

    def _edit(self, offer, **overrides):
        url = "/my/vendor/products/%s/edit" % offer.id
        data = {
            "name": "Renamed By Vendor",
            "seller_sku": offer.seller_sku,
            "price": "77",
            "currency_id": str(offer.currency_id.id),
            "fulfillment_type": "fbm",
            "description_sale": "Rewritten description",
        }
        data.update(overrides)
        return self._post(url, url, **data)

    # --------------------------------------------------- the shared template
    def test_other_seller_cannot_rename_or_unpublish_a_shared_template(self):
        self._edit(self.shared_offer_b)
        self.assertEqual(self.shared_tmpl.name, "Shared Product")
        self.assertTrue(self.shared_tmpl.is_published)
        self.assertEqual(self.shared_tmpl.marketplace_state, "published")
        self.assertFalse(self.shared_tmpl.description_sale)
        # Their own offer is still updated, exactly as before.
        self.assertEqual(self.shared_offer_b.price, 77.0)
        self.assertEqual(self.shared_offer_b.state, "draft")

    def test_original_owner_also_loses_content_rights_once_shared(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self._edit(self.shared_offer_a)
        self.assertEqual(self.shared_tmpl.name, "Shared Product")
        self.assertTrue(self.shared_tmpl.is_published)
        self.assertEqual(self.shared_offer_a.price, 77.0)

    def test_submit_does_not_unpublish_a_shared_template(self):
        url = "/my/vendor/products/%s/submit" % self.shared_offer_b.id
        self._post(url, "/my/vendor/products/%s/edit" % self.shared_offer_b.id)
        self.assertTrue(self.shared_tmpl.is_published)
        self.assertEqual(self.shared_tmpl.marketplace_state, "published")
        self.assertEqual(self.shared_offer_b.state, "pending")

    def _name_input(self, page_text):
        """The <input name="name"> element of the product form, as rendered."""
        match = re.search(r'<input[^>]*name="name"[^>]*>', page_text)
        self.assertTrue(match, "product name input not found on the page")
        return match.group(0)

    def test_shared_form_shows_the_notice_and_locks_the_template_fields(self):
        page = self.url_open("/my/vendor/products/%s/edit" % self.shared_offer_b.id)
        self.assertIn("you can edit your offer only", page.text)
        # Asserted on the element itself: a bare "readonly" substring search
        # would also match unrelated portal markup.
        self.assertIn("readonly", self._name_input(page.text))
        self.assertRegex(page.text, r'<select[^>]*name="public_category_id"[^>]*disabled')
        self.assertRegex(page.text, r'<input[^>]*name="image_1920"[^>]*disabled')

    # ----------------------------------------------------- the owned template
    def test_sole_owner_still_renames_resets_and_unpublishes(self):
        """MUST NOT CHANGE: today's behavior for vendor-owned products."""
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self._edit(self.solo_offer)
        self.assertEqual(self.solo_tmpl.name, "Renamed By Vendor")
        self.assertEqual(self.solo_tmpl.description_sale, "Rewritten description")
        self.assertFalse(self.solo_tmpl.is_published)
        self.assertEqual(self.solo_tmpl.marketplace_state, "draft")
        self.assertEqual(self.solo_offer.state, "draft")

    def test_sole_owner_submit_still_unpublishes(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        url = "/my/vendor/products/%s/submit" % self.solo_offer.id
        self._post(url, "/my/vendor/products/%s/edit" % self.solo_offer.id)
        self.assertFalse(self.solo_tmpl.is_published)
        self.assertEqual(self.solo_tmpl.marketplace_state, "pending")
        self.assertEqual(self.solo_offer.state, "pending")

    def test_owned_form_is_fully_editable(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/products/%s/edit" % self.solo_offer.id)
        self.assertNotIn("you can edit your offer only", page.text)
        self.assertNotIn("readonly", self._name_input(page.text))
        self.assertNotRegex(page.text, r'<select[^>]*name="public_category_id"[^>]*disabled')

    # ------------------------------------------------------- image hardening
    def test_non_image_upload_is_refused_with_a_form_error(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        original = self.solo_tmpl.name
        response = self._edit(
            self.solo_offer,
            files={"image_1920": ("payload.png", b"<?php echo 1; ?>" * 8, "image/png")},
        )
        self.assertIn("Only image files", response.text)
        # The whole edit is refused, not just the image.
        self.assertEqual(self.solo_tmpl.name, original)
        self.assertNotEqual(self.solo_offer.price, 77.0)

    def test_oversized_image_is_refused_with_a_form_error(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.vendor_image_max_mb", 1)
        oversized = PNG_1PX + b"\x00" * (1024 * 1024 + 1)
        response = self._edit(
            self.solo_offer, files={"image_1920": ("big.png", oversized, "image/png")})
        self.assertIn("too large", response.text)
        self.assertEqual(self.solo_tmpl.name, "Solo Product")

    def test_a_real_image_is_still_accepted(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self._edit(self.solo_offer,
                   files={"image_1920": ("tiny.png", PNG_1PX, "image/png")})
        self.assertEqual(self.solo_tmpl.name, "Renamed By Vendor")
        self.assertTrue(self.solo_tmpl.image_1920)

    def test_non_image_is_refused_on_create_too(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        url = "/my/vendor/products/new"
        before = self.env["leaders.marketplace.offer"].search_count([])
        response = self._post(
            url, url, name="Bad Upload", seller_sku="BAD-1", price="10",
            fulfillment_type="fbm", currency_id=str(self.env.company.currency_id.id),
            files={"image_1920": ("x.txt", b"just text, not an image", "image/png")},
        )
        self.assertIn("Only image files", response.text)
        self.assertEqual(self.env["leaders.marketplace.offer"].search_count([]), before)


@tagged("post_install", "-at_install")
class TestPortalCreateStampsTheOwner(HttpCase, MarketplacePortalUserCommon):

    def test_portal_created_product_is_owned_by_its_vendor(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        url = "/my/vendor/products/new"
        self._post(url, url, name="Stamped Product", seller_sku="STAMP-1", price="12",
                   fulfillment_type="fbm",
                   currency_id=str(self.env.company.currency_id.id))
        offer = self.env["leaders.marketplace.offer"].search([("seller_sku", "=", "STAMP-1")])
        self.assertEqual(offer.product_tmpl_id.marketplace_owner_vendor_id, self.vendor)
        self.assertTrue(offer.product_tmpl_id._leaders_vendor_owns_content(self.vendor))
