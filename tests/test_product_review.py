"""The product review workflow, reachable from the back office.

The product state and the offer state are two separate workflows: approving an
offer makes that seller's listing live, approving the product puts it on the
website. Neither implies the other, which is why approving only the offer used
to leave the product invisible.
"""
import re

from odoo import Command
from odoo.exceptions import AccessError, UserError
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

    def test_the_review_action_and_menu_are_installed(self):
        action = self.env.ref("leaders_marketplace.action_product_marketplace_review")
        self.assertEqual(action.res_model, "product.template")
        self.assertIn("search_default_mp_pending", action.context)
        menu = self.env.ref("leaders_marketplace.menu_products_to_review")
        self.assertEqual(menu.action, action)

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
