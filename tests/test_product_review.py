"""The product review workflow, reachable from the back office.

The product state and the offer state are two separate workflows: approving an
offer makes that seller's listing live, approving the product puts it on the
website. Neither implies the other, which is why approving only the offer used
to leave the product invisible.
"""
import re

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tools.safe_eval import safe_eval
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD


class ProductReviewCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Offer = cls.env["leaders.marketplace.offer"]
        cls.reviewer = cls.env["res.users"].create({
            "name": "LM Reviewer", "login": "lm.review@example.com",
            "groups_id": [Command.set([
                cls.env.ref("base.group_user").id,
                cls.env.ref("leaders_marketplace.group_marketplace_reviewer").id,
            ])],
        })

    def _pending(self, sku="REV-1", price=50.0):
        """A product submitted by the vendor, with a pending offer on it."""
        product = self.env["product.product"].create({
            "name": "Submitted Product", "type": "consu", "is_storable": True,
            "list_price": 999.0, "taxes_id": [Command.clear()],
            "marketplace_state": "pending", "is_published": False,
            "marketplace_owner_vendor_id": self.vendor.id,
        })
        offer = self.Offer.create({
            "product_tmpl_id": product.product_tmpl_id.id,
            "product_id": product.id,
            "vendor_id": self.vendor.id,
            "seller_sku": sku,
            "price": price,
            "fulfillment_type": "fbm",
            "stock_location_id": self.vendor._marketplace_stock_location("fbm").id,
            "state": "pending",
        })
        return product.product_tmpl_id, offer


@tagged("post_install", "-at_install")
class TestProductReview(ProductReviewCommon):

    def test_a_reviewer_publishes_a_pending_product(self):
        template, offer = self._pending()
        template.with_user(self.reviewer).action_marketplace_approve()
        self.assertEqual(template.marketplace_state, "published")
        self.assertTrue(template.is_published)
        self.assertTrue(template.marketplace_publish_date)

    def test_rejecting_without_a_reason_raises(self):
        template, offer = self._pending()
        with self.assertRaises(UserError):
            template.with_user(self.reviewer).action_marketplace_reject()
        template.marketplace_reject_reason = "Photos are unusable"
        template.with_user(self.reviewer).action_marketplace_reject()
        self.assertEqual(template.marketplace_state, "rejected")
        self.assertFalse(template.is_published)

    def test_suspend_pulls_a_published_product_off_the_website(self):
        template, offer = self._pending()
        template.action_marketplace_approve()
        template.with_user(self.reviewer).action_marketplace_suspend()
        self.assertEqual(template.marketplace_state, "suspended")
        self.assertFalse(template.is_published)

    def test_back_to_draft(self):
        template, offer = self._pending()
        template.marketplace_reject_reason = "Needs work"
        template.action_marketplace_reject()
        template.with_user(self.reviewer).action_marketplace_reset()
        self.assertEqual(template.marketplace_state, "draft")
        self.assertFalse(template.is_published)

    def test_the_new_actions_are_back_office_only(self):
        template, offer = self._pending()
        template.action_marketplace_approve()
        for method in ("action_marketplace_suspend", "action_marketplace_reset"):
            with self.subTest(method=method), self.assertRaises(AccessError):
                getattr(template.with_user(self.owner_user), method)()


