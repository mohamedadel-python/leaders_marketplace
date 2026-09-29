"""FBM-only mode: the platform has no warehouse of its own (setting fbl_enabled)."""
from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD


class FblModeCommon(MarketplacePortalUserCommon):

    def _fbl(self, on):
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.fbl_enabled", "True" if on else "False")

    def _settings_off(self):
        settings = self.env["res.config.settings"].create({"marketplace_fbl_enabled": False})
        settings.set_values()


@tagged("post_install", "-at_install")
class TestFblDisabledModels(FblModeCommon):

    def test_default_is_on_and_seeded(self):
        param = self.env.ref("leaders_marketplace.param_fbl_enabled")
        self.assertEqual(param.value, "True")
        self.assertTrue(self.env["res.partner"]._leaders_fbl_enabled())
        self.assertTrue(self.env["leaders.marketplace.offer"]._leaders_fbl_allowed())
        self.assertTrue(self.fbl_offer.fbl_enabled)
        settings = self.env["res.config.settings"].create({})
        self.assertTrue(settings.marketplace_fbl_enabled)

    def test_missing_or_garbage_param_is_off_only_when_not_true(self):
        for value, expected in (("True", True), ("true", True), ("1", True), ("False", False), ("", False)):
            self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.fbl_enabled", value)
            self.assertEqual(self.env["res.partner"]._leaders_fbl_enabled(), expected, value)

    def test_default_still_creates_fbl_location(self):
        vendor = self.env["res.partner"].create({
            "name": "Default Vendor", "is_marketplace_vendor": True, "vendor_state": "approved"})
        vendor._marketplace_ensure_vendor_stock()
        self.assertTrue(vendor.vendor_fbl_location_id)
        self.assertTrue(vendor.vendor_warehouse_id)

    def test_off_vendor_stock_has_no_fbl_location(self):
        self._fbl(False)
        vendor = self.env["res.partner"].create({
            "name": "FBM Only Vendor", "is_marketplace_vendor": True, "vendor_state": "approved"})
        vendor._marketplace_ensure_vendor_stock()
        self.assertTrue(vendor.vendor_warehouse_id)
        self.assertEqual(vendor.vendor_location_id, vendor.vendor_warehouse_id.lot_stock_id)
        self.assertFalse(vendor.vendor_fbl_location_id)
        self.assertTrue(vendor._marketplace_stock_location("fbm"))
        with self.assertRaises(UserError):
            vendor._marketplace_stock_location("fbl")
        self.assertFalse(vendor.vendor_fbl_location_id)

    def test_offer_constraint(self):
        self._fbl(False)
        product = self.env["product.product"].create({
            "name": "FBL refused", "type": "consu", "is_storable": True, "taxes_id": [Command.clear()]})
        vals = {
            "product_tmpl_id": product.product_tmpl_id.id, "product_id": product.id,
            "vendor_id": self.vendor.id, "seller_sku": "NOFBL-1", "price": 10.0,
            "stock_location_id": self.vendor.vendor_location_id.id,
        }
        with self.assertRaises(ValidationError):
            self.env["leaders.marketplace.offer"].create(dict(vals, fulfillment_type="fbl"))
        offer = self.env["leaders.marketplace.offer"].create(dict(vals, fulfillment_type="fbm"))
        with self.assertRaises(ValidationError):
            offer.write({"fulfillment_type": "fbl"})
        with self.assertRaises(UserError):
            self.env["leaders.marketplace.offer"]._leaders_check_fulfillment_type("fbl")
        self.assertEqual(
            self.env["leaders.marketplace.offer"]._leaders_check_fulfillment_type("fbm"), "fbm")

    def test_policy_forcing_fbl_refused_and_stored_one_ignored(self):
        Policy = self.env["leaders.fulfillment.policy"]
        stored = Policy.create({"name": "Old FBL", "vendor_id": self.vendor.id,
                                "forced_fulfillment_type": "fbl"})
        self.assertEqual(Policy._policy_for(self.vendor, self.env["product.public.category"]), stored)
        self._fbl(False)
        self.assertFalse(Policy._policy_for(self.vendor, self.env["product.public.category"]))
        with self.assertRaises(UserError):
            Policy.create({"name": "New FBL", "forced_fulfillment_type": "fbl", "vendor_id": self.vendor.id})
        with self.assertRaises(UserError):
            stored.write({"forced_fulfillment_type": "fbl", "name": "touch"})
        Policy.create({"name": "FBM", "vendor_id": self.vendor.id, "forced_fulfillment_type": "fbm"})
        stored.active = False  # archived FBL policies are harmless

    def test_inbound_refused(self):
        self._fbl(False)
        with self.assertRaises(UserError):
            self.env["leaders.fulfillment.inbound"].create({"vendor_id": self.vendor.id})

    def test_blocking_lists_offers_and_stock(self):
        # The fixture holds an active FBL offer with FBL stock.
        with self.assertRaises(UserError) as cm:
            self._settings_off()
        message = str(cm.exception)
        self.assertIn("Split Vendor", message)
        self.assertIn("FBL-1", message)  # offer name
        self.assertIn("FBL stock of Split Vendor", message)
        self.assertTrue(self.env["res.partner"]._leaders_fbl_enabled())

    def _clear_all_fbl_usage(self):
        """Nothing in the database (demo or upgraded data included) may use FBL."""
        self.env["leaders.marketplace.offer"].with_context(active_test=False).search([
            ("fulfillment_type", "=", "fbl"), ("state", "!=", "rejected")]).write({"state": "rejected"})
        for partner in self.env["res.partner"].search([("vendor_fbl_location_id", "!=", False)]):
            for quant in self.env["stock.quant"].sudo().search([
                    ("location_id", "=", partner.vendor_fbl_location_id.id), ("quantity", "!=", 0)]):
                self.env["stock.quant"]._leaders_adjust_vendor_stock(
                    quant.product_id, quant.location_id, quant.owner_id, -quant.quantity)

    def test_turning_off_allowed_once_fbl_is_unused(self):
        self._clear_all_fbl_usage()
        self._settings_off()
        self.assertFalse(self.env["res.partner"]._leaders_fbl_enabled())
        # ... and back on.
        settings = self.env["res.config.settings"].create({"marketplace_fbl_enabled": True})
        settings.set_values()
        self.assertTrue(self.env["res.partner"]._leaders_fbl_enabled())

    def test_buybox_score_ignores_fbl_bonus(self):
        cfg = self.env["leaders.buybox.config"].get_active()
        cfg.write({"fbl_shipping_bonus": 1.0, "fbm_shipping_bonus": 0.0, "shipping_speed_weight": 10.0})
        self.env.flush_all()
        self.fbl_offer.invalidate_recordset(["buybox_score"])
        self.fbl_offer.invalidate_recordset(["buybox_score"])
        self.fbl_offer.modified(["fulfillment_type"])
        score_on = self.fbl_offer.buybox_score
        self._fbl(False)
        self.fbl_offer.modified(["fulfillment_type"])
        self.fbl_offer.invalidate_recordset(["buybox_score"])
        score_off = self.fbl_offer.buybox_score
        self.assertAlmostEqual(score_on - score_off, 10.0 * (1.0 - 0.0), places=4)

    def test_menus_hidden_when_off(self):
        Menu = self.env["ir.ui.menu"]
        inbound = self.env.ref("leaders_marketplace.menu_fbl_inbound")
        fee = self.env.ref("leaders_marketplace.menu_fulfillment_fee")
        policy = self.env.ref("leaders_marketplace.menu_fulfillment_policy")
        visible = Menu._visible_menu_ids()
        self.assertIn(inbound.id, visible)
        self.assertIn(fee.id, visible)
        self._fbl(False)
        visible = Menu._visible_menu_ids()
        self.assertNotIn(inbound.id, visible)
        self.assertNotIn(fee.id, visible)
        self.assertIn(policy.id, visible)

    def test_settings_and_views_hide_fbl(self):
        self._fbl(False)
        offer_view = self.fbl_offer.get_view(view_type="form")
        self.assertIn('invisible="not fbl_enabled"', offer_view["arch"])
        self.assertFalse(self.env["leaders.buybox.config"].get_active().fbl_enabled)

    def test_storage_fee_cron_skipped_when_off(self):
        self.fbl_product.volume = 1.0
        self.env["leaders.fulfillment.fee"].create({
            "name": "Storage", "fee_type": "storage", "calc_method": "per_cbm_day", "amount": 1.0})
        Tx = self.env["leaders.wallet.transaction"]
        self._fbl(False)
        self.env["leaders.fulfillment.fee"].cron_apply_storage_fees()
        self.assertFalse(Tx.search([("vendor_id", "=", self.vendor.id), ("type", "=", "storage_fee")]))
        self._fbl(True)
        self.env["leaders.fulfillment.fee"].cron_apply_storage_fees()
        self.assertTrue(Tx.search([("vendor_id", "=", self.vendor.id), ("type", "=", "storage_fee")]))

    def test_fbm_order_still_works_when_off_and_no_main_warehouse(self):
        self._fbl(False)
        Partner = type(self.env["res.partner"])
        empty = lambda self: self.env["stock.warehouse"].browse()  # noqa: E731
        from unittest.mock import patch
        with patch.object(Partner, "_marketplace_main_warehouse", empty):
            order = self._confirmed_sale_order(self.fbm_product, 1)
            group = order.vendor_order_ids.fulfillment_group_ids
            self.assertEqual(group.fulfillment_type, "fbm")
            self.assertTrue(group.picking_ids)
            vendor = self.env["res.partner"].create({
                "name": "No WH Vendor", "is_marketplace_vendor": True, "vendor_state": "approved"})
            vendor._marketplace_ensure_vendor_stock()
            self.assertFalse(vendor.vendor_fbl_location_id)

    def test_paths_safe_without_main_warehouse_or_fbl_location(self):
        from unittest.mock import patch
        Partner = type(self.env["res.partner"])
        empty = lambda self: self.env["stock.warehouse"].browse()  # noqa: E731
        with patch.object(Partner, "_marketplace_main_warehouse", empty):
            with self.assertRaises(UserError):
                self.env["res.partner"]._marketplace_fbl_root_location()
            with self.assertRaises(UserError):
                self.env["leaders.fulfillment.group"].new({"fulfillment_type": "fbl"})._picking_type()
        # Storage cron and stock lookups survive vendors that never had an FBL location.
        self.vendor.vendor_fbl_location_id = False
        self._fbl(False)
        self.env["leaders.fulfillment.fee"].cron_apply_storage_fees()
        self.assertTrue(self.vendor._marketplace_stock_location("fbm"))


