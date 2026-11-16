"""Wishlist link and customer notification preferences (TASK 29 / D5)."""
from unittest.mock import patch

from lxml import html as lxml_html

from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.leaders_marketplace.models.notification import (
    CUSTOMER_EVENT_CATEGORY, CUSTOMER_MANDATORY_EVENTS)
from odoo.addons.leaders_marketplace.models.res_config_settings import NOTIFICATION_EVENTS
from .test_customer_cancel import CustomerCancelCommon
from .test_security_rpc import PORTAL_PASSWORD

NAV = "odoo.addons.leaders_marketplace.models.portal_nav.LeadersPortalNav._module_installed"
FIELDS = {"shipping": "leaders_notify_shipping", "orders": "leaders_notify_orders",
          "returns": "leaders_notify_returns", "questions": "leaders_notify_questions",
          "promotions": "leaders_notify_promotions"}


class PrefCommon(CustomerCancelCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.fbm_product, cls.vendor.vendor_location_id, cls.vendor, 50)
        set_param = cls.env["ir.config_parameter"].sudo().set_param
        for key, _label in NOTIFICATION_EVENTS:
            if key.startswith("customer_"):
                set_param("leaders_marketplace.notify_%s" % key, "True")

    def _vendor_order(self):
        order = self._sale([(self.fbm_product, 1)])
        order.action_confirm()
        return order.vendor_order_ids[:1]

    def _return(self):
        vendor_order = self._vendor_order()
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "PREF-TRK"
        group.action_ship()
        return self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": vendor_order.line_ids[:1].id, "quantity": 1})],
        })

    def _queued(self, key, record, force=False):
        Mail = self.env["mail.mail"].sudo()
        last = Mail.search([], order="id desc", limit=1).id or 0
        self.env["leaders.notification"].notify(
            key, record, partners=self.customer, force=force)
        return Mail.search([("id", ">", last)])


@tagged("post_install", "-at_install")
class TestNotificationPreferences(PrefCommon):

    def test_fields_and_defaults(self):
        partner = self.env["res.partner"].create({"name": "Pref Fresh"})
        for category, field in FIELDS.items():
            self.assertEqual(partner[field], category != "promotions", field)
        self.assertTrue(partner._leaders_customer_accepts("shipping"))
        self.assertFalse(partner._leaders_customer_accepts("promotions"))

    def test_helper_uses_commercial_partner_and_rejects_unknown(self):
        company = self.env["res.partner"].create({"name": "Pref Co", "is_company": True})
        contact = self.env["res.partner"].create({"name": "Pref Contact", "parent_id": company.id})
        company.leaders_notify_shipping = False
        self.assertFalse(contact._leaders_customer_accepts("shipping"))
        self.assertTrue(contact._leaders_customer_accepts("returns"))
        self.assertFalse(contact._leaders_customer_accepts("nonsense"))
        company.leaders_notify_orders = False
        self.assertTrue(contact._leaders_customer_accepts("orders"), "orders is mandatory")

    def test_every_customer_event_has_a_category(self):
        customer_keys = {k for k, _l in NOTIFICATION_EVENTS if k.startswith("customer_")}
        self.assertTrue(customer_keys)
        self.assertEqual(customer_keys - set(CUSTOMER_EVENT_CATEGORY), set(),
                         "new customer event without a preference category")
        self.assertFalse(set(CUSTOMER_EVENT_CATEGORY) - customer_keys)
        self.assertLessEqual(set(CUSTOMER_EVENT_CATEGORY.values()), set(FIELDS))
        self.assertLessEqual(CUSTOMER_MANDATORY_EVENTS, customer_keys)

    def test_opt_out_skips_the_mail_per_category(self):
        vendor_order = self._vendor_order()
        group = vendor_order.fulfillment_group_ids
        ret = self._return()
        self.assertEqual(len(self._queued("customer_order_shipped", group)), 1)
        self.customer.leaders_notify_shipping = False
        self.assertFalse(self._queued("customer_order_shipped", group))
        self.assertTrue(self._queued("customer_return_approved", ret), "other categories unaffected")
        self.customer.leaders_notify_returns = False
        self.assertFalse(self._queued("customer_return_approved", ret))
        self.assertFalse(self._queued("customer_return_rejected", ret))

    def test_mandatory_events_and_force_ignore_the_opt_out(self):
        vendor_order = self._vendor_order()
        ret = self._return()
        group = vendor_order.fulfillment_group_ids
        self.customer.write({f: False for f in FIELDS.values()})
        self.assertEqual(len(self._queued("customer_order_cancelled", vendor_order)), 1)
        self.assertEqual(len(self._queued("customer_return_refunded", ret)), 1)
        self.assertEqual(len(self._queued("customer_order_shipped", group, force=True)), 1)

    def test_opt_out_of_a_contact_follows_the_commercial_partner(self):
        group = self._vendor_order().fulfillment_group_ids
        contact = self.env["res.partner"].create({
            "name": "Pref Contact 2", "parent_id": self.customer.id, "email": "pref.contact@example.com"})
        self.customer.leaders_notify_shipping = False
        Mail = self.env["mail.mail"].sudo()
        last = Mail.search([], order="id desc", limit=1).id or 0
        self.env["leaders.notification"].notify_customer("customer_order_shipped", group, contact)
        self.assertFalse(Mail.search([("id", ">", last)]))

    def test_vendor_events_are_not_affected(self):
        self.vendor.leaders_notify_shipping = False
        self.assertNotIn("vendor_order_confirmed", CUSTOMER_EVENT_CATEGORY)


