"""Clean portal menus: customer home cards, vendor layout, breadcrumbs (TASK 29 / C1)."""
from lxml import html as lxml_html

from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import PORTAL_PASSWORD, MarketplacePortalUserCommon

OWNER_ONLY = ("Finance", "Delivery Zones", "Staff", "API", "Profile")
CUSTOMER_CARDS = {"My orders", "Returns & disputes", "Account details", "Connection & Security",
                  "Notification preferences"}


@tagged("post_install", "-at_install")
class TestPortalMenus(HttpCase, MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "LM Buyer", "login": "lm.buyer@example.com", "email": "lm.buyer@example.com",
            "password": PORTAL_PASSWORD, "partner_id": cls.customer.id,
            "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
        })
        cls.wishlist = bool(cls.env["ir.module.module"].sudo().search_count([
            ("name", "=", "website_sale_wishlist"), ("state", "=", "installed")]))
        cls.env.cr.flush()

    # -- helpers --------------------------------------------------------
    def _set(self, name, value):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace." + name, value)
        self.env.cr.flush()

    def _dom(self, path):
        response = self.url_open(path)
        self.assertEqual(response.status_code, 200, path)
        return lxml_html.fromstring(response.content)

    def _cards(self, dom):
        """{title: visible} of every home card."""
        cards = {}
        for card in dom.xpath("//div[contains(@class, 'o_portal_index_card')]"):
            title = card.xpath("string(.//a/@title)")
            cards[title] = "d-none" not in card.get("class").split()
        return cards

    def _visible(self, dom):
        return {t for t, shown in self._cards(dom).items() if shown}

    def _nav(self, dom):
        return dom.xpath("//nav[contains(@class, 'lm-vendor-nav')]")[0]

    def _nav_labels(self, dom):
        return [" ".join(a.text_content().split()) for a in self._nav(dom).xpath(".//a")]

    def _nav_hrefs(self, dom):
        return [a.get("href") for a in self._nav(dom).xpath(".//a")]

    def _quotation(self):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": self.fbm_product.id, "product_uom_qty": 1})],
        })
        order.message_subscribe(partner_ids=self.customer.ids)
        order.state = "sent"
        self.env.cr.flush()
        return order

    # -- customer home ---------------------------------------------------
    def test_customer_home_shows_only_the_allowed_cards(self):
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/home")
        expected = set(CUSTOMER_CARDS)
        if self.wishlist:
            expected.add("Wishlist")
        self.assertEqual(self._visible(dom), expected)
        self.assertNotIn("Vendor Dashboard", dom.text_content())

    def test_document_entries_appear_once_the_customer_has_documents(self):
        order = self._quotation()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        counters = self.make_jsonrpc_request("/my/counters", {"counters": ["quotation_count", "order_count"]})
        self.assertEqual(counters, {"quotation_count": 1, "order_count": 0})
        dom = self._dom("/my/home")
        cards = self._cards(dom)
        # The alert card is revealed by the counters script (its count is > 0); it is in the page.
        self.assertIn("Quotations to review", cards)
        self.assertTrue(dom.xpath("//a[@href='/my/quotes']"))
        self.assertNotIn("Your Orders", self._visible(dom))      # no confirmed order: hidden
        order.state = "sale"
        self.env.cr.flush()
        self.make_jsonrpc_request("/my/counters", {"counters": ["order_count"]})
        visible = self._visible(self._dom("/my/home"))
        self.assertIn("Your Orders", visible)
        self.assertTrue(CUSTOMER_CARDS <= visible)

    def test_always_shows_standard_entries_and_never_removes_them(self):
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        self._set("portal_show_standard_entries", "always")
        visible = self._visible(self._dom("/my/home"))
        self.assertIn("Your Orders", visible)
        self.assertTrue(CUSTOMER_CARDS <= visible)
        self._set("portal_show_standard_entries", "never")
        cards = self._cards(self._dom("/my/home"))
        self.assertNotIn("Your Orders", cards)
        self.assertTrue(CUSTOMER_CARDS <= {t for t, shown in cards.items() if shown})
        # Only the cards are hidden: the pages still work.
        self.assertEqual(self.url_open("/my/orders").status_code, 200)

    def test_unknown_setting_value_falls_back_to_default(self):
        self._set("portal_show_standard_entries", "bogus")
        self.assertEqual(self.env["leaders.portal.nav"]._standard_entries_mode(), "only_if_documents")

    def test_vendor_home_is_vendor_cards_plus_my_purchases(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/home")
        self.assertTrue(dom.xpath("//div[contains(@class, 'lm-vendor-home')]"))
        self.assertTrue(dom.xpath("//a[@href='/my/marketplace/orders']//*[contains(text(), 'My Purchases')]"))

    # -- vendor layout ----------------------------------------------------
    def test_owner_nav_has_every_entry_and_a_customer_area_link(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        labels = self._nav_labels(self._dom("/my/vendor"))
        for label in ("Dashboard", "Products", "Orders", "Returns", "Disputes", "Performance",
                      "Finance", "Delivery Zones", "Staff", "Profile", "My purchases"):
            self.assertIn(label, labels)
        self.assertNotIn("Questions", labels)         # route not built yet: no dead link
        self.assertNotIn("API", labels)               # write API off by default
        self.assertNotIn("Import", labels)            # bulk import off by default

    def test_staff_never_see_owner_only_entries(self):
        self.authenticate(self.staff_user.login, PORTAL_PASSWORD)
        for path in ("/my/vendor", "/my/vendor/products", "/my/vendor/orders"):
            dom = self._dom(path)
            labels = self._nav_labels(dom)
            self.assertIn("Orders", labels)
            for label in OWNER_ONLY:
                self.assertNotIn(label, labels)
            for href in ("/my/vendor/finance", "/my/vendor/staff", "/my/vendor/profile",
                         "/my/vendor/delivery-zones", "/my/vendor/api"):
                self.assertNotIn(href, self._nav_hrefs(dom))

    def test_feature_switches_drive_their_entries(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self._set("vendor_bulk_import_enabled", "True")
        labels = self._nav_labels(self._dom("/my/vendor"))
        self.assertIn("Import", labels)
        self.assertNotIn("API", labels)
        self._set("vendor_api_write_enabled", "True")
        labels = self._nav_labels(self._dom("/my/vendor"))
        self.assertIn("API", labels)
        self.authenticate(self.staff_user.login, PORTAL_PASSWORD)
        labels = self._nav_labels(self._dom("/my/vendor"))
        self.assertIn("Import", labels)               # same rule as product creation
        self.assertNotIn("API", labels)

    def test_no_dead_links_in_any_menu_with_every_feature_on(self):
        for name in ("vendor_bulk_import_enabled", "vendor_api_write_enabled",
                     "customer_can_cancel_orders", "vendor_variants_enabled",
                     "vendor_discounts_enabled"):
            self._set(name, "True")
        seen = 0
        for user in (self.owner_user, self.staff_user):
            self.authenticate(user.login, PORTAL_PASSWORD)
            for href in self._nav_hrefs(self._dom("/my/vendor")):
                response = self.url_open(href)
                self.assertEqual(response.status_code, 200, "%s (%s)" % (href, user.login))
                seen += 1
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/home")
        for href in dom.xpath("//div[contains(@class, 'o_portal_index_card')]//a/@href"):
            response = self.url_open(href)
            self.assertEqual(response.status_code, 200, href)
            seen += 1
        self.assertGreater(seen, 15)

    def test_pending_terms_disable_the_sidebar_for_the_owner(self):
        self.env["leaders.vendor.terms"].with_context(active_test=False).search([]).write({"active": False})
        self.env["leaders.vendor.terms"].create({"name": "Seller Agreement", "version": 1, "body": "<p>x</p>"})
        self.env.cr.flush()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/vendor/terms")
        self.assertTrue(self._nav(dom).xpath(".//span[contains(@class, 'disabled')]"))
        self.assertNotIn("/my/vendor/finance", self._nav_hrefs(dom))

    # -- breadcrumbs ------------------------------------------------------
    def _crumbs(self, dom):
        items = dom.xpath("//ol[@id='lm_vendor_breadcrumbs']/li")
        return [(" ".join(li.text_content().split()), li.xpath("string(a/@href)")) for li in items]

    def test_vendor_breadcrumbs(self):
        order = self._confirmed_sale_order(self.fbm_product, 1).vendor_order_ids
        self.env.cr.flush()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        crumbs = self._crumbs(self._dom("/my/vendor"))
        self.assertEqual(crumbs[0][1], "/my/home")
        self.assertEqual(crumbs[-1], ("Vendor", ""))
        crumbs = self._crumbs(self._dom("/my/vendor/orders"))
        self.assertEqual([c[0] for c in crumbs][1:], ["Vendor", "Orders"])
        self.assertEqual(crumbs[1][1], "/my/vendor")
        crumbs = self._crumbs(self._dom("/my/vendor/orders/%s" % order.id))
        self.assertEqual(crumbs[1:], [("Vendor", "/my/vendor"), ("Orders", "/my/vendor/orders"),
                                      ("Order details", "")])
        crumbs = self._crumbs(self._dom("/my/vendor/finance/statement"))
        self.assertEqual(crumbs[-2:], [("Finance", "/my/vendor/finance"), ("Statement of account", "")])

    def test_customer_pages_have_breadcrumbs(self):
        order = self._confirmed_sale_order(self.fbm_product, 1).vendor_order_ids
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/marketplace/orders")
        self.assertIn("My orders", dom.xpath("string(//ol[contains(@class, 'o_portal_submenu')])"))
        dom = self._dom("/my/marketplace/orders/%s" % order.id)
        self.assertTrue(dom.xpath("//ol[contains(@class, 'o_portal_submenu')]//a[@href='/my/marketplace/orders']"))
        self.assertIn(order.name, dom.xpath("string(//ol[contains(@class, 'o_portal_submenu')])"))
        dom = self._dom("/my/marketplace/returns")
        self.assertIn("Returns & disputes", dom.xpath("string(//ol[contains(@class, 'o_portal_submenu')])"))

    def test_returns_page_lists_the_customers_returns_only(self):
        vorder = self._confirmed_sale_order(self.fbm_product, 2).vendor_order_ids
        group = vorder.fulfillment_group_ids[:1]
        group.tracking_ref = "TRK-C1"
        group.action_ship()
        ret = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vorder.id, "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": vorder.line_ids[:1].id, "quantity": 1})],
        })
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/marketplace/returns")
        self.assertIn(ret.name, page.text)
        self.assertIn("/my/marketplace/orders/%s" % vorder.id, page.text)
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self.assertNotIn(ret.name, self.url_open("/my/marketplace/returns").text)