@tagged("post_install", "-at_install")
class TestFblDisabledPortal(HttpCase, FblModeCommon):

    def setUp(self):
        super().setUp()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)

    def _new(self, sku, fulfillment_type):
        return self._post(
            "/my/vendor/products/new", "/my/vendor/products/new",
            name="Portal %s" % sku, seller_sku=sku, price="30", fulfillment_type=fulfillment_type,
            currency_id=str(self.env.company.currency_id.id))

    def test_form_offers_fbl_by_default_only(self):
        self.assertIn('value="fbl"', self.url_open("/my/vendor/products/new").text)
        self._fbl(False)
        page = self.url_open("/my/vendor/products/new")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('value="fbl"', page.text)
        self.assertIn('value="fbm"', page.text)
        edit = self.url_open("/my/vendor/products/%s/edit" % self.fbm_offer.id)
        self.assertNotIn('value="fbl"', edit.text)

    def test_create_route_refuses_fbl_when_off(self):
        Offer = self.env["leaders.marketplace.offer"]
        self._fbl(False)
        response = self._new("PORTAL-FBL-OFF", "fbl")
        self.assertIn("not available", response.text)
        self.assertFalse(Offer.search([("seller_sku", "=", "PORTAL-FBL-OFF")]))
        self._new("PORTAL-FBM-OFF", "fbm")
        self.assertEqual(Offer.search([("seller_sku", "=", "PORTAL-FBM-OFF")]).fulfillment_type, "fbm")

    def test_create_route_accepts_fbl_by_default(self):
        self._new("PORTAL-FBL-ON", "fbl")
        offer = self.env["leaders.marketplace.offer"].search([("seller_sku", "=", "PORTAL-FBL-ON")])
        self.assertEqual(offer.fulfillment_type, "fbl")

    def test_edit_route_refuses_switch_to_fbl_when_off(self):
        self._fbl(False)
        url = "/my/vendor/products/%s/edit" % self.fbm_offer.id
        response = self._post(
            url, url, name="Vendor FBM Product", seller_sku="FBM-1", price="100",
            fulfillment_type="fbl", currency_id=str(self.env.company.currency_id.id))
        self.assertIn("not available", response.text)
        self.assertEqual(self.fbm_offer.fulfillment_type, "fbm")
