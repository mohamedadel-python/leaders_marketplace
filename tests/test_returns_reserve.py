"""The returns reserve, off by default.

Historically the reserve record was written but nothing was withheld and it was
never released. Switched on, the tier percentage is actually held out of the
wallet and paid back once the return window closes with no open return.
"""
from datetime import timedelta

from odoo import Command, fields
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD


class ReserveCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wallet = cls.env["leaders.wallet.transaction"]
        cls.Reserve = cls.env["leaders.vendor.reserve"]
        # A tier that reserves 10% and releases escrow as soon as it ships,
        # so the earning is available immediately and the maths is readable.
        cls.tier = cls.env["leaders.vendor.tier"].create({
            "name": "Reserve Tier", "code": "reserve10", "sequence": 15,
            "commission_rate": 0.0, "reserve_percent": 10.0,
            "escrow_release_policy": "on_ship", "return_policy": "reserve_first",
        })
        cls.vendor.vendor_tier_id = cls.tier
        # The shared fixture seeds 5 units; these tests ship 10 at a time.
        cls.env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.fbm_product, cls.vendor.vendor_location_id, cls.vendor, 95)

    def _enable(self, enabled=True):
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.enforce_returns_reserve", enabled)

    def _earn(self, qty=10):
        """Ship and deliver an FBM order worth qty x 100."""
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({
                "product_id": self.fbm_product.id, "product_uom_qty": qty})],
        })
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "RES-TRK"
        group.action_ship()
        group.action_delivered()
        # Earnings (and the reserve hold that shares their date) are created on
        # hold; the daily cron is what makes them available.
        self.Wallet.cron_release_holds()
        return vendor_order

    def _wallet(self, vendor_order, tx_type):
        return self.Wallet.search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", tx_type)])

    def _available_balance(self):
        return sum(self.Wallet.search([
            ("vendor_id", "=", self.vendor.id), ("state", "=", "available"),
        ]).mapped("amount"))

    def _age_the_shipment(self, vendor_order, days=60):
        vendor_order.date_shipped = fields.Datetime.now() - timedelta(days=days)


@tagged("post_install", "-at_install")
class TestReserveDisabled(ReserveCommon):
    """MUST NOT CHANGE: the default is exactly today's behaviour."""

    def test_the_setting_is_off_by_default(self):
        self.assertFalse(self.Reserve._leaders_reserve_enforced())

    def test_nothing_is_withheld(self):
        vendor_order = self._earn()
        earning = self._wallet(vendor_order, "earning")
        self.assertEqual(earning.amount, 1000.0)
        self.assertFalse(self._wallet(vendor_order, "reserve"))
        self.assertEqual(self._available_balance(), 1000.0)

    def test_the_reserve_record_is_still_written(self):
        vendor_order = self._earn()
        reserve = self.Reserve.search([("vendor_order_id", "=", vendor_order.id)])
        self.assertEqual(len(reserve), 1)
        self.assertEqual(reserve.amount, 100.0)
        self.assertEqual(reserve.state, "held")
        self.assertFalse(reserve.transaction_hold_id)

    def test_the_cron_does_nothing(self):
        vendor_order = self._earn()
        self._age_the_shipment(vendor_order)
        self.assertFalse(self.Reserve.cron_release_reserves())
        reserve = self.Reserve.search([("vendor_order_id", "=", vendor_order.id)])
        self.assertEqual(reserve.state, "held")
        self.assertEqual(self._available_balance(), 1000.0)


