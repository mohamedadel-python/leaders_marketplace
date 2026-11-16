from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


def normalize_account_number(number):
    """Same normalisation as vendor applications: no spaces, upper case."""
    return "".join((number or "").split()).upper()


def mask_account_number(number):
    """``****1234``: only the last four characters of an account are shown."""
    number = normalize_account_number(number)
    if not number:
        return ""
    return "****%s" % number[-4:]


class ResPartner(models.Model):
    _inherit = "res.partner"

    vendor_payout_bank_id = fields.Many2one(
        "res.partner.bank", string="Payout Bank Account", copy=False, readonly=True,
        domain="[('partner_id', '=', id)]", index="btree_not_null",
        help="Bank account settlements are paid to. Set when the vendor application "
             "is approved and replaced only by an approved payout account change.")

    def _leaders_masked_payout_account(self):
        self.ensure_one()
        account = self.commercial_partner_id.vendor_payout_bank_id
        return mask_account_number(account.acc_number) if account else ""


class VendorBankChange(models.Model):
    """A vendor's request to change the account its settlements are paid to."""
    _name = "leaders.vendor.bank.change"
    _description = "Vendor Payout Bank Account Change"
    _inherit = ["mail.thread", "leaders.backoffice.mixin"]
    _order = "id desc"

    name = fields.Char(default="New", copy=False, readonly=True)
    vendor_id = fields.Many2one(
        "res.partner", string="Vendor", required=True, index=True, tracking=True,
        domain="[('is_marketplace_vendor', '=', True)]")
    requested_by = fields.Many2one("res.users", string="Requested By", readonly=True, copy=False)
    bank_name = fields.Char(required=True)
    account_holder = fields.Char(required=True)
    account_number = fields.Char(string="Account Number / IBAN", required=True)
    masked_account_number = fields.Char(compute="_compute_masked_account_number")
    proof = fields.Binary(string="Bank Proof", required=True, attachment=True, copy=False)
    proof_name = fields.Char(string="Proof File Name")
    state = fields.Selection([
        ("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected"),
    ], default="pending", required=True, tracking=True, copy=False, index=True)
    reject_reason = fields.Text(copy=False)
    reviewed_by = fields.Many2one("res.users", readonly=True, copy=False)
    reviewed_date = fields.Datetime(readonly=True, copy=False)
    bank_id = fields.Many2one("res.partner.bank", string="Resulting Bank Account", readonly=True, copy=False)
    previous_bank_id = fields.Many2one("res.partner.bank", string="Replaced Account", readonly=True, copy=False)

    def init(self):
        # One pending request per vendor, whatever path created it.
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS leaders_vendor_bank_change_one_pending "
            "ON leaders_vendor_bank_change (vendor_id) WHERE state = 'pending'")

    @api.depends("account_number")
    def _compute_masked_account_number(self):
        for rec in self:
            rec.masked_account_number = mask_account_number(rec.account_number)

    @api.model
    def _blocks_payout(self, vendor):
        """True when payouts to ``vendor`` must wait for a pending bank change."""
        param = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.block_payout_on_pending_bank_change")
        if param not in ("True", "true", "1"):
            return False
        return bool(self.sudo().search_count([
            ("vendor_id", "=", vendor.commercial_partner_id.id), ("state", "=", "pending")]))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("vendor_id"):
                vals["vendor_id"] = self.env["res.partner"].browse(vals["vendor_id"]).commercial_partner_id.id
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("leaders.vendor.bank.change") or "New"
            if vals.get("account_number"):
                vals["account_number"] = normalize_account_number(vals["account_number"])
            vals.setdefault("requested_by", self.env.user.id)
            if vals.get("vendor_id") and self.sudo().search_count([
                    ("vendor_id", "=", vals["vendor_id"]), ("state", "=", "pending")]):
                raise UserError(_("A payout account change is already waiting for review for this vendor."))
        records = super().create(vals_list)
        for rec in records:
            rec._notify_owner_of_request()
        return records

    def write(self, vals):
        if vals.get("account_number"):
            vals = dict(vals, account_number=normalize_account_number(vals["account_number"]))
        return super().write(vals)

    @api.constrains("proof")
    def _check_proof(self):
        for rec in self:
            if not rec.proof:
                raise ValidationError(_("A bank proof is required to change the payout account."))

    @api.constrains("vendor_id", "state")
    def _check_one_pending(self):
        for rec in self.filtered(lambda r: r.state == "pending"):
            if self.sudo().search_count([
                    ("vendor_id", "=", rec.vendor_id.id), ("state", "=", "pending"), ("id", "!=", rec.id)]):
                raise ValidationError(_("A payout account change is already waiting for review for this vendor."))

    def _notify_owner_of_request(self):
        """Security notice to the vendor owner(s): always sent, no setting."""
        self.ensure_one()
        Notification = self.env["leaders.notification"]
        owners = Notification._vendor_owner_partners(self.vendor_id).filtered("email")
        sent = Notification.notify(
            "vendor_bank_change_requested", self, partners=owners, force=True) if owners else False
        if not sent:
            self.message_post(body=_("The security notice could not be sent to the vendor owner."))

    # -- review ---------------------------------------------------------------
    def _get_or_create_bank_account(self):
        self.ensure_one()
        partner = self.vendor_id.commercial_partner_id
        Bank = self.env["res.partner.bank"].sudo().with_context(active_test=False)
        account = Bank.search([("partner_id", "=", partner.id), ("acc_number", "=", self.account_number)], limit=1)
        if account:
            if not account.active:
                account.active = True
            return account
        bank = self.env["res.bank"].sudo().search([("name", "=ilike", self.bank_name)], limit=1) \
            or self.env["res.bank"].sudo().create({"name": self.bank_name})
        return Bank.create({
            "partner_id": partner.id,
            "bank_id": bank.id,
            "acc_number": self.account_number,
            "acc_holder_name": self.account_holder,
        })

    def action_approve(self):
        self._leaders_check_backoffice("finance")
        for rec in self:
            if rec.state != "pending":
                raise UserError(_("Only a pending request can be approved."))
            partner = rec.vendor_id.commercial_partner_id
            account = rec._get_or_create_bank_account()
            rec.previous_bank_id = partner.vendor_payout_bank_id
            partner.sudo().vendor_payout_bank_id = account.id
            rec.write({
                "state": "approved", "bank_id": account.id,
                "reviewed_by": self.env.user.id, "reviewed_date": fields.Datetime.now(),
            })
            rec.message_post(body=_(
                "Approved: settlements of %(vendor)s are now paid to %(account)s.",
                vendor=partner.display_name, account=mask_account_number(account.acc_number)))
            self.env["leaders.notification"].notify(
                "vendor_bank_change_approved", rec,
                partners=self.env["leaders.notification"]._vendor_owner_partners(partner))
        return True

    def action_reject(self):
        self._leaders_check_backoffice("finance")
        for rec in self:
            if rec.state != "pending":
                raise UserError(_("Only a pending request can be rejected."))
            if not (rec.reject_reason or "").strip():
                raise UserError(_("Please provide a rejection reason."))
            rec.write({
                "state": "rejected",
                "reviewed_by": self.env.user.id, "reviewed_date": fields.Datetime.now(),
            })
            self.env["leaders.notification"].notify(
                "vendor_bank_change_rejected", rec,
                partners=self.env["leaders.notification"]._vendor_owner_partners(rec.vendor_id))
        return True
