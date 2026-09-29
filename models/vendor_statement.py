import io
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

#: Longest period one statement may cover.
STATEMENT_MAX_DAYS = 366


class ResPartner(models.Model):
    _inherit = "res.partner"

    def _leaders_vendor_statement(self, date_from=None, date_to=None):
        """Statement of account of ONE vendor for a period (portal, PDF, xlsx, backend).

        Balance basis (the wallet ledger, ``leaders.wallet.transaction``):

        * every transaction of the vendor counts, whatever its state (hold,
          available or settled), EXCEPT ``cod_collected``: that is cash the
          vendor already holds and never part of a payout (exactly what
          ``leaders.marketplace.accounting._leaders_check_ledger`` does). So a
          statement that ends today closes on that check's
          ``hold + available + settled`` wallet total.
        * amounts are signed: credit = money in favour of the vendor (positive),
          debit = money taken from the vendor (negative: commission, fees,
          returns, penalties, negative adjustments, reserve held).
        * the date of a movement is the date its transaction was CREATED (UTC),
          ordered by creation then id; ``available_date`` is only shown.
        * opening = sum of movements created before ``date_from``;
          closing = opening + movements of the period.
        * a transaction's ``state`` is its state NOW (the ledger keeps no
          history), so it is shown per line and summed in ``closing_by_state``
          (movements up to ``date_to``), but never changes the balance.
        """
        self.ensure_one()
        vendor = self.commercial_partner_id
        date_from, date_to = self.env["leaders.vendor.statement"]._resolve_dates(date_from, date_to)
        currency = vendor.company_currency_id or self.env.company.currency_id
        Tx = self.env["leaders.wallet.transaction"].sudo()
        base = [("vendor_id", "=", vendor.id), ("type", "!=", "cod_collected")]
        start = datetime.combine(date_from, datetime.min.time())
        end = datetime.combine(date_to + timedelta(days=1), datetime.min.time())
        opening = sum(Tx.search(base + [("create_date", "<", start)]).mapped("amount"))
        txs = Tx.search(base + [("create_date", ">=", start), ("create_date", "<", end)],
                        order="create_date, id")
        type_labels = dict(Tx._fields["type"]._description_selection(self.env))
        state_labels = dict(Tx._fields["state"]._description_selection(self.env))
        balance = currency.round(opening)
        lines, total_debit, total_credit = [], 0.0, 0.0
        for tx in txs:
            amount = tx.amount
            credit = amount if amount > 0 else 0.0
            debit = -amount if amount < 0 else 0.0
            total_credit += credit
            total_debit += debit
            balance = currency.round(balance + amount)
            lines.append({
                "id": tx.id,
                "date": fields.Datetime.to_datetime(tx.create_date).date(),
                "type": tx.type,
                "type_label": type_labels.get(tx.type, tx.type),
                "reference": tx.reference or tx.vendor_order_id.name or "",
                "state": tx.state,
                "state_label": state_labels.get(tx.state, tx.state),
                "available_date": tx.available_date or False,
                "settlement": tx.settlement_id.name or "",
                "debit": currency.round(debit),
                "credit": currency.round(credit),
                "balance": balance,
            })
        up_to = Tx.search(base + [("create_date", "<", end)])
        settlements = self.env["leaders.settlement"].sudo().search([
            ("vendor_id", "=", vendor.id),
            ("period_from", "<=", date_to), ("period_to", ">=", date_from),
        ], order="period_from, id")
        settle_labels = dict(settlements._fields["state"]._description_selection(self.env))
        payout = vendor._leaders_masked_payout_account()
        return {
            "vendor": vendor,
            "vendor_name": vendor.vendor_store_name or vendor.name,
            "currency": currency,
            "date_from": date_from,
            "date_to": date_to,
            "opening": currency.round(opening),
            "total_debit": currency.round(total_debit),
            "total_credit": currency.round(total_credit),
            "closing": balance,
            "closing_by_state": {
                state: currency.round(sum(up_to.filtered(lambda t: t.state == state).mapped("amount")))
                for state in ("hold", "available", "settled")},
            "lines": lines,
            "settlements": [{
                "name": s.name, "period_from": s.period_from, "period_to": s.period_to,
                "state": s.state, "state_label": settle_labels.get(s.state, s.state),
                "gross": s.gross_amount, "deductions": s.deductions, "net": s.net_amount,
                "payout_account": payout,
            } for s in settlements],
        }


