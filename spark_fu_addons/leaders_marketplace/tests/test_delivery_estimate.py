"""D4: optional delivery estimate ("Arrives in X-Y days") per seller."""
import odoo
from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase
from lxml import html as lxml_html

from .test_delivery_platform import PlatformZoneCommon, CUSTOMER_PASSWORD
from .test_security_rpc import PORTAL_PASSWORD, MarketplacePortalUserCommon


class EstimateCommon(PlatformZoneCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.zone_model = cls.env["leaders.vendor.delivery.zone"]
        cls.vendor_zone = cls.zone_model.search([
            ("vendor_id", "=", cls.vendor.id), ("state_id", "=", cls.cairo.id)])
        cls.vendor2_zone = cls.zone_model.search([
            ("vendor_id", "=", cls.vendor2.id), ("state_id", "=", cls.cairo.id)])

    def _set_days(self):
        self.platform_zone.write({"lead_days_min": 1, "lead_days_max": 2})
        self.vendor_zone.write({"lead_days_min": 3, "lead_days_max": 5})

    def _cart(self, lines):
        order = self._order_with_carrier(lines, self.address_cairo, with_delivery_line=True)
        order.order_line.filtered("is_delivery").price_unit = (
            self.carrier.marketplace_zone_rate_shipment(order)["price"])
        return order

    def _mixed(self):
        return self._order_with_carrier(
            [(self.own_product, 1), (self.fbm_product, 1), (self.vendor2_product, 1)],
            self.address_cairo)


@tagged("post_install", "-at_install")
class TestDeliveryEstimate(EstimateCommon):

    def test_defaults_are_empty(self):
        self.assertEqual((self.vendor_zone.lead_days_min, self.vendor_zone.lead_days_max), (0, 0))
        self.assertEqual(self.vendor_zone._delivery_estimate_text(), "")
        self.assertEqual(self._mixed()._leaders_delivery_estimates(), [])

    def test_constraints(self):
        for vals in ({"lead_days_min": -1}, {"lead_days_max": -2},
                     {"lead_days_min": 5, "lead_days_max": 3}):
            with self.assertRaises(ValidationError):
                self.vendor_zone.write(vals)
        # only min, only max, both, equal are all valid
        for vals in ({"lead_days_min": 2, "lead_days_max": 0}, {"lead_days_min": 0, "lead_days_max": 4},
                     {"lead_days_min": 2, "lead_days_max": 2}, {"lead_days_min": 0, "lead_days_max": 0}):
            self.vendor_zone.write(vals)

    def test_text_variants(self):
        text = self.zone_model._lead_days_text
        self.assertEqual(text(2, 4), "Arrives in 2–4 days")
        self.assertEqual(text(3, 3), "Arrives in 3 days")
        self.assertEqual(text(1, 1), "Arrives in 1 day")
        self.assertEqual(text(0, 4), "Arrives within 4 days")
        self.assertEqual(text(2, 0), "Arrives in 2+ days")
        self.assertEqual(text(0, 0), "")

    def test_mixed_cart_platform_and_vendor_estimates(self):
        self._set_days()
        estimates = self._mixed()._leaders_delivery_estimates()
        name = self.vendor.vendor_store_name or self.vendor.name
        self.assertEqual(estimates, [
            ("Leaders", "Arrives in 1–2 days"), (name, "Arrives in 3–5 days")])   # vendor2 has none

    def test_fbl_line_uses_the_platform_zone(self):
        self._set_days()
        order = self._order_with_carrier([(self.fbl_product, 1)], self.address_cairo)
        self.assertEqual(order._leaders_delivery_estimates(), [("Leaders", "Arrives in 1–2 days")])

    def test_other_governorate_and_other_carrier_show_nothing(self):
        self._set_days()
        order = self._order_with_carrier([(self.own_product, 1)], self.address_alexandria)
        self.assertEqual(order._leaders_delivery_estimates(), [])
        order = self._mixed()
        order.carrier_id = self.env.ref("delivery.free_delivery_carrier")
        self.assertEqual(order._leaders_delivery_estimates(), [])

    def test_after_the_split(self):
        self._set_days()
        order = self._cart([(self.own_product, 1), (self.fbm_product, 1)])
        order.action_confirm()
        self.assertTrue(order.vendor_order_ids)
        name = self.vendor.vendor_store_name or self.vendor.name
        self.assertEqual(order._leaders_delivery_estimates(), [
            ("Leaders", "Arrives in 1–2 days"), (name, "Arrives in 3–5 days")])
        self.assertEqual(order._leaders_platform_delivery_estimate(), "Arrives in 1–2 days")
        self.assertEqual(order.vendor_order_ids._leaders_delivery_estimates(),
                         [(name, "Arrives in 3–5 days")])

    def test_after_the_split_vendor_only_has_no_platform_part(self):
        self._set_days()
        order = self._cart([(self.fbm_product, 1)])
        order.action_confirm()
        estimates = order._leaders_delivery_estimates()
        self.assertEqual([t for _l, t in estimates], ["Arrives in 3–5 days"])

    def test_backend_views_load(self):
        for view in ("view_vendor_delivery_zone_list", "view_vendor_delivery_zone_form"):
            arch = self.env.ref("leaders_marketplace.%s" % view).get_view(
                self.env.ref("leaders_marketplace.%s" % view).id)
            self.assertIn("lead_days_min", arch["arch"] if isinstance(arch, dict) else str(arch))
        self.zone_model.get_views([(False, "list"), (False, "form"), (False, "search")])


@tagged("post_install", "-at_install")
class TestDeliveryEstimatePages(HttpCase, EstimateCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer_partner = cls.env["res.partner"].create({
            "name": "Estimate Customer", "email": "lm.estimate@example.com",
            "state_id": cls.cairo.id, "country_id": cls.egypt.id,
        })
        cls.customer_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Estimate Customer", "login": "lm.estimate@example.com",
            "email": "lm.estimate@example.com", "password": CUSTOMER_PASSWORD,
            "partner_id": cls.customer_partner.id,
            "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
        })
        website = cls.env["website"].search([], limit=1)
        lang = cls.env["res.lang"]._activate_lang("ar_001")
        website.language_ids = [Command.link(lang.id)]
        cls.ar_prefix = "/" + lang.url_code

    def _order(self):
        lines = [Command.create({"product_id": p.id, "product_uom_qty": 1})
                 for p in (self.own_product, self.fbm_product)]
        order = self.env["sale.order"].create({
            "partner_id": self.customer_partner.id,
            "partner_shipping_id": self.customer_partner.id,
            "carrier_id": self.carrier.id,
            "website_id": self.env["website"].search([], limit=1).id,
            "order_line": lines + [Command.create({
                "product_id": self.carrier.product_id.id, "is_delivery": True,
                "product_uom_qty": 1, "price_unit": 70.0})],
        })
        order.message_subscribe(partner_ids=self.customer_partner.ids)
        return order

    def test_cart_and_confirmation_show_the_estimates(self):
        self._set_days()
        order = self._order()
        session = self.authenticate(self.customer_user.login, CUSTOMER_PASSWORD)
        session["sale_order_id"] = order.id
        odoo.http.root.session_store.save(session)
        page = self.url_open("/shop/cart").text
        self.assertIn("Arrives in 1–2 days", page)
        self.assertIn("Arrives in 3–5 days", page)
        order.action_confirm()
        session = self.authenticate(self.customer_user.login, CUSTOMER_PASSWORD)
        session["sale_last_order_id"] = order.id
        odoo.http.root.session_store.save(session)
        page = self.url_open("/shop/confirmation").text
        self.assertIn("Arrives in 1–2 days", page)
        self.assertIn("Arrives in 3–5 days", page)

    def test_cart_shows_nothing_when_empty(self):
        order = self._order()
        session = self.authenticate(self.customer_user.login, CUSTOMER_PASSWORD)
        session["sale_order_id"] = order.id
        odoo.http.root.session_store.save(session)
        self.assertNotIn("Arrives", self.url_open("/shop/cart").text)

    def test_order_pages_en_and_ar(self):
        self._set_days()
        order = self._order()
        order.action_confirm()
        vorder = order.vendor_order_ids
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.unified_customer_orders", "True")
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, CUSTOMER_PASSWORD)
        for lang, prefix in (("en", ""), ("ar", self.ar_prefix)):
            page = self.url_open("%s/my/marketplace/orders/%s" % (prefix, vorder.id))
            self.assertEqual(page.status_code, 200)
            dom = lxml_html.fromstring(page.content)
            self.assertEqual(bool(dom.xpath("//div[@id='wrapwrap' and contains(@class, 'o_rtl')]")), lang == "ar")
            box = dom.xpath("//*[@id='lm_order_delivery_estimate']")
            self.assertTrue(box, lang)
            if lang == "en":
                self.assertIn("Arrives in 3–5 days", box[0].text_content())
            page = self.url_open("%s/my/marketplace/checkouts/%s" % (prefix, order.id))
            self.assertEqual(page.status_code, 200)
            dom = lxml_html.fromstring(page.content)
            self.assertEqual(len(dom.xpath("//*[contains(@class, 'lm-delivery-estimate')]")), 2, lang)
            if lang == "en":
                self.assertIn("Arrives in 1–2 days", page.text)
                self.assertIn("Arrives in 3–5 days", page.text)

    def test_order_page_shows_nothing_when_empty(self):
        order = self._order()
        order.action_confirm()
        self.authenticate(self.customer_user.login, CUSTOMER_PASSWORD)
        page = self.url_open("/my/marketplace/orders/%s" % order.vendor_order_ids.id)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Arrives", page.text)


