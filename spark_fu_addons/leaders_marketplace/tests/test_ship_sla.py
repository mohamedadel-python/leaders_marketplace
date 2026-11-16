"""Ship-by SLA (TASK 29 / A7)."""
import importlib.util
from datetime import timedelta

from odoo import Command, fields
from odoo.modules.module import get_module_path
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import PORTAL_PASSWORD, MarketplacePortalUserCommon


def _migration(name):
    path = get_module_path("leaders_marketplace") + "/migrations/18.0.4.0.54/%s.py" % name
    spec = importlib.util.spec_from_file_location("lm_migration_%s" % name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShipSlaCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.param = cls.env["ir.config_parameter"].sudo()
        cls.KPI = cls.env["leaders.vendor.kpi"]
        cls.Penalty = cls.env["leaders.vendor.penalty"]
        cls.reviewer = cls.env["res.users"].create({
            "name": "SLA Reviewer", "login": "lm.sla.reviewer@example.com",
            "email": "lm.sla.reviewer@example.com",
            "groups_id": [Command.set([
                cls.env.ref("base.group_user").id,
                cls.env.ref("leaders_marketplace.group_marketplace_reviewer").id])],
        })
        cls.env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.fbm_product, cls.vendor.vendor_location_id, cls.vendor, 95)

    def _order(self, qty=1):
        return self._confirmed_sale_order(self.fbm_product, qty).vendor_order_ids

    def _make_stale_late(self, order, hours=3):
        """Deadline in the past, is_late left False (as when only the clock moved)."""
        self.env.flush_all()
        past = fields.Datetime.now() - timedelta(hours=hours)
        self.env.cr.execute(
            "UPDATE leaders_vendor_order SET ship_by_date = %s, is_late = FALSE WHERE id = %s",
            [past, order.id])
        order.invalidate_recordset()

    def _late_mails(self):
        return self.env["mail.mail"].sudo().search([("subject", "like", "is late: ship it now")])

    def _late_activities(self, order):
        return self.env["mail.activity"].sudo().search([
            ("res_model", "=", "leaders.vendor.order"), ("res_id", "=", order.id)])