@tagged("post_install", "-at_install")
class TestApproveProductAndOffer(ProductReviewCommon):

    def test_approving_the_offer_alone_leaves_the_product_unpublished(self):
        """The behaviour that made this task necessary; it is unchanged."""
        template, offer = self._pending()
        offer.with_user(self.reviewer).action_approve()
        self.assertEqual(offer.state, "active")
        self.assertEqual(template.marketplace_state, "pending")
        self.assertFalse(template.is_published)

    def test_approve_product_and_offer_does_both(self):
        template, offer = self._pending()
        offer.with_user(self.reviewer).action_approve_with_product()
        self.assertEqual(offer.state, "active")
        self.assertEqual(template.marketplace_state, "published")
        self.assertTrue(template.is_published)

    def test_it_leaves_an_already_published_product_alone(self):
        template, offer = self._pending()
        template.action_marketplace_approve()
        published_on = template.marketplace_publish_date
        offer.with_user(self.reviewer).action_approve_with_product()
        self.assertEqual(offer.state, "active")
        self.assertEqual(template.marketplace_publish_date, published_on)

    def test_approving_the_vendors_product_approves_its_offer_too(self):
        """BUG-00871: the product was published but its offer stayed pending, so it never reached the website."""
        template, offer = self._pending()
        other_vendor = self.vendor.copy({"name": "Other Seller Co"})
        other = self.Offer.create({
            "product_tmpl_id": template.id, "product_id": template.product_variant_id.id, "vendor_id": other_vendor.id,
            "seller_sku": "REV-OTHER", "price": 60.0, "fulfillment_type": "fbm", "state": "pending",
            "stock_location_id": self.vendor._marketplace_stock_location("fbm").id})
        template.with_user(self.reviewer).action_marketplace_approve()
        self.assertEqual((template.marketplace_state, offer.state), ("published", "active"))
        self.assertEqual(other.state, "pending")  # another seller's offer keeps its own review

    def test_it_is_back_office_only(self):
        template, offer = self._pending()
        with self.assertRaises(AccessError):
            offer.with_user(self.owner_user).action_approve_with_product()

    def test_the_offer_carries_its_products_state(self):
        template, offer = self._pending()
        self.assertEqual(offer.product_marketplace_state, "pending")
        template.action_marketplace_approve()
        self.assertEqual(offer.product_marketplace_state, "published")


@tagged("post_install", "-at_install")
class TestReviewScreens(ProductReviewCommon):

    def test_the_review_action_and_the_section_menus_are_installed(self):
        action = self.env.ref("leaders_marketplace.action_product_marketplace_review")
        self.assertEqual(action.res_model, "product.template")
        self.assertIn("search_default_mp_pending", action.context)
        # review is a filter of each section of Marketplace > Products, which open the marketplace product screen
        base = self.env.ref("leaders_marketplace.view_product_template_marketplace_base")
        for key in ("power", "equipment", "parts", "services"):
            section = self.env.ref("leaders_marketplace.action_marketplace_products_%s" % key)
            self.assertEqual(section.view_ids.filtered(lambda v: v.view_mode == "form").view_id, base)
            self.assertEqual(self.env.ref("leaders_marketplace.menu_marketplace_products_%s" % key).action, section)
        self.assertFalse(self.env.ref("leaders_marketplace.menu_products_to_review", raise_if_not_found=False))

    def test_the_pending_filter_finds_a_submitted_product(self):
        template, offer = self._pending()
        pending = self.env["product.template"].search([
            ("marketplace_state", "=", "pending")])
        self.assertIn(template, pending)

    def test_the_review_list_columns_resolve(self):
        template, offer = self._pending()
        self.assertEqual(template.marketplace_vendor_id, self.vendor)
        self.assertEqual(template.marketplace_offer_count, 1)

    def test_the_vendor_column_falls_back_to_the_first_offer(self):
        """A catalogue product has no portal owner."""
        product = self.env["product.product"].create({
            "name": "Catalogue", "type": "consu", "is_storable": True,
            "marketplace_state": "pending",
        })
        self.Offer.create({
            "product_tmpl_id": product.product_tmpl_id.id, "product_id": product.id,
            "vendor_id": self.vendor.id, "seller_sku": "CAT-REV", "price": 10.0,
            "fulfillment_type": "fbm",
            "stock_location_id": self.vendor._marketplace_stock_location("fbm").id,
        })
        self.assertFalse(product.product_tmpl_id.marketplace_owner_vendor_id)
        self.assertEqual(product.product_tmpl_id.marketplace_vendor_id, self.vendor)

    def test_the_out_of_stock_offer_filter_works(self):
        template, offer = self._pending()
        offer.action_approve()
        empty = self.Offer.search([("available_qty_cache", "<=", 0)])
        self.assertIn(offer, empty, "an offer with no stock was not found")
        self.env["stock.quant"]._leaders_adjust_vendor_stock(
            offer.product_id, self.vendor.vendor_location_id, self.vendor, 5)
        self.assertNotIn(offer, self.Offer.search([("available_qty_cache", "<=", 0)]))

    def test_the_offer_search_view_is_wired_to_the_action(self):
        action = self.env.ref("leaders_marketplace.action_marketplace_offer")
        self.assertEqual(
            action.search_view_id,
            self.env.ref("leaders_marketplace.view_marketplace_offer_search"))