@tagged("post_install", "-at_install")
class TestVendorPortalEstimateForm(HttpCase, MarketplacePortalUserCommon):

    def test_owner_saves_days_and_staff_cannot(self):
        Zone = self.env["leaders.vendor.delivery.zone"]
        cairo = self.env.ref("base.state_eg_c")
        zones_url = "/my/vendor/delivery-zones"
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self._post("%s/new" % zones_url, zones_url, state_id=str(cairo.id), price="35",
                   lead_days_min="2", lead_days_max="4")
        zone = Zone.search([("vendor_id", "=", self.vendor.id), ("state_id", "=", cairo.id)])
        self.assertEqual((zone.lead_days_min, zone.lead_days_max), (2, 4))
        page = self.url_open(zones_url).text
        self.assertIn('name="lead_days_min"', page)
        self._post("%s/%s/update" % (zones_url, zone.id), zones_url, price="35",
                   lead_days_min="", lead_days_max="6")
        self.assertEqual((zone.lead_days_min, zone.lead_days_max), (0, 6))
        # invalid range is refused, nothing changes
        self._post("%s/%s/update" % (zones_url, zone.id), zones_url, price="35",
                   lead_days_min="9", lead_days_max="3")
        self.assertEqual((zone.lead_days_min, zone.lead_days_max), (0, 6))
        # staff cannot
        self.authenticate(self.staff_user.login, PORTAL_PASSWORD)
        token = self._csrf(zones_url)
        response = self.url_open("%s/%s/update" % (zones_url, zone.id), data={
            "csrf_token": token, "price": "35", "lead_days_max": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(zone.lead_days_max, 6)
