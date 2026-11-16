"""COD cash actually received, and money actually given back on cancellation.

Two open ends in the ledger: a platform-delivered COD order opened a customer
receivable that nothing ever closed, and cancelling an order the customer had
already paid left their money as an unapplied credit with no refund prepared.
"""
from odoo import Command, fields
from odoo.tests import tagged

from .test_vendor_split import MarketplaceVendorCommon


class CodRefundCommon(MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.cod_enabled", "True")

    def _cash_journal(self):
        return self.env["account.journal"].create({
            "name": "COD Cash", "code": "LMCOD", "type": "cash",
        })

    def _set_cod_journal(self, journal):
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.cod_cash_journal_id", journal.id)

    def _order(self, product, qty, cod=True):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({
                "product_id": product.id, "product_uom_qty": qty})],
        })
        if cod:
            order.leaders_payment_method = "cod"
        order.action_confirm()
        return order.vendor_order_ids

    def _deliver(self, vendor_order, ref="COD-TRK"):
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = ref
        group.action_ship()
        group.action_delivered()
        return vendor_order

    def _open_receivable(self, vendor_order):
        """Residual on the collection receivable of this order."""
        move = vendor_order.collection_move_id
        if not move:
            return 0.0
        lines = move.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable")
        return sum(lines.mapped("amount_residual"))


@tagged("post_install", "-at_install")
class TestCodCashReceipt(CodRefundCommon):

    def test_fbl_cod_closes_the_customer_receivable(self):
        self._configure_accounts()
        journal = self._cash_journal()
        self._set_cod_journal(journal)

        vendor_order = self._deliver(self._order(self.fbl_product, 2))

        self.assertEqual(vendor_order.state, "completed")
        self.assertTrue(vendor_order.collection_move_id)
        payment = vendor_order.cod_payment_id
        self.assertTrue(payment, "no cash receipt was recorded")
        # Odoo 18 leaves a posted payment "in_process" until a bank statement
        # matches it; what matters here is that its entry is posted.
        self.assertEqual(payment.move_id.state, "posted")
        self.assertNotEqual(payment.state, "draft")
        self.assertEqual(payment.journal_id, journal)
        self.assertEqual(payment.amount, vendor_order.amount_total)
        self.assertEqual(payment.memo, "COD %s" % vendor_order.name)
        self.assertEqual(
            self.company.currency_id.round(self._open_receivable(vendor_order)), 0.0,
            "the customer receivable was left open")

    def test_without_a_journal_nothing_is_booked(self):
        """MUST NOT CHANGE: today's behaviour when the setting is empty."""
        self._configure_accounts()
        vendor_order = self._deliver(self._order(self.fbl_product, 2))
        self.assertFalse(vendor_order.cod_payment_id)
        self.assertGreater(self._open_receivable(vendor_order), 0.0)
        self.assertTrue(any(
            "No Cash on Delivery journal configured" in body
            for body in vendor_order.message_ids.mapped("body")))

    def test_fbm_cod_is_untouched(self):
        """An FBM vendor holds the cash themselves; nothing is collected by us."""
        self._configure_accounts()
        self._set_cod_journal(self._cash_journal())
        vendor_order = self._deliver(self._order(self.fbm_product, 2))
        self.assertFalse(vendor_order.collection_move_id)
        self.assertFalse(vendor_order.cod_payment_id)

    def test_an_online_order_gets_no_cod_receipt(self):
        self._configure_accounts()
        self._set_cod_journal(self._cash_journal())
        vendor_order = self._deliver(self._order(self.fbl_product, 2, cod=False))
        self.assertFalse(vendor_order.cod_payment_id)

    def test_completing_twice_books_the_cash_once(self):
        self._configure_accounts()
        self._set_cod_journal(self._cash_journal())
        vendor_order = self._deliver(self._order(self.fbl_product, 2))
        first = vendor_order.cod_payment_id
        vendor_order.action_mark_completed()
        self.assertEqual(vendor_order.cod_payment_id, first)

    def test_it_skips_gracefully_without_configured_accounts(self):
        self._set_cod_journal(self._cash_journal())
        vendor_order = self._deliver(self._order(self.fbl_product, 2))  # must not raise
        self.assertEqual(vendor_order.state, "completed")
        self.assertFalse(vendor_order.cod_payment_id)