@tagged("post_install", "-at_install")
class TestReserveEnabled(ReserveCommon):

    def setUp(self):
        super().setUp()
        self._enable()

    def test_the_reserve_is_withheld_from_the_wallet(self):
        vendor_order = self._earn()
        earning = self._wallet(vendor_order, "earning")
        hold = self._wallet(vendor_order, "reserve")
        self.assertEqual(earning.amount, 1000.0, "the earning itself is untouched")
        self.assertEqual(len(hold), 1)
        self.assertEqual(hold.amount, -100.0)
        self.assertEqual(hold.available_date, earning.available_date)
        self.assertEqual(self._available_balance(), 900.0)

    def test_the_hold_is_linked_both_ways(self):
        vendor_order = self._earn()
        reserve = self.Reserve.search([("vendor_order_id", "=", vendor_order.id)])
        hold = self._wallet(vendor_order, "reserve")
        self.assertEqual(reserve.transaction_hold_id, hold)
        self.assertEqual(hold.reserve_id, reserve)

    def test_the_cron_waits_for_the_window(self):
        vendor_order = self._earn()
        self.Reserve.cron_release_reserves()      # shipped just now
        reserve = self.Reserve.search([("vendor_order_id", "=", vendor_order.id)])
        self.assertEqual(reserve.state, "held")
        self.assertEqual(self._available_balance(), 900.0)

    def test_the_cron_pays_the_reserve_back(self):
        vendor_order = self._earn()
        self._age_the_shipment(vendor_order)

        self.Reserve.cron_release_reserves()

        reserve = self.Reserve.search([("vendor_order_id", "=", vendor_order.id)])
        self.assertEqual(reserve.state, "released")
        self.assertEqual(reserve.transaction_release_id.amount, 100.0)
        self.assertEqual(self._available_balance(), 1000.0)

    def test_a_return_before_release_is_paid_only_for_what_is_left(self):
        """The worked example: reserve 100, return eats 60, release pays 40."""
        vendor_order = self._earn()
        line = vendor_order.line_ids.filtered(lambda l: not l.is_delivery)[:1]
        ret = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": line.id, "quantity": 0.6})],
        })
        ret.action_vendor_approve()
        ret.action_receive()
        reserve = self.Reserve.search([("vendor_order_id", "=", vendor_order.id)])
        self.assertEqual(reserve.consumed_amount, 60.0)

        self._age_the_shipment(vendor_order)
        self.Reserve.cron_release_reserves()

        self.assertEqual(reserve.state, "released")
        self.assertEqual(reserve.transaction_release_id.amount, 40.0)

    def test_an_open_return_blocks_the_release(self):
        vendor_order = self._earn()
        line = vendor_order.line_ids.filtered(lambda l: not l.is_delivery)[:1]
        self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Still deciding",
            "line_ids": [Command.create({"vendor_line_id": line.id, "quantity": 1})],
        })
        self._age_the_shipment(vendor_order)

        self.Reserve.cron_release_reserves()

        reserve = self.Reserve.search([("vendor_order_id", "=", vendor_order.id)])
        self.assertEqual(reserve.state, "held", "released while a return was open")
        self.assertEqual(self._available_balance(), 900.0)

    def test_a_fully_consumed_reserve_pays_nothing_back(self):
        vendor_order = self._earn()
        line = vendor_order.line_ids.filtered(lambda l: not l.is_delivery)[:1]
        ret = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "All of it",
            "line_ids": [Command.create({"vendor_line_id": line.id, "quantity": 10})],
        })
        ret.action_vendor_approve()
        ret.action_receive()
        self._age_the_shipment(vendor_order)

        self.Reserve.cron_release_reserves()

        reserve = self.Reserve.search([("vendor_order_id", "=", vendor_order.id)])
        # Eaten entirely by the return: it is closed as "consumed", not
        # "released", and there is nothing left to pay back.
        self.assertEqual(reserve.state, "consumed")
        self.assertFalse(reserve.transaction_release_id)

    def test_running_the_cron_twice_releases_once(self):
        vendor_order = self._earn()
        self._age_the_shipment(vendor_order)
        self.Reserve.cron_release_reserves()
        self.Reserve.cron_release_reserves()
        self.assertEqual(len(self._wallet(vendor_order, "reserve").filtered(
            lambda t: t.amount > 0)), 1)

    def test_the_settlement_nets_the_reserve_out(self):
        """Reserve transactions are ordinary signed transactions."""
        vendor_order = self._earn()
        today = fields.Date.today()
        self.Wallet.search([
            ("vendor_id", "=", self.vendor.id), ("state", "=", "available"),
        ]).write({"available_date": today})
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": today, "period_to": today,
            "currency_id": self.company.currency_id.id,
        })
        settlement.action_collect_transactions()
        self.assertEqual(settlement.net_amount, 900.0)

    def test_the_release_date_is_shown_from_the_shipment(self):
        vendor_order = self._earn()
        reserve = self.Reserve.search([("vendor_order_id", "=", vendor_order.id)])
        expected = fields.Date.to_date(vendor_order.date_shipped) + timedelta(
            days=self.Reserve._leaders_reserve_hold_days())
        self.assertEqual(reserve.release_date, expected)


@tagged("post_install", "-at_install")
class TestReservePortal(HttpCase, ReserveCommon):

    def test_the_finance_page_shows_the_reserve_when_enabled(self):
        self._enable()
        self._earn()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/finance")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Returns Reserve", page.text)
        self.assertIn("Released on", page.text)

    def test_the_finance_page_says_nothing_when_disabled(self):
        self._earn()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/finance")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Released on", page.text)
