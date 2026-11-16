"""A1: the zone carrier prices the platform's own shipments too."""
import importlib.util
import os

import odoo
from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_delivery_zone import TestDeliveryZone

CUSTOMER_PASSWORD = "lm-customer-pass"


class PlatformZoneCommon(TestDeliveryZone):
    """Fixtures: Cairo platform price 40; vendor prices 30 / 15 (parent class)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.platform_zone = cls.env["leaders.vendor.delivery.zone"].create({
            "is_platform": True, "state_id": cls.cairo.id, "price": 40.0,
        })

    def _rate(self, lines, address=None):
        order = self._order_with_carrier(lines, address or self.address_cairo)
        return order, self.carrier.marketplace_zone_rate_shipment(order)

    def _set(self, key, value):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.%s" % key, value)


@tagged("post_install", "-at_install")
class TestPlatformDeliveryRate(PlatformZoneCommon):

    def test_company_only_cart_pays_the_platform_price(self):
        _order, result = self._rate([(self.own_product, 1)])
        self.assertTrue(result["success"])
        self.assertEqual(result["price"], 40.0)

    def test_mixed_cart_charges_platform_once_plus_each_vendor(self):
        _order, result = self._rate(
            [(self.own_product, 2), (self.own_product, 1), (self.fbm_product, 1), (self.vendor2_product, 1)])
        self.assertEqual(result["price"], 40.0 + 30.0 + 15.0)

    def test_fbm_only_cart_is_unchanged(self):
        _order, result = self._rate([(self.fbm_product, 1), (self.vendor2_product, 1)])
        self.assertEqual(result["price"], 45.0)

    def test_fbl_cart_pays_the_platform_price_not_the_vendor_price(self):
        _order, result = self._rate([(self.fbl_product, 1)])
        self.assertEqual(result["price"], 40.0)

    def test_fbl_and_fbm_vendor_cart(self):
        _order, result = self._rate([(self.fbl_product, 1), (self.vendor2_product, 1)])
        self.assertEqual(result["price"], 40.0 + 15.0)

    def test_missing_platform_zone_is_a_clear_error_never_zero(self):
        _order, result = self._rate([(self.own_product, 1)], self.address_alexandria)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_message"], "We do not deliver to %s." % self.alexandria.name)

    def test_missing_platform_zone_blocks_fbl_too(self):
        _order, result = self._rate([(self.fbl_product, 1)], self.address_alexandria)
        self.assertFalse(result["success"])
        self.assertIn("We do not deliver to", result["error_message"])

    def test_platform_zone_is_unique_per_governorate(self):
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self.env["leaders.vendor.delivery.zone"].create({
                    "is_platform": True, "state_id": self.cairo.id, "price": 10.0})

    def test_vendor_zone_needs_a_vendor(self):
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self.env["leaders.vendor.delivery.zone"].create({
                    "state_id": self.alexandria.id, "price": 10.0})

    # ------------------------------------------------------ free shipping
    def test_platform_free_shipping_threshold(self):
        self.platform_zone.free_shipping_threshold = 100.0
        # own product is 50 each: 1 -> 50 (paid), 2 -> 100 (free)
        self.assertEqual(self._rate([(self.own_product, 1)])[1]["price"], 40.0)
        self.assertEqual(self._rate([(self.own_product, 2)])[1]["price"], 0.0)

    def test_platform_threshold_counts_company_and_fbl_lines_only(self):
        self.platform_zone.free_shipping_threshold = 80.0
        # 50 company + 100 FBM vendor goods: the vendor goods do not count.
        self.assertEqual(
            self._rate([(self.own_product, 1), (self.fbm_product, 1)])[1]["price"], 40.0 + 30.0)

    def test_vendor_free_shipping_threshold(self):
        zone = self.env["leaders.vendor.delivery.zone"].search([
            ("vendor_id", "=", self.vendor.id), ("state_id", "=", self.cairo.id)])
        zone.free_shipping_threshold = 100.0     # fbm_product sells at 100
        _order, result = self._rate([(self.fbm_product, 1), (self.vendor2_product, 1)])
        self.assertEqual(result["price"], 15.0)

    def test_threshold_default_is_off(self):
        self.assertFalse(self.platform_zone.free_shipping_threshold)
        self.assertEqual(self._rate([(self.own_product, 100)])[1]["price"], 40.0)

    # ------------------------------------------------------------ breakdown
    def test_breakdown_before_the_split(self):
        order = self._order_with_carrier(
            [(self.own_product, 1), (self.fbm_product, 1), (self.vendor2_product, 1)], self.address_cairo)
        breakdown = order._leaders_delivery_breakdown()
        self.assertEqual(breakdown[0], ("Leaders delivery", 40.0))
        self.assertEqual(
            sorted(a for _l, a in breakdown[1:]), [15.0, 30.0])
        text = order._leaders_delivery_breakdown_text()
        self.assertIn("Leaders delivery: ", text)
        self.assertIn(" · ", text)
        self.assertIn("%s delivery: " % (self.vendor2.vendor_store_name or self.vendor2.name), text)

    def test_breakdown_says_free(self):
        self.platform_zone.free_shipping_threshold = 50.0
        order = self._order_with_carrier([(self.own_product, 1)], self.address_cairo)
        self.assertEqual(order._leaders_delivery_breakdown_text(), "Leaders delivery: Free")

    def test_breakdown_is_empty_for_other_carriers(self):
        fixed = self.env.ref("delivery.free_delivery_carrier")
        order = self._order_with_carrier([(self.own_product, 1)], self.address_cairo)
        order.carrier_id = fixed
        self.assertEqual(order._leaders_delivery_breakdown(), [])


@tagged("post_install", "-at_install")
class TestPlatformDeliverySplit(PlatformZoneCommon):

    def _cart(self, lines, price=None):
        order = self._order_with_carrier(lines, self.address_cairo, with_delivery_line=True)
        rate = self.carrier.marketplace_zone_rate_shipment(order)
        order.order_line.filtered("is_delivery").price_unit = rate["price"] if price is None else price
        return order

    def test_mixed_cart_platform_part_stays_on_the_company_order(self):
        order = self._cart([(self.own_product, 1), (self.fbm_product, 1), (self.vendor2_product, 1)])
        self.assertEqual(order.order_line.filtered("is_delivery").price_unit, 85.0)
        order.action_confirm()
        self.assertEqual(order.order_line.filtered("is_delivery").price_unit, 40.0)
        by_vendor = {o.vendor_id: o.line_ids.filtered("is_delivery").price_unit
                     for o in order.vendor_order_ids}
        self.assertEqual(by_vendor, {self.vendor: 30.0, self.vendor2: 15.0})
        self.assertEqual(
            order._leaders_delivery_breakdown()[0], ("Leaders delivery", 40.0))
        self.assertEqual(len(order._leaders_delivery_breakdown()), 3)

    def test_company_only_cart_keeps_the_whole_delivery(self):
        order = self._cart([(self.own_product, 1)])
        order.action_confirm()
        self.assertEqual(order.order_line.filtered("is_delivery").price_unit, 40.0)

    def test_fbl_only_cart_default_vendor_mode_pays_the_vendor_the_platform_price(self):
        order = self._cart([(self.fbl_product, 1)])
        order.action_confirm()
        self.assertEqual(order.vendor_order_ids.line_ids.filtered("is_delivery").price_unit, 40.0)

    def test_fbl_only_cart_platform_mode_books_platform_income(self):
        self._configure_accounts()
        self._set("fbl_delivery_income", "platform")
        order = self._cart([(self.fbl_product, 1)])
        order.action_confirm()
        self.assertFalse(order.vendor_order_ids.line_ids.filtered("is_delivery"))
        move = self.env["account.move"].search([
            ("ref", "like", "Marketplace delivery income%")], limit=1)
        self.assertEqual(sum(move.line_ids.mapped("credit")), 40.0)

    def test_fbl_plus_fbm_vendor_splits_remainder_and_vendor_price(self):
        order = self._cart([(self.fbl_product, 1), (self.vendor2_product, 1)])   # 40 + 15
        order.action_confirm()
        by_vendor = {o.vendor_id: sorted(o.line_ids.filtered("is_delivery").mapped("price_unit"))
                     for o in order.vendor_order_ids}
        self.assertEqual(by_vendor[self.vendor], [40.0])
        self.assertEqual(by_vendor[self.vendor2], [15.0])

    def test_free_vendor_shipping_moves_zero_from_the_company_line(self):
        zone = self.env["leaders.vendor.delivery.zone"].search([
            ("vendor_id", "=", self.vendor.id), ("state_id", "=", self.cairo.id)])
        zone.free_shipping_threshold = 100.0
        order = self._cart([(self.own_product, 1), (self.fbm_product, 1)])
        self.assertEqual(order.order_line.filtered("is_delivery").price_unit, 40.0)
        order.action_confirm()
        self.assertEqual(order.order_line.filtered("is_delivery").price_unit, 40.0)
        self.assertEqual(
            order.vendor_order_ids.line_ids.filtered("is_delivery").price_unit, 0.0)


@tagged("post_install", "-at_install")
class TestPlatformDeliveryPages(HttpCase, PlatformZoneCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer_partner = cls.env["res.partner"].create({
            "name": "Breakdown Customer", "email": "lm.breakdown@example.com",
            "state_id": cls.cairo.id, "country_id": cls.egypt.id,
        })
        cls.customer_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Breakdown Customer", "login": "lm.breakdown@example.com",
            "email": "lm.breakdown@example.com", "password": CUSTOMER_PASSWORD,
            "partner_id": cls.customer_partner.id,
            "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
        })

    def _order(self, with_delivery=True):
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

    @staticmethod
    def _load_migration():
        path = os.path.join(
            os.path.dirname(__file__), "..", "migrations", "18.0.4.0.54",
            "post-A1-order-mail-delivery-breakdown.py")
        spec = importlib.util.spec_from_file_location("lm_a1_mail_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_cart_shows_the_breakdown(self):
        order = self._order()
        session = self.authenticate(self.customer_user.login, CUSTOMER_PASSWORD)
        session["sale_order_id"] = order.id
        odoo.http.root.session_store.save(session)
        page = self.url_open("/shop/cart").text
        self.assertIn("Leaders delivery", page)
        self.assertIn("delivery", page)
        self.assertIn(self.vendor.vendor_store_name or self.vendor.name, page)

    def test_confirmation_page_and_email_show_the_breakdown(self):
        self._set("vendor_only_presentation", "True")
        # Databases created before A1 hold the old (noupdate) body: the
        # migration upgrades it, on a fresh one it is a no-op.
        migration = self._load_migration()
        migration.migrate(self.env.cr, None)
        migration.migrate(self.env.cr, None)    # idempotent
        self.env.invalidate_all()
        tpl = self.env.ref("leaders_marketplace.mail_template_marketplace_order_confirmation")
        self.assertIn("_leaders_delivery_breakdown_text", tpl.body_html)
        order = self._order()
        order.with_context(send_email=True).action_confirm()
        session = self.authenticate(self.customer_user.login, CUSTOMER_PASSWORD)
        session["sale_last_order_id"] = order.id
        odoo.http.root.session_store.save(session)
        page = self.url_open("/shop/confirmation").text
        self.assertIn("Leaders delivery", page)
        self.assertIn("40.00", page)
        mail = self.env["mail.mail"].sudo().search([
            ("model", "=", "sale.order"), ("res_id", "=", order.id),
            ("subject", "ilike", "marketplace order")], limit=1)
        self.assertTrue(mail)
        self.assertIn("Leaders delivery", mail.body_html)
        self.assertIn(" · ", mail.body_html)