@tagged("post_install", "-at_install")
class TestPortalShowsRejections(HttpCase, ProductReviewCommon):

    def test_the_vendor_sees_both_rejection_reasons(self):
        template, offer = self._pending()
        offer.reject_reason = "Your price is above the cap"
        offer.action_reject()
        template.marketplace_reject_reason = "The photo shows another product"
        template.action_marketplace_reject()

        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/products")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Your price is above the cap", page.text)
        self.assertIn("The photo shows another product", page.text)

    def test_the_vendor_is_told_the_product_is_not_live_yet(self):
        template, offer = self._pending()
        offer.action_approve()          # offer live, product still pending
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/products")
        self.assertIn("is not on the website yet", page.text)

    def test_nothing_is_said_once_both_are_live(self):
        template, offer = self._pending()
        offer.action_approve_with_product()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/products")
        # Scoped to this product's row: the shared fixture has other offers on
        # products that really are still unpublished, and they must keep saying so.
        row = re.search(
            r"<td[^>]*>\s*<span>Submitted Product</span>(.*?)</td>", page.text, re.S)
        self.assertTrue(row, "the product row was not rendered")
        self.assertNotIn("is not on the website yet", row.group(1))



@tagged("post_install", "-at_install")
class TestMarketplaceProductScreen(ProductReviewCommon):
    """The marketplace has its own product screen; Odoo's standard product form is left as Odoo ships it."""

    def test_odoo_form_is_clean_and_the_marketplace_form_has_the_catalog(self):
        standard = self.env["product.template"].get_view(self.env.ref("product.product_template_only_form_view").id, "form")["arch"]
        for name in ("leaders_brand_id", "leaders_part_number", "marketplace_state", "leaders_service_summary"):
            self.assertNotIn('name="%s"' % name, standard)
        from lxml import etree
        ours = etree.tostring(  # the whole screen, whatever the groups of the user reading it
            self.env.ref("leaders_marketplace.view_product_template_marketplace_base")._get_combined_arch(), encoding="unicode")
        for name in ("leaders_brand_id", "leaders_part_number", "action_marketplace_approve", "leaders_service_summary",
                     "leaders_asset_code", "leaders_inspection_result", "product_template_image_ids"):
            self.assertIn('name="%s"' % name, ours)
        self.assertEqual(ours.count('name="marketplace_state"'), 1)  # only the status bar, not again in a tab
        self.assertEqual(ours.count('name="leaders_rent_price_day"'), 1)  # no longer repeated on the Rental tab

    def test_a_product_made_in_a_section_lands_in_it_and_can_be_approved_from_draft(self):
        from odoo.tests import Form
        action = self.env.ref("leaders_marketplace.action_marketplace_products_parts")
        ctx = dict(eval(action.context) if isinstance(action.context, str) else action.context)
        with Form(self.env["product.template"].with_context(**ctx),
                  view="leaders_marketplace.view_product_template_marketplace_base") as form:
            form.name = "Section made filter"
        product = form.record
        self.assertIn(self.env.ref("leaders_marketplace.leaders_categ_parts"), product.public_categ_ids)
        self.assertIn(product, self.env["product.template"].search(eval(action.domain) if isinstance(action.domain, str) else action.domain))


    def test_the_subcategory_field_offers_only_the_sections_own_categories(self):
        from lxml import etree
        arch = etree.fromstring(self.env["product.template"].with_context(
            lm_section_root=self.env.ref("leaders_marketplace.leaders_categ_power").id).get_view(
            self.env.ref("leaders_marketplace.view_product_template_marketplace_base").id, "form")["arch"])
        field = arch.xpath("//field[@name='public_categ_ids'][@string='Subcategory']")[0]
        self.assertIn("lm_section_root", field.get("domain"))
        Category = self.env["product.public.category"]
        power = self.env.ref("leaders_marketplace.leaders_categ_power")
        offered = Category.search([("id", "child_of", power.id), ("id", "!=", power.id)])
        self.assertIn(self.env.ref("leaders_marketplace.leaders_categ_generators"), offered)
        self.assertNotIn(self.env.ref("leaders_marketplace.leaders_categ_loaders"), offered)
        self.assertNotIn(power, offered)  # the section itself is not a subcategory
        for key, ref in (("power", "leaders_categ_power"), ("parts", "leaders_categ_parts")):
            action = self.env.ref("leaders_marketplace.action_marketplace_products_%s" % key)
            self.assertIn("lm_section_root", action.context)