@tagged("post_install", "-at_install")
class TestShipSla(ShipSlaCommon):

    def test_ship_by_uses_the_global_setting_default_2(self):
        before = fields.Datetime.now()
        order = self._order()
        self.assertTrue(order.ship_by_date)
        delta = order.ship_by_date - before
        self.assertTrue(timedelta(days=2) <= delta < timedelta(days=2, minutes=5), delta)
        self.assertFalse(order.is_late)

    def test_global_setting_changes_the_deadline(self):
        self.param.set_param("leaders_marketplace.ship_sla_days", "5")
        before = fields.Datetime.now()
        order = self._order()
        self.assertTrue(timedelta(days=5) <= order.ship_by_date - before < timedelta(days=5, minutes=5))

    def test_tier_override_wins_and_zero_means_global(self):
        tier = self.env["leaders.vendor.tier"].create({
            "name": "Fast", "code": "FAST-SLA", "ship_sla_days": 1})
        self.vendor.vendor_tier_id = tier
        before = fields.Datetime.now()
        order = self._order()
        self.assertTrue(timedelta(days=1) <= order.ship_by_date - before < timedelta(days=1, minutes=5))
        tier.ship_sla_days = 0
        before = fields.Datetime.now()
        order = self._order()
        self.assertTrue(timedelta(days=2) <= order.ship_by_date - before < timedelta(days=2, minutes=5))

    def test_is_late_compute_and_cron_flip(self):
        order = self._order()
        self.assertFalse(order.is_late)
        self._make_stale_late(order)
        self.assertFalse(order.is_late, "nothing refreshed it yet")
        self.env["leaders.vendor.order"].cron_late_shipments()
        order.invalidate_recordset()
        self.assertTrue(order.is_late)

    def test_shipping_clears_is_late(self):
        order = self._order()
        self._make_stale_late(order)
        self.env["leaders.vendor.order"].cron_late_shipments()
        self.assertTrue(order.is_late)
        group = order.fulfillment_group_ids
        group.tracking_ref = "SLA-1"
        group.action_ship()
        self.assertFalse(order.is_late)

    def test_cancelled_order_is_never_late(self):
        order = self._order()
        self._make_stale_late(order)
        order.action_cancel()
        self.env["leaders.vendor.order"].cron_late_shipments()
        order.invalidate_recordset()
        self.assertFalse(order.is_late)

    def test_notification_once_activity_and_email(self):
        order = self._order()
        self.param.set_param("leaders_marketplace.notify_vendor_order_late", "True")
        self._make_stale_late(order)
        cron = self.env["leaders.vendor.order"].cron_late_shipments
        mails = len(self._late_mails())
        cron()
        self.assertTrue(order.late_notified)
        activities = self._late_activities(order).filtered(lambda a: a.user_id == self.reviewer)
        self.assertEqual(len(activities), 1)
        self.assertEqual(len(self._late_mails()), mails + 1)
        cron()
        self.assertEqual(len(self._late_activities(order).filtered(lambda a: a.user_id == self.reviewer)), 1)
        self.assertEqual(len(self._late_mails()), mails + 1)

    def test_notification_setting_off_notifies_nobody(self):
        order = self._order()
        self.param.set_param("leaders_marketplace.notify_vendor_order_late", "False")
        self._make_stale_late(order)
        mails = len(self._late_mails())
        self.env["leaders.vendor.order"].cron_late_shipments()
        order.invalidate_recordset()
        self.assertTrue(order.is_late, "the flag is data and stays")
        self.assertFalse(order.late_notified)
        self.assertFalse(self._late_activities(order))
        self.assertEqual(len(self._late_mails()), mails)

    def test_kpi_late_shipment_rate(self):
        now = fields.Datetime.now()
        on_time, late_shipped, unshipped_late, open_ok = (self._order() for _i in range(4))
        on_time.write({"ship_by_date": now + timedelta(days=1), "date_shipped": now})
        late_shipped.write({
            "ship_by_date": now - timedelta(days=1), "date_shipped": now})
        unshipped_late.write({"ship_by_date": now - timedelta(hours=1)})
        open_ok.write({"ship_by_date": now + timedelta(days=1)})
        month = self.KPI._period_start(fields.Date.today())
        values = self.KPI._compute_for_period(self.vendor, month)
        self.assertEqual(values["order_count"], 4)
        self.assertAlmostEqual(values["late_shipment_rate"], 50.0)

    def test_kpi_late_rate_excludes_cancelled_orders(self):
        now = fields.Datetime.now()
        ok = self._order()
        ok.write({"ship_by_date": now + timedelta(days=1)})
        cancelled = self._order()
        cancelled.write({"ship_by_date": now - timedelta(days=1)})
        cancelled.action_cancel()
        values = self.KPI._compute_for_period(self.vendor, self.KPI._period_start(fields.Date.today()))
        self.assertAlmostEqual(values["late_shipment_rate"], 0.0)

    def _draft_late_warnings(self):
        return self.Penalty.search([
            ("vendor_id", "=", self.vendor.id), ("level", "=", "warning"),
            ("state", "=", "draft"), ("reason", "like", "Late shipment rate")])

    def test_penalty_only_when_setting_on_and_above_threshold(self):
        order = self._order()
        order.write({"ship_by_date": fields.Datetime.now() - timedelta(hours=1)})   # 100% late
        self.KPI.cron_compute_kpis()
        self.assertFalse(self._draft_late_warnings(), "off by default")
        self.param.set_param("leaders_marketplace.late_shipment_auto_warning", "True")
        self.param.set_param("leaders_marketplace.late_shipment_rate_threshold", "150")
        self.KPI.cron_compute_kpis()
        self.assertFalse(self._draft_late_warnings(), "under the threshold")
        self.param.set_param("leaders_marketplace.late_shipment_rate_threshold", "20")
        self.KPI.cron_compute_kpis()
        self.KPI.cron_compute_kpis()
        warning = self._draft_late_warnings()
        self.assertEqual(len(warning), 1)
        self.assertEqual(warning.state, "draft")
        self.assertEqual(self.vendor.vendor_state, "approved")

    def test_backfill_gives_open_orders_a_deadline(self):
        migration = _migration("post-A7-ship-by-backfill")
        open_old = self._order()
        shipped = self._order()
        shipped.fulfillment_group_ids.write({"tracking_ref": "BF-1"})
        shipped.fulfillment_group_ids.action_ship()
        cancelled = self._order()
        cancelled.action_cancel()
        self.env.flush_all()
        old_date = fields.Datetime.now() - timedelta(days=10)
        self.env.cr.execute(
            "UPDATE leaders_vendor_order SET ship_by_date = NULL, is_late = FALSE, late_notified = FALSE, "
            "date_order = %s WHERE id IN %s", [old_date, tuple((open_old | shipped | cancelled).ids)])
        migration.backfill_ship_by(self.env.cr)
        self.env.invalidate_all()
        (open_old | shipped | cancelled).invalidate_recordset()
        self.assertEqual(open_old.ship_by_date, old_date + timedelta(days=2))
        self.assertTrue(open_old.is_late)
        self.assertTrue(open_old.late_notified, "old late orders must not be announced later")
        self.assertFalse(shipped.ship_by_date)
        self.assertFalse(cancelled.ship_by_date)

    def test_notification_defaults_migration_is_idempotent(self):
        migration = _migration("pre-A7-notification-defaults")
        self.env.cr.execute(
            "DELETE FROM ir_config_parameter WHERE key = 'leaders_marketplace.notify_vendor_bank_change_approved'")
        self.env.cr.execute(
            "DELETE FROM ir_model_data WHERE module = 'leaders_marketplace' "
            "AND name = 'param_notify_vendor_bank_change_approved'")
        migration.migrate(self.env.cr, "18.0.4.0.53")
        migration.migrate(self.env.cr, "18.0.4.0.53")
        self.env.cr.execute(
            "SELECT value FROM ir_config_parameter WHERE key = 'leaders_marketplace.notify_vendor_bank_change_approved'")
        self.assertEqual(self.env.cr.fetchall(), [("False",)])


@tagged("post_install", "-at_install")
class TestShipSlaPortal(HttpCase, ShipSlaCommon):

    def test_portal_shows_ship_by_column_and_late_badge(self):
        order = self._order()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/orders")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Ship by", page.text)
        self.assertNotIn("lm-badge--danger ms-1", page.text)
        self._make_stale_late(order)
        self.env["leaders.vendor.order"].cron_late_shipments()
        page = self.url_open("/my/vendor/orders")
        self.assertIn("lm-badge--danger ms-1", page.text)
        detail = self.url_open("/my/vendor/orders/%s" % order.id)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Ship by:", detail.text)
        self.assertIn("lm-badge--danger ms-1", detail.text)