@tagged("post_install", "-at_install")
class TestCancellationRefund(CodRefundCommon):

    def _paid_online_order(self):
        """An online order whose collection receivable has been settled."""
        self._configure_accounts()
        vendor_order = self._order(self.fbm_product, 2, cod=False)
        self.assertTrue(vendor_order.collection_move_id)
        journal = self.env["account.journal"].search(
            [("type", "=", "bank"), ("company_id", "=", self.company.id)], limit=1)
        payment = self.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer",
            "partner_id": self.customer.commercial_partner_id.id,
            "amount": vendor_order.amount_total,
            "currency_id": vendor_order.currency_id.id,
            "journal_id": journal.id,
        })
        payment.action_post()
        receivable = vendor_order.collection_move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
        pay_lines = payment.move_id.line_ids.filtered(
            lambda l: l.account_id in receivable.account_id and not l.reconciled)
        (receivable + pay_lines).reconcile()
        self.assertEqual(
            self.company.currency_id.round(self._open_receivable(vendor_order)), 0.0)
        return vendor_order

    def _open_customer_receivable_total(self):
        accounts = self.env["leaders.marketplace.accounting"]._get_accounts()
        lines = self.env["account.move.line"].search([
            ("partner_id", "=", self.customer.commercial_partner_id.id),
            ("account_id.account_type", "=", "asset_receivable"),
            ("parent_state", "=", "posted"),
            ("reconciled", "=", False),
        ])
        return sum(lines.mapped("amount_residual"))

    def test_cancelling_a_paid_order_prepares_a_draft_refund(self):
        vendor_order = self._paid_online_order()

        vendor_order.action_cancel()

        payment = vendor_order.refund_payment_id
        self.assertTrue(payment, "no refund was prepared")
        self.assertEqual(payment.state, "draft", "the refund must not post itself")
        self.assertEqual(payment.payment_type, "outbound")
        self.assertEqual(payment.amount, 200.0)
        self.assertEqual(payment.leaders_cancelled_order_id, vendor_order)
        self.assertTrue(any("refund payment" in body
                            for body in vendor_order.message_ids.mapped("body")))

    def test_posting_the_refund_leaves_no_open_receivable(self):
        vendor_order = self._paid_online_order()
        vendor_order.action_cancel()

        vendor_order.refund_payment_id.action_post()

        self.assertEqual(
            self.company.currency_id.round(self._open_customer_receivable_total()), 0.0,
            "the customer's credit was never applied")

    def test_an_unpaid_order_prepares_no_refund(self):
        """Nothing was received, so there is nothing to give back."""
        self._configure_accounts()
        vendor_order = self._order(self.fbm_product, 2, cod=False)
        vendor_order.action_cancel()
        self.assertFalse(vendor_order.refund_payment_id)

    def test_a_cod_order_prepares_no_refund(self):
        """The customer never paid us: the cash never left their hands."""
        self._configure_accounts()
        vendor_order = self._order(self.fbm_product, 2)
        vendor_order.action_cancel()
        self.assertFalse(vendor_order.refund_payment_id)

    def test_cancelling_twice_prepares_one_refund(self):
        vendor_order = self._paid_online_order()
        vendor_order.action_cancel()
        first = vendor_order.refund_payment_id
        vendor_order.action_cancel()
        self.assertEqual(vendor_order.refund_payment_id, first)

    def test_it_skips_gracefully_without_configured_accounts(self):
        vendor_order = self._order(self.fbm_product, 2, cod=False)
        vendor_order.action_cancel()          # must not raise
        self.assertEqual(vendor_order.state, "cancelled")
        self.assertFalse(vendor_order.refund_payment_id)


@tagged("post_install", "-at_install")
class TestCodFinanceView(CodRefundCommon):

    def test_the_cod_follow_up_action_is_installed(self):
        action = self.env.ref("leaders_marketplace.action_cod_vendor_order")
        self.assertEqual(action.res_model, "leaders.vendor.order")
        self.assertIn("('payment_method', '=', 'cod')", action.domain)
        menu = self.env.ref("leaders_marketplace.menu_cod_orders")
        self.assertEqual(menu.action, action)