@tagged("post_install", "-at_install")
class TestMarketplaceCategoryScreen(ProductReviewCommon):
    """Marketplace > Configuration > Categories: one menu per section, a screen with the guide's and the API's fields."""

    def test_the_four_section_menus_open_their_own_categories(self):
        parent = self.env.ref("leaders_marketplace.menu_leaders_category")
        self.assertFalse(parent.action)
        sections = {"power": "leaders_categ_power", "equipment": "leaders_categ_equipment",
                    "parts": "leaders_categ_parts", "services": "leaders_categ_services"}
        for key, root_xmlid in sections.items():
            menu = self.env.ref("leaders_marketplace.menu_marketplace_categories_%s" % key)
            self.assertEqual(menu.parent_id, parent)
            action = self.env.ref("leaders_marketplace.action_marketplace_categories_%s" % key)
            root = self.env.ref("leaders_marketplace.%s" % root_xmlid)
            found = self.env["product.public.category"].search(safe_eval(action.domain))
            self.assertIn(root, found)
            self.assertEqual(found, self.env["product.public.category"].search([("id", "child_of", root.id)]))

    def test_the_screen_has_the_guide_and_api_fields_only(self):
        from lxml import etree
        view = self.env.ref("leaders_marketplace.view_public_category_marketplace_form")
        arch = etree.tostring(view._get_combined_arch(), encoding="unicode")
        for name in ("name", "parent_id", "image_1920", "leaders_description", "leaders_field_set", "leaders_can_rent",
                     "leaders_accepts_used", "leaders_inspection_required", "leaders_icon", "leaders_delivery_surcharge",
                     "leaders_group", "leaders_hidden", "leaders_slug", "sequence"):
            self.assertIn('name="%s"' % name, arch, name)
        for name in ("website_id", "website_description"):
            self.assertNotIn('name="%s"' % name, arch, name)

    def test_a_subcategory_is_made_under_its_section(self):
        from odoo.tests import Form
        root = self.env.ref("leaders_marketplace.leaders_categ_parts")
        view = self.env.ref("leaders_marketplace.view_public_category_marketplace_form")
        Category = self.env["product.public.category"].with_context(lm_cat_root=root.id, lm_cat_section="parts",
                                                                   default_parent_id=root.id)
        with Form(Category, view=view) as form:
            form.name = "Belts"
            form.leaders_accepts_used = True
            form.leaders_inspection_required = True
        categ = form.record
        self.assertEqual((categ.parent_id, categ.leaders_inspection_required), (root, True))
        self.assertTrue(categ.leaders_slug)


@tagged("post_install", "-at_install")
class TestAutomaticProductLink(ProductReviewCommon):
    """The product link (slug) is made by the platform, like the categories': never typed on the screen."""

    def test_the_link_is_made_from_the_name_and_cannot_be_typed(self):
        admin = self.env.ref("base.user_admin")
        Template = self.env["product.template"].with_user(admin)
        gen = self.env.ref("leaders_marketplace.leaders_categ_generators")
        tmpl = Template.create({"name": "مولد كهربائي صامت", "leaders_slug": "typed-by-hand",
                                "public_categ_ids": [Command.set(gen.ids)]})
        self.assertNotEqual(tmpl.leaders_slug, "typed-by-hand")
        self.assertRegex(tmpl.leaders_slug, r"^[a-z0-9-]+$")  # Arabic written in Latin letters
        tmpl.write({"leaders_slug": "typed-again"})
        self.assertNotEqual(tmpl.leaders_slug, "typed-again")
        # it follows the name until the product is published, then it stays
        tmpl.with_context(lang="en_US").name = "Silent Generator"
        self.assertEqual(tmpl.leaders_slug, "silent-generator")
        tmpl.sudo().write({"marketplace_state": "published", "is_published": True})
        tmpl.with_context(lang="en_US").name = "Quiet Generator"
        self.assertEqual(tmpl.leaders_slug, "silent-generator")

    def test_a_new_service_takes_its_link_by_itself(self):
        admin = self.env.ref("base.user_admin")
        services = self.env.ref("leaders_marketplace.leaders_categ_services")
        service = self.env["product.template"].with_user(admin).create({
            "name": "Crane Hire", "type": "service", "public_categ_ids": [Command.set(services.ids)]})
        self.assertEqual(service.leaders_slug, "crane-hire")
