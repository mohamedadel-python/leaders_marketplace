"""Catalog offers, seller display, storefronts and ratings."""
import re

from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import PORTAL_PASSWORD
from .test_vendor_split import MarketplaceVendorCommon


@tagged("post_install", "-at_install")
class TestCatalogOffers(HttpCase, MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.vendor.vendor_store_name = "Alpha Store"
        cls.vendor.vendor_store_slug = False
        cls.vendor._marketplace_ensure_store_slug()
        tmpl = cls.fbm_product.product_tmpl_id
        tmpl.write({"marketplace_state": "published", "is_published": True, "sale_ok": True})
        cls.tmpl = tmpl
        cls.vendor_b = env["res.partner"].create({
            "name": "Beta Vendor", "vendor_store_name": "Beta Store",
            "is_marketplace_vendor": True, "vendor_state": "approved",
        })
        cls.vendor_b._marketplace_ensure_vendor_stock()
        env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.fbm_product, cls.vendor_b.vendor_location_id, cls.vendor_b, 5)
        contact = env["res.partner"].create({
            "name": "Beta Owner", "email": "beta.owner@example.com",
            "parent_id": cls.vendor_b.id, "type": "contact"})
        cls.user_b = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Beta Owner", "login": "beta.owner@example.com", "email": "beta.owner@example.com",
            "password": PORTAL_PASSWORD, "partner_id": contact.id,
            "groups_id": [Command.set([env.ref("leaders_marketplace.group_vendor_owner").id])],
        })
        cls.customer.email = "cat.customer@example.com"
        cls.customer_user = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Cat Customer", "login": "cat.customer@example.com",
            "email": "cat.customer@example.com", "password": PORTAL_PASSWORD,
            "partner_id": cls.customer.id,
            "groups_id": [Command.set([env.ref("base.group_portal").id])],
        })

    def _enable(self, value="True"):
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.catalog_offers_enabled", value)

    def _csrf(self, url):
        page = self.url_open(url)
        self.assertEqual(page.status_code, 200, url)
        return re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text).group(1)

    def _post(self, url, form_url, **data):
        data["csrf_token"] = self._csrf(form_url)
        return self.url_open(url, data=data)

    def _b_sells(self, sku="BETA-1", price="80"):
        self.authenticate(self.user_b.login, PORTAL_PASSWORD)
        return self._post(
            "/my/vendor/products/sell-existing", "/my/vendor/products/new",
            product_id=self.fbm_product.id, seller_sku=sku, price=price, fulfillment_type="fbm")

    def _b_offer(self):
        return self.env["leaders.marketplace.offer"].sudo().search([("vendor_id", "=", self.vendor_b.id)])

    def _live_b_offer(self):
        self._b_sells()
        offer = self._b_offer()
        offer.action_submit()
        offer.action_approve()
        return offer

    # ------------------------------------------------------------ slug
    def test_slug_is_generated_and_unique(self):
        self.assertEqual(self.vendor.vendor_store_slug, "alpha-store")
        twin = self.env["res.partner"].create({
            "name": "Twin", "vendor_store_name": "Alpha Store",
            "is_marketplace_vendor": True, "vendor_state": "approved"})
        self.assertEqual(twin.vendor_store_slug, "alpha-store-2")

    # ------------------------------------------------- sell an existing product
    def test_off_by_default(self):
        self.authenticate(self.user_b.login, PORTAL_PASSWORD)
        response = self._post(
            "/my/vendor/products/sell-existing", "/my/vendor/products/new",
            product_id=self.fbm_product.id, seller_sku="X", price="1")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self._b_offer())
        self.assertNotIn("Other sellers", self.url_open(self.tmpl.website_url).text)

    def test_vendor_b_sells_the_catalog_product_without_a_new_template(self):
        self._enable()
        templates = self.env["product.template"].search_count([])
        offer = self._live_b_offer()
        self.assertEqual(offer.product_id, self.fbm_product)
        self.assertEqual(self.env["product.template"].search_count([]), templates)
        self.assertEqual(self.tmpl.marketplace_offer_ids.filtered("active").mapped("vendor_id"),
                         self.vendor | self.vendor_b)
        winners = (self.fbm_offer | offer).filtered("is_buybox_winner")
        self.assertEqual(len(winners), 1)

    def test_duplicate_offer_is_blocked(self):
        self._enable()
        self._b_sells()
        response = self._b_sells(sku="BETA-2")
        self.assertIn("already have an offer", response.text)
        self.assertEqual(len(self._b_offer()), 1)

    def test_catalog_search_lists_published_products(self):
        self._enable()
        self.authenticate(self.user_b.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/products/new?q=Vendor FBM")
        self.assertIn("Vendor FBM Product", page.text)
        self.assertIn("sell-existing", page.text)

    # ------------------------------------------------------------ product page
    def test_product_page_lists_the_other_seller_and_the_cart_honours_it(self):
        self._enable()
        offer_b = self._live_b_offer()
        self.authenticate(None, None)  # back to the public user
        page = self.url_open(self.tmpl.website_url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Sold by", page.text)
        self.assertIn("Other sellers", page.text)
        winner = (self.fbm_offer | offer_b).filtered("is_buybox_winner")
        other = (self.fbm_offer | offer_b) - winner
        self.assertIn(other.vendor_id.vendor_store_name, page.text)

        token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text).group(1)
        self.url_open("/shop/cart/update", data={
            "csrf_token": token, "product_id": self.fbm_product.id, "add_qty": 1, "offer_id": other.id})
        order = self.env["sale.order"].sudo().search([("website_id", "!=", False)], order="id desc", limit=1)
        line = order.order_line.filtered(lambda l: l.product_id == self.fbm_product)
        self.assertEqual(line.marketplace_offer_id, other)
        self.assertAlmostEqual(line.price_unit, other.price)

    # --------------------------------------------------------------- storefront
    def test_store_page_is_200_for_approved_and_404_for_unapproved(self):
        self._enable()
        self.assertEqual(self.url_open("/seller/%s" % self.vendor.vendor_store_slug).status_code, 200)
        pending = self.env["res.partner"].create({
            "name": "Pending Vendor", "is_marketplace_vendor": True,
            "vendor_state": "submitted", "vendor_store_slug": "pending-vendor"})
        self.assertEqual(self.url_open("/seller/%s" % pending.vendor_store_slug).status_code, 404)
        self.assertEqual(self.url_open("/seller/no-such-store").status_code, 404)

    # ------------------------------------------------------------------ ratings
    def _completed_order(self):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": self.fbm_product.id, "product_uom_qty": 1})],
        })
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "RATE-TRK"
        group.action_ship()
        group.action_delivered()
        self.assertEqual(vendor_order.state, "completed")
        return vendor_order

    def test_rating_updates_the_average_after_the_cron(self):
        self._enable()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.vendor_rating_min_count", "1")
        vendor_order = self._completed_order()
        self.fbm_offer.rating = 2.0
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        url = "/my/marketplace/orders/%s" % vendor_order.id
        self.assertIn("Rate this order", self.url_open(url).text)
        self._post(url + "/rate", url, rating="4", feedback="Good")
        self.assertEqual(self.vendor.vendor_rating_count, 1)
        self.assertEqual(self.vendor.vendor_rating_avg, 4.0)
        self.assertEqual(self.fbm_offer.rating, 2.0)   # not until the cron runs

        self.env["res.partner"].cron_sync_vendor_ratings()
        self.assertEqual(self.fbm_offer.rating, 4.0)
        self.assertNotIn("Rate this order", self.url_open(url).text)

    def test_manual_rating_is_kept_below_the_minimum(self):
        self._enable()
        vendor_order = self._completed_order()   # min count stays at the default 5
        self.fbm_offer.rating = 2.0
        vendor_order._marketplace_submit_rating(5)
        self.env["res.partner"].cron_sync_vendor_ratings()
        self.assertEqual(self.vendor.vendor_rating_count, 1)
        self.assertEqual(self.fbm_offer.rating, 2.0)
