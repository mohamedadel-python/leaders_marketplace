"""Vendor statement of account (B5): one model method feeding portal, PDF, xlsx and backend."""
import io
from urllib.parse import urlencode
from datetime import date

import openpyxl

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import PORTAL_PASSWORD, MarketplacePortalUserCommon


class StatementCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Tx = cls.env["leaders.wallet.transaction"]
        cls.other_vendor = cls.env["res.partner"].create({
            "name": "Other Statement Vendor", "is_marketplace_vendor": True, "vendor_state": "approved"})

    @classmethod
    def _tx(cls, ttype, amount, day, state="available", vendor=None, reference=None):
        """A wallet transaction created on ``day`` (create_date is set in SQL)."""
        vendor = vendor or cls.vendor
        tx = cls.Tx.create({
            "vendor_id": vendor.id, "type": ttype, "amount": amount, "state": state,
            "currency_id": cls.env.company.currency_id.id, "reference": reference or "REF-%s" % ttype,
        })
        cls.env.cr.execute("UPDATE leaders_wallet_transaction SET create_date = %s WHERE id = %s",
                           ("%s 10:00:00" % day, tx.id))
        tx.invalidate_recordset()
        return tx

    @classmethod
    def _seed(cls):
        cls._tx("earning", 1000.0, "2026-01-10", state="settled")
        cls._tx("commission", -100.0, "2026-01-10", state="settled")
        cls._tx("earning", 500.0, "2026-02-05", state="available", reference="ORD-A")
        cls._tx("penalty", -50.0, "2026-02-06")
        cls._tx("return", -200.0, "2026-02-20")
        cls._tx("adjustment", 25.0, "2026-02-28")
        cls._tx("cod_collected", 999.0, "2026-02-15")           # never part of the balance
        cls._tx("earning", 300.0, "2026-03-01", state="hold")
        cls._tx("earning", 7777.0, "2026-02-10", vendor=cls.other_vendor)


