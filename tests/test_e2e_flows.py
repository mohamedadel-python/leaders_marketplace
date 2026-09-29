"""End-to-end flows with money reconciliation.

Every scenario runs the real path (checkout payment, stock, portal, settlement)
and ends with ledger assertions: escrow and vendor payable back to zero, the
customer's receivable closed, commission income equal to the commission
transactions.
"""
import os
import re
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD
from .test_vendor_split import MarketplaceVendorCommon


class E2ECommon(HttpCase, MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.Accounting = env["leaders.marketplace.accounting"]
        cls.Wallet = env["leaders.wallet.transaction"]
        set_param = env["ir.config_parameter"].sudo().set_param

        # ---- company accounts, journals and settings
        MarketplaceVendorCommon._configure_accounts(cls)
        cls.fee_income = env["account.account"].create({
            "name": "E2E Fee Income", "code": "LME2E1", "account_type": "income"})
        set_param("leaders_marketplace.fee_income_account_id", cls.fee_income.id)
        cls.cash_journal = env["account.journal"].create({
            "name": "E2E COD Cash", "code": "LME2C", "type": "cash"})
        set_param("leaders_marketplace.cod_cash_journal_id", cls.cash_journal.id)
        set_param("leaders_marketplace.cod_enabled", "True")
        set_param("sale.automatic_invoice", True)
        accounts = cls.Accounting._get_accounts()
        cls.commission_income = env["account.account"].browse(
            int(env["ir.config_parameter"].sudo().get_param("leaders_marketplace.commission_income_account_id")))
        cls.receivable = cls.customer.property_account_receivable_id

        # ---- payment provider (demo, test mode)
        # Official runs install payment_demo. Only with LM_ALLOW_SKIP_PAYMENT=1 is the
        # customer's payment booked by hand, as the transaction flow would post it.
        cls.provider = env["payment.provider"].search([("code", "=", "demo")], limit=1)
        if cls.provider and cls.provider.journal_id:
            cls.provider.state = "test"
        elif os.environ.get("LM_ALLOW_SKIP_PAYMENT") == "1":
            cls.provider = env["payment.provider"]
        else:
            raise AssertionError(
                "The demo payment provider (payment_demo) is not installed; run with "
                "-i leaders_marketplace,payment_demo, or LM_ALLOW_SKIP_PAYMENT=1 to book the "
                "payment by hand.")

        # ---- vendor A (Bronze, FBM) is the base fixture's vendor; vendor B (Gold, FBL)
        cls.vendor_a = cls.vendor
        cls.vendor_a.vendor_tier_id = env.ref("leaders_marketplace.vendor_tier_bronze")
        env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.fbm_product, cls.vendor_a.vendor_location_id, cls.vendor_a, 50)
        cls.vendor_b = env["res.partner"].create({
            "name": "E2E Vendor B", "is_marketplace_vendor": True, "vendor_state": "approved",
            "vendor_tier_id": env.ref("leaders_marketplace.vendor_tier_gold").id})
        cls.vendor_b._marketplace_ensure_vendor_stock()
        cls.b_product = env["product.product"].create({
            "name": "E2E FBL Product", "type": "consu", "is_storable": True,
            "list_price": 60.0, "taxes_id": [Command.clear()]})
        cls.b_offer = env["leaders.marketplace.offer"].create({
            "product_tmpl_id": cls.b_product.product_tmpl_id.id, "product_id": cls.b_product.id,
            "vendor_id": cls.vendor_b.id, "seller_sku": "E2E-B-FBL", "price": 60.0,
            "fulfillment_type": "fbl",
            "stock_location_id": cls.vendor_b._marketplace_stock_location("fbl").id})
        env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.b_product, cls.vendor_b.vendor_fbl_location_id, cls.vendor_b, 50)
        cls.b_offer.action_approve()
        contact = env["res.partner"].create({
            "name": "E2E Owner B", "email": "e2e.owner.b@example.com",
            "parent_id": cls.vendor_b.id, "type": "contact"})
        cls.owner_b = env["res.users"].with_context(no_reset_password=True).create({
            "name": "E2E Owner B", "login": "e2e.owner.b@example.com", "email": "e2e.owner.b@example.com",
            "password": PORTAL_PASSWORD, "partner_id": contact.id,
            "groups_id": [Command.set([env.ref("leaders_marketplace.group_vendor_owner").id])]})

        # ---- 10% commission plan for each vendor
        for vendor in (cls.vendor_a, cls.vendor_b):
            env["leaders.commission.rule"].create({
                "name": "E2E 10%% %s" % vendor.name, "scope": "vendor", "vendor_id": vendor.id,
                "type": "achieve", "plan_effect": "deduction",
                "achieve_type": "amount_sold", "achieve_rate": 0.10}).action_approve()

        # ---- zone carrier with prices
        cls.cairo = env.ref("base.state_eg_c")
        cls.carrier = env.ref("leaders_marketplace.marketplace_zone_delivery_carrier")
        for vendor, price in ((cls.vendor_a, 30.0), (cls.vendor_b, 15.0)):
            env["leaders.vendor.delivery.zone"].create({
                "vendor_id": vendor.id, "state_id": cls.cairo.id, "price": price})
        cls.address = env["res.partner"].create({
            "name": "E2E Cairo Address", "state_id": cls.cairo.id, "country_id": env.ref("base.eg").id})

    # ---------------------------------------------------------------- helpers
    def _order(self, products, cod=False, carrier=False):
        lines = [Command.create({"product_id": p.id, "product_uom_qty": q}) for p, q in products]
        vals = {"partner_id": self.customer.id, "order_line": lines}
        if carrier:
            lines.append(Command.create({
                "product_id": self.carrier.product_id.id, "is_delivery": True,
                "product_uom_qty": 1, "price_unit": 45.0}))
            vals.update({"partner_shipping_id": self.address.id, "carrier_id": self.carrier.id})
        order = self.env["sale.order"].create(vals)
        if cod:
            order.leaders_payment_method = "cod"
        return order

    def _pay(self, order):
        if not self.provider:
            return self._pay_by_hand(order)
        tx = self.env["payment.transaction"].create({
            "provider_id": self.provider.id, "operation": "online_redirect",
            "payment_method_id": self.provider.payment_method_ids[:1].id,
            "reference": "E2E-%s" % order.id, "amount": order.amount_total,
            "currency_id": order.currency_id.id, "partner_id": self.customer.id,
            "sale_order_ids": [Command.set(order.ids)]})
        tx._set_done()
        tx._post_process()
        return tx

    def _pay_by_hand(self, order):
        """Confirm the checkout and book the customer's online payment against everything they owe."""
        total = order.amount_total
        order.with_context(send_email=True).action_confirm()
        if order.state == "sale":
            order._create_invoices().action_post()
        journal = self.env["account.journal"].search([("type", "=", "bank")], limit=1)
        payment = self.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer",
            "partner_id": self.customer.id, "amount": total,
            "currency_id": order.currency_id.id, "journal_id": journal.id})
        payment.action_post()
        open_lines = self.env["account.move.line"].search([
            ("account_id", "=", self.receivable.id), ("partner_id", "=", self.customer.id),
            ("parent_state", "=", "posted"), ("reconciled", "=", False)])
        open_lines.reconcile()
        order.vendor_order_ids._update_invoice_payment_state()
        return payment

    def _ship_and_deliver(self, vendor_order, tag):
        for group in vendor_order.fulfillment_group_ids:
            group.tracking_ref = "E2E-%s-%s" % (tag, group.id)
            group.action_ship()
        for group in vendor_order.fulfillment_group_ids:
            if group.fulfillment_type == "fbm":
                # Task 05 "vendor" mode: the vendor's report is the delivery.
                group.action_report_delivery(reported_by=self.owner_user)
            else:
                group.action_delivered()
        self.assertEqual(vendor_order.state, "completed")

    def _receivable_balance(self):
        lines = self.env["account.move.line"].search([
            ("account_id", "=", self.receivable.id), ("parent_state", "=", "posted"),
            ("partner_id", "=", self.customer.commercial_partner_id.id)])
        return self.env.company.currency_id.round(sum(lines.mapped("balance")))

    def _commission_income_balance(self):
        lines = self.env["account.move.line"].search([
            ("account_id", "=", self.commission_income.id), ("parent_state", "=", "posted")])
        return self.env.company.currency_id.round(sum(lines.mapped("credit")) - sum(lines.mapped("debit")))

    def _request_settlement(self, user, vendor):
        self.authenticate(user.login, PORTAL_PASSWORD)
        today = fields.Date.today()
        self._post("/my/vendor/finance/settlement/request", "/my/vendor/finance",
                   period_from=str(today - timedelta(days=5)), period_to=str(today))
        settlement = self.env["leaders.settlement"].search(
            [("vendor_id", "=", vendor.id)], order="id desc", limit=1)
        self.assertTrue(settlement.transaction_ids, "the portal request collected nothing")
        return settlement

    def _escrow_by_orders(self, vendor):
        """Cross-check: the escrow balance rebuilt from the entries each order and return points at."""
        vendor = vendor.commercial_partner_id
        escrow = self.Accounting._get_accounts()["escrow"]
        orders = self.env["leaders.vendor.order"].search([("vendor_id", "=", vendor.id)])
        returns = self.env["leaders.vendor.return"].search([("vendor_id", "=", vendor.id)])
        moves = (orders.collection_move_id | orders.collection_reversal_move_id
                 | orders.earning_move_id | orders.cod_due_move_id | returns.refund_move_id)
        lines = moves.filtered(lambda m: m.state == "posted").line_ids.filtered(
            lambda l: l.account_id.id == escrow)
        return self.env.company.currency_id.round(sum(lines.mapped("credit")) - sum(lines.mapped("debit")))

    def _assert_settled(self, vendor):
        ledger = self.Accounting._leaders_check_ledger(vendor)
        self.assertEqual(ledger["escrow"], self._escrow_by_orders(vendor))
        self.assertEqual(ledger["escrow"], 0.0, "escrow of %s" % vendor.name)
        self.assertEqual(ledger["payable"], 0.0, "vendor payable of %s" % vendor.name)
        self.assertEqual(ledger["wallet"]["unsettled"], 0.0, "wallet of %s" % vendor.name)
        return ledger


