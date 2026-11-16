"""Vendor KPI snapshots, which nothing used to create.

The model and both screens existed, but no code ever wrote a row, so
/my/vendor/performance and the backend KPI menu were always empty.
"""
from dateutil.relativedelta import relativedelta

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import HttpCase
from psycopg2 import IntegrityError
from odoo.tools import mute_logger

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD


class KpiCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.KPI = cls.env["leaders.vendor.kpi"]
        cls.month = cls.KPI._period_start(fields.Date.today())
        cls.manager = cls.env["res.users"].create({
            "name": "KPI Manager", "login": "lm.kpi.mgr@example.com",
            "email": "lm.kpi.mgr@example.com",
            "groups_id": [Command.set([
                cls.env.ref("base.group_user").id,
                cls.env.ref("leaders_marketplace.group_marketplace_manager").id,
            ])],
        })
        cls.env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.fbm_product, cls.vendor.vendor_location_id, cls.vendor, 95)

    def _order(self, qty=1):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({
                "product_id": self.fbm_product.id, "product_uom_qty": qty})],
        })
        order.action_confirm()
        return order.vendor_order_ids

    def _completed(self, ref):
        vendor_order = self._order()
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = ref
        group.action_ship()
        group.action_delivered()
        return vendor_order

    def _fulfilled(self, ref):
        vendor_order = self._order()
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = ref
        group.action_ship()
        return vendor_order

    def _cancelled(self):
        vendor_order = self._order()
        vendor_order.action_cancel()
        return vendor_order


@tagged("post_install", "-at_install")
class TestKpiComputation(KpiCommon):

    def test_the_worked_example(self):
        """3 orders: one completed, one cancelled, one fulfilled."""
        self._completed("KPI-C")
        self._cancelled()
        self._fulfilled("KPI-F")

        values = self.KPI._compute_for_period(self.vendor, self.month)

        self.assertEqual(values["order_count"], 3)
        self.assertAlmostEqual(values["completion_rate"], 33.33, places=2)
        self.assertAlmostEqual(values["cancellation_rate"], 33.33, places=2)

    def test_a_pending_order_is_not_counted(self):
        """Nothing has happened to it yet; counting it would skew every rate."""
        self._completed("KPI-P1")
        pending = self.env["leaders.vendor.order"].create({
            "vendor_id": self.vendor.id,
            "partner_id": self.customer.id,
            "currency_id": self.company.currency_id.id,
            "date_order": fields.Datetime.now(),
        })
        self.assertEqual(pending.state, "pending")

        values = self.KPI._compute_for_period(self.vendor, self.month)

        # Two vendor orders exist; only the one that actually moved counts.
        self.assertEqual(values["order_count"], 1)
        self.assertEqual(values["completion_rate"], 100.0)

    def test_an_empty_month_is_all_zeroes_not_a_crash(self):
        values = self.KPI._compute_for_period(
            self.vendor, self.month - relativedelta(months=6))
        self.assertEqual(values["order_count"], 0)
        self.assertEqual(values["completion_rate"], 0.0)
        self.assertEqual(values["cancellation_rate"], 0.0)
        self.assertEqual(values["avg_ship_hours"], 0.0)

    def test_average_ship_hours_uses_shipped_orders_only(self):
        vendor_order = self._completed("KPI-SH")
        vendor_order.date_order = vendor_order.date_shipped - relativedelta(hours=5)
        self._cancelled()           # never shipped, must not drag the mean
        values = self.KPI._compute_for_period(self.vendor, self.month)
        self.assertAlmostEqual(values["avg_ship_hours"], 5.0, places=1)

    def test_the_rating_is_the_mean_of_the_active_offers(self):
        self.fbm_offer.rating = 4.0
        self.fbl_offer.rating = 2.0
        values = self.KPI._compute_for_period(self.vendor, self.month)
        self.assertAlmostEqual(values["avg_rating"], 3.0, places=2)

    def test_orders_of_another_month_are_excluded(self):
        self._completed("KPI-M1")
        last_month = self.month - relativedelta(months=1)
        self.assertEqual(
            self.KPI._compute_for_period(self.vendor, last_month)["order_count"], 0)