@tagged("post_install", "-at_install")
class TestNotificationPreferencesPortal(HttpCase, PrefCommon):

    def _dom(self, path):
        response = self.url_open(path)
        self.assertEqual(response.status_code, 200, path)
        return lxml_html.fromstring(response.content)

    def _save(self, **data):
        data["csrf_token"] = self._csrf_token()
        return self.url_open("/my/marketplace/notifications", data=data)

    def _csrf_token(self):
        dom = self._dom("/my/marketplace/notifications")
        return dom.xpath("//form[@id='lm_notifications_form']//input[@name='csrf_token']/@value")[0]

    def test_login_is_required(self):
        self.authenticate(None, None)
        response = self.url_open("/my/marketplace/notifications", allow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        self.assertIn("/web/login", response.headers["Location"])

    def test_get_shows_defaults_and_required_note(self):
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/marketplace/notifications")
        checked = {n: bool(dom.xpath("//input[@name='%s']/@checked" % n))
                   for n in ("shipping", "returns", "questions", "promotions")}
        self.assertEqual(checked, {"shipping": True, "returns": True, "questions": True, "promotions": False})
        self.assertFalse(dom.xpath("//input[@name='orders']"))
        text = dom.text_content()
        self.assertIn("Promotions (marketing emails)", text)
        self.assertIn("Vendor and account security emails are not affected", text)
        self.assertIn("Required emails", text)

    def test_post_saves_on_the_own_commercial_partner_and_flashes(self):
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        response = self._save(shipping="1", promotions="1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("saved=1", response.url)
        self.assertIn("Your preferences were saved.", response.text)
        self.customer.invalidate_recordset()
        self.assertEqual(
            {c: self.customer[f] for c, f in FIELDS.items()},
            {"shipping": True, "orders": True, "returns": False, "questions": False, "promotions": True})
        dom = self._dom("/my/marketplace/notifications")
        self.assertTrue(dom.xpath("//input[@name='promotions']/@checked"))
        self.assertFalse(dom.xpath("//input[@name='returns']/@checked"))

    def test_a_user_only_changes_their_own_partner(self):
        other = self.owner_user.partner_id.commercial_partner_id
        before = other.leaders_notify_shipping
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        self._save(partner_id=other.id, id=other.id)   # injected ids are ignored
        other.invalidate_recordset()
        self.assertEqual(other.leaders_notify_shipping, before)
        self.customer.invalidate_recordset()
        self.assertFalse(self.customer.leaders_notify_shipping)

    def test_post_without_csrf_is_refused(self):
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        response = self.url_open("/my/marketplace/notifications", data={"shipping": "1"})
        self.assertEqual(response.status_code, 400)

    def test_arabic_page_renders_rtl(self):
        lang = self.env["res.lang"]._activate_lang("ar_001")
        self.env["website"].search([], limit=1).language_ids = [Command.link(lang.id)]
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        response = self.url_open("/%s/my/marketplace/notifications" % lang.url_code)
        self.assertEqual(response.status_code, 200)
        dom = lxml_html.fromstring(response.content)
        self.assertTrue(dom.xpath("//div[@id='wrapwrap' and contains(@class, 'o_rtl')]"))
        self.assertTrue(dom.xpath("//form[@id='lm_notifications_form']"))

    def test_home_links_the_page_and_breadcrumb(self):
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/home")
        self.assertTrue(dom.xpath("//a[@href='/my/marketplace/notifications']"))
        entries = self.env["leaders.portal.nav"]._customer_entries()
        self.assertIn("/my/marketplace/notifications", [e["url"] for e in entries])
        page = self._dom("/my/marketplace/notifications")
        self.assertIn("Notification preferences", " ".join(page.xpath("//ol[contains(@class,'breadcrumb')]//li")[-1].text_content().split()))

    # -- wishlist -----------------------------------------------------------
    def test_wishlist_link_only_when_the_module_is_installed(self):
        Nav = self.env["leaders.portal.nav"]
        with patch(NAV, return_value=True):
            urls = [e["url"] for e in Nav._customer_entries()]
        self.assertIn("/shop/wishlist", urls)
        with patch(NAV, return_value=False):
            urls = [e["url"] for e in Nav._customer_entries()]
        self.assertNotIn("/shop/wishlist", urls)

    def test_wishlist_target_is_a_real_route_when_installed(self):
        installed = bool(self.env["ir.module.module"].sudo().search_count([
            ("name", "=", "website_sale_wishlist"), ("state", "=", "installed")]))
        self.assertEqual(self.env["leaders.portal.nav"]._module_installed("website_sale_wishlist"), installed)
        self.assertNotIn("website_sale_wishlist", self.env.ref("base.module_leaders_marketplace").dependencies_id.mapped("name"))
        routes = [str(rule.rule) for rule in self.env["ir.http"].routing_map().iter_rules()]
        self.assertEqual("/shop/wishlist" in routes, installed)