@tagged("post_install", "-at_install")
class TestE2EFlows(E2ECommon):

    # 1. online, mixed cart ---------------------------------------------------
    def test_1_online_mixed_cart_to_paid_settlement(self):
        order = self._order([(self.own_product, 1), (self.fbm_product, 1), (self.b_product, 1)], carrier=True)
        self._pay(order)

        self.assertEqual(order.state, "sale")
        vendor_orders = order.vendor_order_ids
        self.assertEqual(vendor_orders.mapped("vendor_id"), self.vendor_a | self.vendor_b)
        for vendor_order in vendor_orders:
            receivable_lines = vendor_order.collection_move_id.line_ids.filtered(
                lambda l: l.account_id == self.receivable)
            self.assertTrue(receivable_lines.reconciled, "collection not matched with the payment")
        self.assertEqual(self._receivable_balance(), 0.0)

        for vendor_order in vendor_orders:
            self._ship_and_deliver(vendor_order, vendor_order.vendor_id.id)
        earnings = self.Wallet.search([("vendor_order_id", "in", vendor_orders.ids), ("type", "=", "earning")])
        self.assertEqual(len(earnings), 2)
        earnings.write({"available_date": fields.Date.today() - timedelta(days=1)})
        self.Wallet.cron_release_holds()
        self.assertEqual(set(earnings.mapped("state")), {"available"})

        settlements = self.env["leaders.settlement"]
        for user, vendor in ((self.owner_user, self.vendor_a), (self.owner_b, self.vendor_b)):
            settlements |= self._request_settlement(user, vendor)
        settlements.action_approve()
        for settlement in settlements:
            self.assertGreater(settlement.net_amount, 0)
            settlement.action_mark_paid()
            self.assertEqual(settlement.state, "paid")

        for vendor in (self.vendor_a, self.vendor_b):
            self._assert_settled(vendor)
        commissions = self.Wallet.search([("type", "=", "commission")])
        self.assertTrue(commissions)
        self.assertEqual(self._commission_income_balance(), -sum(commissions.mapped("amount")))
        self.assertEqual(self._receivable_balance(), 0.0)

    # 2. COD FBM ---------------------------------------------------------------
    def test_2_cod_fbm_vendor_debt_is_collected(self):
        vendor_order = self._order([(self.fbm_product, 1)], cod=True)
        vendor_order.action_confirm()
        vendor_order = vendor_order.vendor_order_ids
        self._ship_and_deliver(vendor_order, "cod-fbm")

        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self._post("/my/vendor/orders/%s/confirm-cod-payment" % vendor_order.id,
                   "/my/vendor/orders/%s" % vendor_order.id)
        self.assertTrue(vendor_order.cod_cash_confirmed)

        settlement = self._request_settlement(self.owner_user, self.vendor_a)
        settlement.action_approve()
        self.assertLess(settlement.net_amount, 0, "the vendor owes the commission")
        settlement.action_register_vendor_payment()
        self.assertEqual(settlement.state, "paid")
        self._assert_settled(self.vendor_a)
        self.assertEqual(self._receivable_balance(), 0.0)

    # 3. COD FBL ---------------------------------------------------------------
    def test_3_cod_fbl_cash_closes_the_receivable(self):
        order = self._order([(self.b_product, 1)], cod=True)
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        self.assertEqual(self._receivable_balance(), 0.0, "COD collection is only booked at delivery")
        self._ship_and_deliver(vendor_order, "cod-fbl")
        self.assertEqual(vendor_order.cod_payment_id.move_id.state, "posted")
        self.assertEqual(self._receivable_balance(), 0.0)

    # 4. return after release --------------------------------------------------
    def test_4_partial_return_after_release_is_refunded(self):
        order = self._order([(self.fbm_product, 3)])
        self._pay(order)
        vendor_order = order.vendor_order_ids
        self._ship_and_deliver(vendor_order, "ret")
        earning = self.Wallet.search([("vendor_order_id", "=", vendor_order.id), ("type", "=", "earning")])
        self.assertTrue(earning and earning.amount == 300.0)

        ret = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": vendor_order.line_ids[:1].id, "quantity": 1})]})
        ret.action_vendor_approve()
        ret.action_receive()
        self.assertEqual(ret.state, "refunded")
        self.assertEqual(ret.refund_amount, 100.0)
        self.assertEqual(ret.wallet_transaction_id.amount, -100.0, "the vendor's wallet is reduced")
        self.assertIn("/RINV/", ret.credit_note_id.name)
        self.assertEqual(ret.refund_payment_id.state, "draft")

        ret.refund_payment_id.action_post()
        self.assertEqual(ret.refund_payment_id.move_id.state, "posted")
        self.assertEqual(self._receivable_balance(), 0.0)
        ledger = self.Accounting._leaders_check_ledger(self.vendor_a)
        self.assertEqual(ledger["escrow"], 0.0)
        self.assertEqual(ledger["wallet"]["unsettled"], 200.0)     # 300 earned - 100 returned
        self.assertEqual(ledger["payable"], 200.0)

    # 5. cancel paid order -----------------------------------------------------
    def test_5_cancelling_a_paid_order_refunds_the_customer(self):
        order = self._order([(self.fbm_product, 2)])
        self._pay(order)
        vendor_order = order.vendor_order_ids
        vendor_order.action_cancel()
        self.assertEqual(vendor_order.state, "cancelled")
        self.assertEqual(vendor_order.refund_payment_id.state, "draft")
        vendor_order.refund_payment_id.action_post()
        self.assertEqual(self._receivable_balance(), 0.0)
        ledger = self.Accounting._leaders_check_ledger(self.vendor_a)
        self.assertEqual(ledger["escrow"], 0.0)
        self.assertEqual(ledger["payable"], 0.0)

    # 6. security smoke --------------------------------------------------------
    def test_6_vendor_rpc_writes_fail(self):
        order = self._order([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        state = vendor_order.state
        as_vendor = self.owner_user
        with self.assertRaises(AccessError):
            vendor_order.with_user(as_vendor).write({"state": "completed"})
        with self.assertRaises(AccessError):
            vendor_order.with_user(as_vendor).action_cancel()
        with self.assertRaises(AccessError):
            self.env["leaders.wallet.transaction"].with_user(as_vendor).create({
                "vendor_id": self.vendor_a.id, "type": "adjustment", "amount": 1000.0,
                "currency_id": self.env.company.currency_id.id})
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor_a.id, "period_from": fields.Date.today(),
            "period_to": fields.Date.today()})
        with self.assertRaises(AccessError):
            settlement.with_user(as_vendor).action_approve()
        with self.assertRaises(AccessError):
            self.fbm_offer.with_user(as_vendor).write({"price": 1.0})
        self.assertEqual(vendor_order.state, state)

    # the helper itself --------------------------------------------------------
    def test_ledger_check_reports_escrow_payable_and_wallet(self):
        order = self._order([(self.fbm_product, 1)])
        self._pay(order)
        vendor_order = order.vendor_order_ids
        ledger = self.Accounting._leaders_check_ledger(self.vendor_a)
        self.assertEqual(ledger["escrow"], 100.0)       # held until released
        self.assertEqual(ledger["escrow"], self._escrow_by_orders(self.vendor_a))
        self.assertEqual(ledger["payable"], 0.0)
        self._ship_and_deliver(vendor_order, "helper")
        ledger = self.Accounting._leaders_check_ledger(self.vendor_a)
        self.assertEqual(ledger["escrow"], 0.0)
        self.assertEqual(ledger["escrow"], self._escrow_by_orders(self.vendor_a))
        self.assertEqual(ledger["payable"], 100.0)
        self.assertEqual(ledger["wallet"]["unsettled"], 100.0)
        escrow_lines = self.env["account.move.line"].search([
            ("account_id", "=", self.Accounting._get_accounts()["escrow"]),
            ("move_id", "in", (vendor_order.collection_move_id | vendor_order.earning_move_id).ids)])
        self.assertTrue(escrow_lines)
        self.assertEqual(escrow_lines.partner_id, self.vendor_a, "every escrow line carries the vendor")