@tagged("post_install", "-at_install")
class TestVendorStatementModel(StatementCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._seed()

    def st(self, date_from, date_to, vendor=None):
        return (vendor or self.vendor)._leaders_vendor_statement(date_from, date_to)

    def test_opening_plus_movements_is_closing_across_periods(self):
        jan = self.st("2026-01-01", "2026-01-31")
        self.assertEqual((jan["opening"], jan["closing"]), (0.0, 900.0))
        feb = self.st("2026-02-01", "2026-02-28")
        self.assertEqual(feb["opening"], 900.0)
        self.assertEqual(feb["total_credit"], 525.0)
        self.assertEqual(feb["total_debit"], 250.0)
        self.assertEqual(feb["closing"], 900.0 + 525.0 - 250.0)
        self.assertEqual([l["balance"] for l in feb["lines"]], [1400.0, 1350.0, 1150.0, 1175.0])
        self.assertEqual(
            feb["opening"] + feb["total_credit"] - feb["total_debit"], feb["closing"])
        mar = self.st("2026-03-01", "2026-03-31")
        self.assertEqual(mar["opening"], feb["closing"])
        self.assertEqual(mar["closing"], 1475.0)
        # A period without movements keeps the balance.
        apr = self.st("2026-04-01", "2026-04-30")
        self.assertEqual((apr["opening"], apr["closing"], apr["lines"]), (1475.0, 1475.0, []))

    def test_types_and_cod_are_handled(self):
        feb = self.st("2026-02-01", "2026-02-28")
        types = [l["type"] for l in feb["lines"]]
        self.assertEqual(types, ["earning", "penalty", "return", "adjustment"])
        self.assertNotIn("cod_collected", types)
        line = feb["lines"][0]
        self.assertEqual((line["debit"], line["credit"], line["reference"]), (0.0, 500.0, "ORD-A"))
        self.assertEqual(line["date"], date(2026, 2, 5))
        self.assertTrue(line["type_label"])
        self.assertEqual(feb["lines"][1]["debit"], 50.0)

    def test_states_do_not_change_the_balance(self):
        full = self.st("2026-01-01", "2026-03-31")
        self.assertEqual(full["closing"], 1475.0)
        self.assertEqual(full["closing_by_state"], {"hold": 300.0, "available": 275.0, "settled": 900.0})
        self.assertEqual(sum(full["closing_by_state"].values()), full["closing"])

    def test_other_vendors_never_appear(self):
        full = self.st("2026-01-01", "2026-03-31")
        self.assertNotIn(7777.0, [l["credit"] for l in full["lines"]])
        other = self.st("2026-02-01", "2026-02-28", vendor=self.other_vendor)
        self.assertEqual(other["closing"], 7777.0)
        # A contact of the vendor resolves to the same commercial partner.
        contact = self.owner_user.partner_id
        self.assertEqual(self.st("2026-01-01", "2026-03-31", vendor=contact)["closing"], 1475.0)

    def test_closing_agrees_with_the_ledger_check_when_the_period_ends_today(self):
        self._configure_accounts()
        self._tx("earning", 40.0, fields.Date.today(), state="hold")
        self._tx("commission", -4.0, fields.Date.today(), state="available")
        wallet = self.env["leaders.marketplace.accounting"]._leaders_check_ledger(self.vendor)["wallet"]
        statement = self.st("2026-01-01", fields.Date.today())
        self.assertEqual(statement["closing"], wallet["hold"] + wallet["available"] + wallet["settled"])
        self.assertEqual(statement["closing"], 1511.0)
        self.assertEqual(statement["closing_by_state"]["hold"], wallet["hold"])
        self.assertEqual(statement["closing_by_state"]["available"], wallet["available"])
        self.assertEqual(statement["closing_by_state"]["settled"], wallet["settled"])

    def test_date_validation(self):
        with self.assertRaises(UserError):
            self.st("2026-02-10", "2026-02-01")
        with self.assertRaises(UserError):
            self.st("not-a-date", "2026-02-01")
        with self.assertRaises(UserError):
            self.st("2026-02-10", None)
        with self.assertRaises(UserError):
            self.st("2025-01-01", "2026-03-01")          # more than a year
        self.assertTrue(self.st("2025-03-02", "2026-03-01"))  # exactly 365 days is fine

    def test_default_period_is_the_current_month(self):
        statement = self.vendor._leaders_vendor_statement()
        today = fields.Date.today()
        self.assertEqual(statement["date_from"], today.replace(day=1))
        self.assertEqual(statement["date_to"].month, today.month)
        self.assertEqual((statement["date_to"].replace(day=28) - statement["date_to"]).days <= 0, True)

    def test_settlements_of_the_period_are_listed_with_status_and_account(self):
        bank = self.env["res.partner.bank"].create({"partner_id": self.vendor.id, "acc_number": "EG12345678"})
        self.vendor.vendor_payout_bank_id = bank
        Settlement = self.env["leaders.settlement"]
        inside = Settlement.create({
            "vendor_id": self.vendor.id, "period_from": date(2026, 2, 1), "period_to": date(2026, 2, 28)})
        Settlement.create({
            "vendor_id": self.vendor.id, "period_from": date(2025, 11, 1), "period_to": date(2025, 11, 30)})
        Settlement.create({
            "vendor_id": self.other_vendor.id, "period_from": date(2026, 2, 1), "period_to": date(2026, 2, 28)})
        feb = self.st("2026-02-01", "2026-02-28")
        self.assertEqual([s["name"] for s in feb["settlements"]], [inside.name])
        row = feb["settlements"][0]
        self.assertEqual(row["state"], "draft")
        self.assertTrue(row["state_label"])
        self.assertEqual(row["payout_account"], "****5678")

    def test_xlsx_is_a_readable_workbook_with_header_and_totals(self):
        statement = self.st("2026-02-01", "2026-02-28")
        data = self.env["leaders.vendor.statement"]._xlsx(statement)
        book = openpyxl.load_workbook(io.BytesIO(data))
        self.assertEqual(len(book.sheetnames), 2)
        sheet = book[book.sheetnames[0]]
        rows = [[c for c in row] for row in sheet.iter_rows(values_only=True)]
        flat = [v for row in rows for v in row if v is not None]
        self.assertIn(statement["vendor_name"], flat)
        self.assertIn("ORD-A", flat)
        opening = next(r for r in rows if r[2] == "Opening balance")
        closing = next(r for r in rows if r[2] == "Closing balance")
        totals = next(r for r in rows if r[2] == "Total movements")
        self.assertEqual(opening[6], 900.0)
        self.assertEqual(closing[6], 1175.0)
        self.assertEqual((totals[4], totals[5]), (250.0, 525.0))
        self.assertEqual(sum(1 for r in rows if r[2] in ("REF-penalty", "REF-return", "REF-adjustment", "ORD-A")), 4)
        self.assertTrue(self.env["leaders.vendor.statement"]._filename(statement, "xlsx").endswith(".xlsx"))

    def test_pdf_report_renders(self):
        report = self.env["ir.actions.report"]
        content, kind = report._render_qweb_pdf(
            "leaders_marketplace.action_report_vendor_statement", self.vendor.ids,
            data={"date_from": "2026-02-01", "date_to": "2026-02-28"})
        text = content.decode() if isinstance(content, bytes) else content
        if kind == "html":       # no wkhtmltopdf in the test environment
            self.assertIn("Vendor Statement of Account", text)
            self.assertIn("ORD-A", text)
            self.assertIn("Closing balance", text)
            self.assertNotIn("7777", text)
        else:
            self.assertTrue(content.startswith(b"%PDF"))


@tagged("post_install", "-at_install")
class TestVendorStatementWizard(StatementCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._seed()
        cls.manager = cls.env["res.users"].create({
            "name": "Statement Manager", "login": "st.manager@example.com",
            "groups_id": [(6, 0, [cls.env.ref("base.group_user").id,
                                  cls.env.ref("leaders_marketplace.group_marketplace_manager").id])]})

    def _wizard(self, user=None, **vals):
        values = {"vendor_id": self.vendor.id, "date_from": "2026-02-01", "date_to": "2026-02-28"}
        values.update(vals)
        return self.env["leaders.vendor.statement.wizard"].with_user(user or self.manager).create(values)

    def test_default_vendor_from_the_partner_form(self):
        wizard = self.env["leaders.vendor.statement.wizard"].with_user(self.manager).with_context(
            active_model="res.partner", active_id=self.owner_user.partner_id.id).default_get(["vendor_id"])
        self.assertEqual(wizard["vendor_id"], self.vendor.id)

    def test_print_returns_the_report_action_with_dates(self):
        action = self._wizard().action_print()
        self.assertEqual(action["type"], "ir.actions.report")
        self.assertEqual(action["report_name"], "leaders_marketplace.report_vendor_statement")
        self.assertEqual(action["data"], {"date_from": "2026-02-01", "date_to": "2026-02-28"})
        self.assertEqual(action["context"]["active_ids"], [self.vendor.id])

    def test_xlsx_action_points_to_the_download_route(self):
        action = self._wizard().action_xlsx()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertIn("date_from=2026-02-01", action["url"])

    def test_bad_dates_and_non_vendors_are_refused(self):
        with self.assertRaises(UserError):
            self._wizard(date_from="2026-03-01").action_print()
        with self.assertRaises(UserError):
            self._wizard(vendor_id=self.customer.id).action_print()

    def test_staff_without_the_groups_have_no_access(self):
        with self.assertRaises(AccessError):
            self._wizard(user=self.owner_user)


@tagged("post_install", "-at_install")
class TestVendorStatementPortal(HttpCase, StatementCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._seed()
        cls.env.cr.flush()

    def _get(self, path, **params):
        return self.url_open("%s?%s" % (path, urlencode(params)) if params else path)

    def test_owner_sees_the_page_with_balances_and_download_buttons(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self._get("/my/vendor/finance/statement", date_from="2026-02-01", date_to="2026-02-28")
        self.assertEqual(page.status_code, 200)
        for text in ("Statement of account", "ORD-A", "Opening balance", "Closing balance",
                     "Download PDF", "Download XLSX",
                     "/my/vendor/finance/statement.pdf?date_from=2026-02-01&amp;date_to=2026-02-28",
                     "/my/vendor/finance/statement.xlsx?date_from=2026-02-01"):
            self.assertIn(text, page.text)
        self.assertNotIn("7,777", page.text)
        self.assertNotIn("7777", page.text)
        self.assertNotIn("999.00", page.text)      # cod_collected is not in the statement

    def test_default_period_and_bad_dates_render_a_message(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self.assertEqual(self._get("/my/vendor/finance/statement").status_code, 200)
        page = self._get("/my/vendor/finance/statement", date_from="2026-03-01", date_to="2026-02-01")
        self.assertEqual(page.status_code, 200)
        self.assertIn("start date cannot be after", page.text)
        page = self._get("/my/vendor/finance/statement", date_from="2020-01-01", date_to="2026-02-01")
        self.assertIn("more than one year", page.text)

    def test_xlsx_and_pdf_downloads(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        response = self._get("/my/vendor/finance/statement.xlsx", date_from="2026-02-01", date_to="2026-02-28")
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response.headers["Content-Type"])
        book = openpyxl.load_workbook(io.BytesIO(response.content))
        flat = [v for row in book[book.sheetnames[0]].iter_rows(values_only=True) for v in row if v is not None]
        self.assertIn(1175.0, flat)
        self.assertNotIn(7777.0, flat)
        response = self._get("/my/vendor/finance/statement.pdf", date_from="2026-02-01", date_to="2026-02-28")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ORD-A", response.text if not response.content.startswith(b"%PDF") else "ORD-A")
        bad = self._get("/my/vendor/finance/statement.xlsx", date_from="2026-03-01", date_to="2026-02-01")
        self.assertEqual(bad.status_code, 400)
        bad = self._get("/my/vendor/finance/statement.pdf", date_from="x", date_to="2026-02-01")
        self.assertEqual(bad.status_code, 400)

    def test_staff_are_refused_everywhere(self):
        self.authenticate(self.staff_user.login, PORTAL_PASSWORD)
        for path in ("", ".pdf", ".xlsx"):
            response = self._get("/my/vendor/finance/statement" + path)
            self.assertEqual(response.status_code, 403, path)
            self.assertNotIn("ORD-A", response.text)

    def test_backend_xlsx_route_needs_manager_or_finance(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        response = self._get("/leaders/vendor_statement/xlsx", vendor_id=self.vendor.id,
                             date_from="2026-02-01", date_to="2026-02-28")
        self.assertNotEqual(response.status_code, 200)
        self.env["res.users"].create({
            "name": "Route Finance", "login": "route.finance@example.com", "password": PORTAL_PASSWORD,
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id,
                                  self.env.ref("leaders_marketplace.group_marketplace_finance").id])]})
        self.env.cr.flush()
        self.authenticate("route.finance@example.com", PORTAL_PASSWORD)
        response = self._get("/leaders/vendor_statement/xlsx", vendor_id=self.vendor.id,
                             date_from="2026-02-01", date_to="2026-02-28")
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response.headers["Content-Type"])