class VendorStatement(models.AbstractModel):
    """Helpers around the vendor statement: dates, xlsx, file name."""
    _name = "leaders.vendor.statement"
    _description = "Vendor Statement Helpers"

    @api.model
    def _resolve_dates(self, date_from=None, date_to=None):
        """Validated ``(date_from, date_to)``; empty values default to the current month."""
        def parse(value):
            if not value:
                return False
            try:
                return fields.Date.to_date(value)
            except (TypeError, ValueError):
                raise UserError(_("Invalid date: %s. Use the format YYYY-MM-DD.", value))
        date_from, date_to = parse(date_from), parse(date_to)
        if not date_from and not date_to:
            today = fields.Date.context_today(self)
            date_from = today.replace(day=1)
            date_to = date_from + relativedelta(months=1, days=-1)
        elif not date_from or not date_to:
            raise UserError(_("Please give both the start and the end date."))
        if date_from > date_to:
            raise UserError(_("The start date cannot be after the end date."))
        if (date_to - date_from).days + 1 > STATEMENT_MAX_DAYS:
            raise UserError(_("A statement cannot cover more than one year."))
        return date_from, date_to

    @api.model
    def _filename(self, statement, extension):
        return "statement-%s-%s-%s.%s" % (
            statement["vendor"].id, statement["date_from"], statement["date_to"], extension)

    @api.model
    def _xlsx(self, statement):
        """The statement as an xlsx workbook (bytes): 'Statement' and 'Settlements' sheets."""
        import xlsxwriter
        stream = io.BytesIO()
        book = xlsxwriter.Workbook(stream, {"in_memory": True})
        title = book.add_format({"bold": True, "font_size": 14})
        head = book.add_format({"bold": True, "bg_color": "#D9E1F2", "border": 1, "align": "center"})
        date_fmt = book.add_format({"num_format": "yyyy-mm-dd", "align": "left"})
        number = book.add_format({"num_format": "#,##0.00"})
        bold_number = book.add_format({"num_format": "#,##0.00", "bold": True, "top": 1})
        bold = book.add_format({"bold": True, "top": 1})
        text = book.add_format({})
        currency = statement["currency"]

        sheet = book.add_worksheet(_("Statement")[:31])
        sheet.write(0, 0, _("Vendor Statement of Account"), title)
        sheet.write(1, 0, _("Vendor"))
        sheet.write(1, 1, statement["vendor_name"])
        sheet.write(2, 0, _("Period"))
        sheet.write(2, 1, "%s - %s" % (statement["date_from"], statement["date_to"]))
        sheet.write(3, 0, _("Currency"))
        sheet.write(3, 1, currency.name)
        headers = [_("Date"), _("Type"), _("Reference"), _("Status"),
                   _("Debit"), _("Credit"), _("Balance")]
        row = 5
        for col, label in enumerate(headers):
            sheet.write(row, col, label, head)
        row += 1
        sheet.write(row, 2, _("Opening balance"), bold)
        sheet.write_number(row, 6, statement["opening"], bold_number)
        row += 1
        first = row
        for line in statement["lines"]:
            sheet.write_datetime(row, 0, datetime.combine(line["date"], datetime.min.time()), date_fmt)
            sheet.write(row, 1, line["type_label"], text)
            sheet.write(row, 2, line["reference"], text)
            sheet.write(row, 3, line["state_label"], text)
            sheet.write_number(row, 4, line["debit"], number)
            sheet.write_number(row, 5, line["credit"], number)
            sheet.write_number(row, 6, line["balance"], number)
            row += 1
        sheet.write(row, 2, _("Total movements"), bold)
        sheet.write_number(row, 4, statement["total_debit"], bold_number)
        sheet.write_number(row, 5, statement["total_credit"], bold_number)
        row += 1
        sheet.write(row, 2, _("Closing balance"), bold)
        sheet.write_number(row, 6, statement["closing"], bold_number)
        sheet.set_column(0, 0, 12)
        sheet.set_column(1, 3, 22)
        sheet.set_column(4, 6, 16)
        sheet.freeze_panes(6, 0)

        sheet2 = book.add_worksheet(_("Settlements")[:31])
        headers2 = [_("Settlement"), _("Period from"), _("Period to"), _("Status"),
                    _("Gross"), _("Deductions"), _("Net"), _("Payout account")]
        for col, label in enumerate(headers2):
            sheet2.write(0, col, label, head)
        for i, s in enumerate(statement["settlements"], start=1):
            sheet2.write(i, 0, s["name"])
            sheet2.write_datetime(i, 1, datetime.combine(s["period_from"], datetime.min.time()), date_fmt)
            sheet2.write_datetime(i, 2, datetime.combine(s["period_to"], datetime.min.time()), date_fmt)
            sheet2.write(i, 3, s["state_label"])
            sheet2.write_number(i, 4, s["gross"], number)
            sheet2.write_number(i, 5, s["deductions"], number)
            sheet2.write_number(i, 6, s["net"], number)
            sheet2.write(i, 7, s["payout_account"])
        sheet2.set_column(0, 3, 18)
        sheet2.set_column(4, 7, 16)
        book.close()
        return stream.getvalue()


