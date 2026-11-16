"""Pre-listing inspection result: the vendor pays the inspection when the product does not match its listing.

The admin picks the default way in the settings (deducted from the next settlement, or a separate invoice);
the reviewer can change the amount and the way for one case in the wizard.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ProductTemplateInspection(models.Model):
    _inherit = "product.template"

    leaders_inspection_cost = fields.Monetary(
        "Inspection cost in the price", currency_field="currency_id", copy=False,
        groups="leaders_marketplace.group_marketplace_reviewer,leaders_marketplace.group_marketplace_manager",
        help="The pre-listing inspection the vendor paid. It is added to the price the buyer sees (never shown as an "
             "inspection fee), so the vendor gets it back when the machine sells. Only the platform sees it.")
    leaders_inspection_result = fields.Selection(
        [("passed", "Matches the listing"), ("mismatch", "Does not match the listing")],
        string="Inspection result", copy=False, readonly=True, tracking=True)

    def action_leaders_inspection_passed(self):
        self._leaders_check_backoffice("reviewer")
        self.sudo().write({"leaders_inspected": True, "leaders_inspection_result": "passed"})
        self._leaders_log_inspection(_("Inspection: matches the listing"))

    def _leaders_log_inspection(self, note):
        """The asset log of a rental machine keeps the result of its last inspection."""
        for tmpl in self.filtered(lambda t: t.leaders_rentable):
            self.env["leaders.asset.move"].sudo().create({
                "product_tmpl_id": tmpl.id, "state": tmpl.leaders_asset_state or "available", "note": note})

    def action_leaders_inspection_mismatch(self):
        self.ensure_one()
        self._leaders_check_backoffice("reviewer")
        return {
            "type": "ir.actions.act_window", "res_model": "leaders.inspection.mismatch.wizard", "view_mode": "form",
            "target": "new", "name": _("Inspection does not match"),
            "context": {"default_product_tmpl_id": self.id},
        }


class InspectionMismatchWizard(models.TransientModel):
    _name = "leaders.inspection.mismatch.wizard"
    _description = "Inspection Does Not Match"

    def _default_mode(self):
        return self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.inspection_fee_mode") or "deduct"

    def _default_amount(self):
        try:
            return float(self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.inspection_fee_amount") or 0)
        except ValueError:
            return 0.0

    product_tmpl_id = fields.Many2one("product.template", string="Product", required=True, readonly=True)
    vendor_id = fields.Many2one("res.partner", compute="_compute_vendor_id", string="Vendor")
    amount = fields.Monetary("Inspection fee", currency_field="currency_id", default=_default_amount)
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)
    mode = fields.Selection(
        [("deduct", "Deduct from the vendor's next settlement"), ("invoice", "Separate invoice to the vendor")],
        string="Charged by", required=True, default=_default_mode)
    reason = fields.Text("What does not match", required=True, help="Shown to the vendor as the rejection reason.")

    @api.depends("product_tmpl_id")
    def _compute_vendor_id(self):
        for wizard in self:
            wizard.vendor_id = wizard.product_tmpl_id.marketplace_vendor_id.commercial_partner_id

    def action_confirm(self):
        self.ensure_one()
        tmpl = self.product_tmpl_id
        tmpl._leaders_check_backoffice("reviewer")
        if self.amount < 0:
            raise UserError(_("The inspection fee cannot be negative."))
        vendor = self.vendor_id
        if self.amount and not vendor:
            raise UserError(_("This product has no vendor to charge the inspection to."))
        charge = False
        if self.amount:
            charge = self._charge_invoice(vendor) if self.mode == "invoice" else self._charge_wallet(vendor)
        tmpl.sudo().write({
            "leaders_inspected": False, "leaders_inspection_result": "mismatch",
            "marketplace_reject_reason": self.reason,
        })
        if tmpl.marketplace_state != "rejected":
            tmpl.action_marketplace_reject()
        how = dict(self._fields["mode"].selection)[self.mode]
        body = _("Inspection does not match the listing: %(reason)s", reason=self.reason)
        if charge:
            body += "\n" + _("Inspection fee %(amount)s charged to %(vendor)s (%(how)s, %(ref)s).",
                             amount=self.amount, vendor=vendor.display_name, how=how, ref=charge.display_name)
        tmpl.sudo().message_post(body=body)
        tmpl._leaders_log_inspection(_("Inspection: does not match (%s)", self.reason))
        return {"type": "ir.actions.act_window_close"}

    def _charge_wallet(self, vendor):
        return self.env["leaders.wallet.transaction"].sudo().create({
            "vendor_id": vendor.id, "type": "inspection_fee", "amount": -self.amount,
            "currency_id": self.currency_id.id, "state": "available", "available_date": fields.Date.today(),
            "reference": "INSP/%s" % self.product_tmpl_id.id,
            "note": _("Inspection of %(product)s: %(reason)s", product=self.product_tmpl_id.display_name, reason=self.reason),
        })

    def _charge_invoice(self, vendor):
        accounts = self.env["leaders.marketplace.accounting"]._get_accounts()
        line = {
            "product_id": self.env.ref("leaders_marketplace.product_inspection_fee").id,
            "name": _("Inspection of %s (does not match the listing)", self.product_tmpl_id.display_name),
            "quantity": 1, "price_unit": self.amount,
            "tax_ids": [(6, 0, self.env["leaders.marketplace.accounting"]._commission_tax_ids().ids)],
        }
        if accounts.get("fee_income"):
            line["account_id"] = accounts["fee_income"]
        move = self.env["account.move"].sudo().create({
            "move_type": "out_invoice", "partner_id": vendor.id, "invoice_line_ids": [(0, 0, line)],
            "invoice_origin": self.product_tmpl_id.display_name,
        })
        move.action_post()
        return move
