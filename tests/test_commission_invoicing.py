"""Optional platform invoice to the vendor for commission and fees."""
from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD
from .test_vendor_split import MarketplaceVendorCommon


class InvoicingCommon(MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

    def _setup_accounting(self):
        self._configure_accounts()
        fee_income = self.env["account.account"].create({
            "name": "Fee Income", "code": "LMFEE1", "account_type": "income"})
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.fee_income_account_id", fee_income.id)
        self.fee_income = fee_income
        self.commission_income = self.env["account.account"].browse(int(
            self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.commission_income_account_id")))
        self.payable = self.env["account.account"].browse(int(
            self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.vendor_payable_account_id")))
        self.vat = self.env["account.tax"].create({
            "name": "LM VAT 14", "amount": 14.0, "amount_type": "percent", "type_tax_use": "sale",
            "company_id": self.company.id})
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.commission_tax_ids", str(self.vat.id))

    def _mode(self, mode):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.commission_invoicing", mode)

    def _settlement(self):
        """One delivered FBM order (100), a 10% plan (-10) and a penalty (-5), settled."""
        plan = self.env["leaders.commission.rule"].create({
            "name": "Invoice plan", "scope": "vendor", "vendor_id": self.vendor.id,
            "type": "achieve", "plan_effect": "deduction",
            "achieve_type": "amount_sold", "achieve_rate": 0.10})
        plan.action_approve()
        order = self._order([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        group = vendor_order.fulfillment_group_ids
        group.tracking_ref = "INV-TRK"
        group.action_ship()
        group.action_delivered()
        self.env["leaders.wallet.transaction"].search([
            ("vendor_order_id", "=", vendor_order.id), ("type", "=", "earning")], limit=1).write(
            {"state": "available", "available_date": fields.Date.today()})
        self.penalty = self.env["leaders.wallet.transaction"].create({
            "vendor_id": self.vendor.id, "type": "penalty", "amount": -5.0,
            "currency_id": self.company.currency_id.id, "state": "available",
            "available_date": fields.Date.today(), "reference": "Late shipping"})
        settlement = self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": fields.Date.today(),
            "period_to": fields.Date.today(), "currency_id": self.company.currency_id.id})
        settlement.action_collect_transactions()
        return settlement

    def _payable_balance(self):
        lines = self.env["account.move.line"].search([
            ("account_id", "=", self.payable.id), ("parent_state", "=", "posted"),
            ("partner_id", "=", self.vendor.commercial_partner_id.id)])
        return self.company.currency_id.round(sum(lines.mapped("balance")))


@tagged("post_install", "-at_install")
class TestCommissionInvoicing(InvoicingCommon):

    def test_journal_mode_is_todays_behaviour(self):
        self._setup_accounting()
        settlement = self._settlement()
        settlement.action_approve()
        self.assertFalse(settlement.commission_invoice_id)
        move = settlement.commission_move_id
        self.assertEqual(move.state, "posted")
        self.assertEqual(sum(move.line_ids.filtered(lambda l: l.account_id == self.commission_income).mapped("credit")), 10.0)
        self.assertTrue(self.penalty.move_id, "the penalty is booked by its own entry, as before")
        self.assertFalse(settlement.transaction_ids.filtered(lambda t: t.type == "fee_tax"))
        settlement.action_mark_paid()
        self.assertEqual(self._payable_balance(), 0.0)

    def test_invoice_mode_issues_one_taxed_invoice_and_no_commission_entry(self):
        self._setup_accounting()
        self._mode("invoice")
        settlement = self._settlement()
        self.assertFalse(self.penalty.move_id, "the invoice bills the penalty; no separate entry")
        settlement.action_approve()

        invoice = settlement.commission_invoice_id
        self.assertEqual(invoice.move_type, "out_invoice")
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.partner_id, self.vendor.commercial_partner_id)
        self.assertEqual(invoice.amount_untaxed, 15.0)
        self.assertAlmostEqual(invoice.amount_tax, 2.1)
        by_account = {l.account_id: l.price_subtotal for l in invoice.invoice_line_ids}
        self.assertEqual(by_account[self.commission_income], 10.0)
        self.assertEqual(by_account[self.fee_income], 5.0)
        offset = settlement.commission_invoice_offset_move_id
        self.assertEqual(offset.state, "posted")
        self.assertAlmostEqual(sum(offset.line_ids.filtered(
            lambda l: l.account_id == self.payable).mapped("debit")), 17.1)
        self.assertIn(invoice.payment_state, ("paid", "in_payment"), "nothing left to collect")
        self.assertFalse(settlement.commission_move_id, "no commission journal entry in this mode")

        tax_tx = settlement.transaction_ids.filtered(lambda t: t.type == "fee_tax")
        self.assertAlmostEqual(tax_tx.amount, -2.1)
        self.assertAlmostEqual(settlement.net_amount, 82.9)

        settlement.action_mark_paid()
        self.assertEqual(self._payable_balance(), 0.0)

    def test_approving_twice_issues_one_invoice(self):
        self._setup_accounting()
        self._mode("invoice")
        settlement = self._settlement()
        settlement.action_approve()
        first = settlement.commission_invoice_id
        settlement.action_approve()
        self.assertEqual(settlement.commission_invoice_id, first)
        self.assertEqual(len(settlement.transaction_ids.filtered(lambda t: t.type == "fee_tax")), 1)

    def test_cancelling_a_settlement_credits_its_invoice(self):
        self._setup_accounting()
        self._mode("invoice")
        settlement = self._settlement()
        settlement.action_approve()
        invoice = settlement.commission_invoice_id

        settlement.action_cancel()

        self.assertEqual(settlement.state, "cancelled")
        credit_note = self.env["account.move"].search([("reversed_entry_id", "=", invoice.id)])
        self.assertEqual(credit_note.move_type, "out_refund")
        self.assertEqual(credit_note.state, "posted")
        self.assertAlmostEqual(credit_note.amount_total, invoice.amount_total)
        lines = self.env["account.move.line"].search([
            ("account_id", "=", self.vendor.commercial_partner_id.property_account_receivable_id.id),
            ("parent_state", "=", "posted"), ("partner_id", "=", self.vendor.commercial_partner_id.id)])
        self.assertEqual(self.company.currency_id.round(sum(lines.mapped("balance"))), 0.0)
        # The netting is undone: the vendor is owed the full earning again, the wallet nets to it.
        self.assertEqual(self._payable_balance(), -100.0)   # balance = debit - credit: 100 owed
        taxes = settlement.transaction_ids.filtered(lambda t: t.type == "fee_tax")
        self.assertEqual(round(sum(taxes.mapped("amount")), 2), 0.0)
        # The penalty went back to the pool, to be billed by the next settlement.
        self.assertFalse(self.penalty.settlement_id)

    def test_a_paid_settlement_still_cannot_be_cancelled(self):
        self._setup_accounting()
        self._mode("invoice")
        settlement = self._settlement()
        settlement.action_approve()
        settlement.action_mark_paid()
        with self.assertRaises(UserError):
            settlement.action_cancel()

    def test_without_configured_accounts_it_skips_with_a_message(self):
        self._mode("invoice")
        settlement = self._settlement()
        settlement.action_approve()          # must not raise
        self.assertFalse(settlement.commission_invoice_id)
        self.assertTrue(any("skipped the commission invoice" in b for b in settlement.message_ids.mapped("body")))

    def test_the_einvoice_hook_exists(self):
        self.assertTrue(callable(self.env["leaders.marketplace.accounting"]._leaders_prepare_einvoice))


@tagged("post_install", "-at_install")
class TestCommissionInvoicePortal(HttpCase, MarketplacePortalUserCommon, InvoicingCommon):

    def test_owner_downloads_the_invoice(self):
        self._setup_accounting()
        self._mode("invoice")
        settlement = self._settlement()
        settlement.action_approve()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/finance")
        self.assertIn("/my/vendor/finance/settlement/%s/invoice" % settlement.id, page.text)
        response = self.url_open("/my/vendor/finance/settlement/%s/invoice" % settlement.id)
        self.assertEqual(response.status_code, 200)