class VendorStatementReport(models.AbstractModel):
    _name = "report.leaders_marketplace.report_vendor_statement"
    _description = "Vendor Statement Report"

    @api.model
    def _get_report_values(self, docids, data=None):
        data = data or {}
        partners = self.env["res.partner"].sudo().browse(docids)
        return {
            "doc_ids": docids,
            "doc_model": "res.partner",
            "docs": partners,
            "statements": {
                p.id: p._leaders_vendor_statement(data.get("date_from"), data.get("date_to"))
                for p in partners},
        }


class VendorStatementWizard(models.TransientModel):
    _name = "leaders.vendor.statement.wizard"
    _description = "Vendor Statement Wizard"

    vendor_id = fields.Many2one(
        "res.partner", required=True, domain=[("is_marketplace_vendor", "=", True)])
    date_from = fields.Date(
        required=True, default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(
        required=True,
        default=lambda self: fields.Date.context_today(self).replace(day=1) + relativedelta(months=1, days=-1))

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if self.env.context.get("active_model") == "res.partner" and self.env.context.get("active_id"):
            res["vendor_id"] = self.env["res.partner"].browse(
                self.env.context["active_id"]).commercial_partner_id.id
        return res

    def _check_staff(self):
        if not self.env.su and not (
                self.env.user.has_group("leaders_marketplace.group_marketplace_manager")
                or self.env.user.has_group("leaders_marketplace.group_marketplace_finance")):
            raise AccessError(_("Only marketplace managers and finance can print vendor statements."))

    def _dates(self):
        self.ensure_one()
        self._check_staff()
        if not self.vendor_id.is_marketplace_vendor:
            raise UserError(_("%s is not a marketplace vendor.", self.vendor_id.display_name))
        return self.env["leaders.vendor.statement"]._resolve_dates(self.date_from, self.date_to)

    def action_print(self):
        date_from, date_to = self._dates()
        return self.env.ref("leaders_marketplace.action_report_vendor_statement").report_action(
            self.vendor_id.commercial_partner_id,
            data={"date_from": str(date_from), "date_to": str(date_to)})

    def action_xlsx(self):
        date_from, date_to = self._dates()
        return {
            "type": "ir.actions.act_url",
            "url": "/leaders/vendor_statement/xlsx?vendor_id=%s&date_from=%s&date_to=%s" % (
                self.vendor_id.commercial_partner_id.id, date_from, date_to),
            "target": "self",
        }