@tagged("post_install", "-at_install")
class TestKpiUpsert(KpiCommon):

    def _rows(self):
        return self.KPI.search([
            ("vendor_id", "=", self.vendor.id), ("period", "=", self.month)])

    def test_recomputing_is_idempotent(self):
        self._completed("KPI-I1")
        self.KPI._upsert_for_period(self.vendor, self.month)
        self.KPI._upsert_for_period(self.vendor, self.month)
        self.KPI._upsert_for_period(self.vendor, self.month)
        self.assertEqual(len(self._rows()), 1, "recompute created duplicates")

    def test_recomputing_picks_up_new_orders(self):
        self._completed("KPI-U1")
        snapshot = self.KPI._upsert_for_period(self.vendor, self.month)
        self.assertEqual(snapshot.order_count, 1)

        self._cancelled()
        self.KPI._upsert_for_period(self.vendor, self.month)

        self.assertEqual(snapshot.order_count, 2)
        self.assertAlmostEqual(snapshot.cancellation_rate, 50.0, places=2)

    @mute_logger("odoo.sql_db")
    def test_two_rows_for_the_same_month_are_refused(self):
        self.KPI._upsert_for_period(self.vendor, self.month)
        with self.assertRaises(IntegrityError):
            with self.cr.savepoint():
                self.KPI.create({"vendor_id": self.vendor.id, "period": self.month})

    def test_the_cron_covers_this_month_and_the_last(self):
        self._completed("KPI-CR")
        self.KPI.cron_compute_kpis()
        periods = self.KPI.search([("vendor_id", "=", self.vendor.id)]).mapped("period")
        self.assertIn(self.month, periods)
        self.assertIn(self.month - relativedelta(months=1), periods)

    def test_the_cron_skips_a_rejected_vendor(self):
        self.vendor.vendor_state = "rejected"
        self.KPI.cron_compute_kpis()
        self.assertFalse(self.KPI.search([("vendor_id", "=", self.vendor.id)]))

    def test_the_recompute_button_is_manager_only(self):
        snapshot = self.KPI._upsert_for_period(self.vendor, self.month)
        with self.assertRaises(AccessError):
            snapshot.with_user(self.owner_user).action_recompute_kpis()
        snapshot.with_user(self.manager).action_recompute_kpis()   # no raise

    def test_recompute_all_is_manager_only(self):
        with self.assertRaises(AccessError):
            self.KPI.with_user(self.owner_user).action_recompute_all_kpis()
        self.KPI.with_user(self.manager).action_recompute_all_kpis()


@tagged("post_install", "-at_install")
class TestKpiAutoWarning(KpiCommon):

    def _set(self, enabled, threshold=20.0):
        param = self.env["ir.config_parameter"].sudo()
        param.set_param("leaders_marketplace.kpi_auto_warning", enabled)
        param.set_param("leaders_marketplace.kpi_cancellation_threshold", threshold)

    def _draft_warnings(self):
        return self.env["leaders.vendor.penalty"].search([
            ("vendor_id", "=", self.vendor.id),
            ("level", "=", "warning"), ("state", "=", "draft")])

    def test_off_by_default(self):
        self._cancelled()
        self.KPI.cron_compute_kpis()
        self.assertFalse(self._draft_warnings())

    def test_a_high_rate_prepares_a_draft_warning_only(self):
        self._set(True, threshold=20.0)
        self._cancelled()           # 100% cancellation
        self.KPI.cron_compute_kpis()
        warning = self._draft_warnings()
        self.assertEqual(len(warning), 1)
        self.assertEqual(warning.state, "draft", "a threshold must never punish by itself")
        self.assertEqual(self.vendor.vendor_state, "approved")
        self.assertIn("Cancellation rate", warning.reason)

    def test_a_rate_under_the_threshold_prepares_nothing(self):
        self._set(True, threshold=80.0)
        self._completed("KPI-W1")
        self._cancelled()           # 50%, under 80
        self.KPI.cron_compute_kpis()
        self.assertFalse(self._draft_warnings())

    def test_the_same_month_is_not_flagged_twice(self):
        self._set(True, threshold=20.0)
        self._cancelled()
        self.KPI.cron_compute_kpis()
        self.KPI.cron_compute_kpis()
        self.assertEqual(len(self._draft_warnings()), 1)


@tagged("post_install", "-at_install")
class TestKpiPortal(HttpCase, KpiCommon):

    def test_the_performance_page_shows_the_months(self):
        self._completed("KPI-PT")
        self._cancelled()
        self.KPI._upsert_for_period(self.vendor, self.month)

        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/performance")

        self.assertEqual(page.status_code, 200)
        self.assertNotIn("No performance data yet", page.text)
        # Both rates are 50%; a raw float would render "50.0", so asserting the
        # two-decimal form is what proves the formatting, not the value.
        # twice in the monthly table and twice in the KPI cards of the latest month
        self.assertEqual(page.text.count("50.00%"), 4)
        self.assertNotIn(">50.0<", page.text)

    def test_a_vendor_with_no_snapshot_is_told_so(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/performance")
        self.assertEqual(page.status_code, 200)
        self.assertIn("No performance data yet", page.text)
