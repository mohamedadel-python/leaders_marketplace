"""The vendor's contract model (project guide, feature 1): every vendor sells free for the first months, then
chooses near the end of the free period between a subscription (the buyer sees the vendor's name and deals with
them) and a commission (the name is hidden, the platform is in between). A later switch is a request the admin
reviews; it applies to all the vendor's products at once."""
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

REMIND_DAYS = 14  # the choice opens (and the vendor is reminded) this many days before the free period ends
CONTRACT_MODELS = [("subscription", "Subscription"), ("commission", "Commission")]


class ResPartnerContract(models.Model):
    _inherit = "res.partner"

    leaders_contract_model = fields.Selection(CONTRACT_MODELS, string="Contract model", tracking=True,
                                              help="Chosen by the vendor near the end of the free period.")
    leaders_free_until = fields.Date("Free period until", help="Starts when the vendor is activated.")
    leaders_contract_chosen_on = fields.Date("Contract chosen on", readonly=True)
    leaders_contract_reminded = fields.Boolean(copy=False)

    def _leaders_name_hidden(self):
        """Commission vendors sell under the platform's name: the buyer never sees who they are."""
        return self.commercial_partner_id.leaders_contract_model == "commission"

    def _leaders_contract_choice_open(self):
        self.ensure_one()
        free_until = self.commercial_partner_id.leaders_free_until
        return not free_until or fields.Date.context_today(self) >= free_until - timedelta(days=REMIND_DAYS)

    def leaders_choose_contract(self, code):
        vendor = self.commercial_partner_id
        if code not in dict(CONTRACT_MODELS):
            raise UserError(_("Unknown contract model."))
        if vendor.leaders_contract_model:
            raise UserError(_("Your contract model is already chosen; ask for a switch instead."))
        if not vendor._leaders_contract_choice_open():
            raise UserError(_("You choose your contract model near the end of your free period."))
        vendor.sudo().write({"leaders_contract_model": code, "leaders_contract_chosen_on": fields.Date.context_today(self)})

    @api.model
    def cron_remind_contract_choice(self):
        """Remind the vendors whose free period ends soon and who have not chosen yet."""
        limit = fields.Date.context_today(self) + timedelta(days=REMIND_DAYS)
        vendors = self.sudo().search([("is_marketplace_vendor", "=", True), ("vendor_state", "=", "approved"),
                                      ("leaders_contract_model", "=", False), ("leaders_contract_reminded", "=", False),
                                      ("leaders_free_until", "!=", False), ("leaders_free_until", "<=", limit)])
        for vendor in vendors:
            followers = vendor.child_ids.filtered("email") or vendor
            vendor.message_post(
                body=_("Your free period ends on %(date)s. Choose your contract model (subscription or commission) "
                       "in your vendor portal: Contract model.", date=vendor.leaders_free_until),
                partner_ids=followers.ids, message_type="comment", subtype_xmlid="mail.mt_comment")
            vendor.leaders_contract_reminded = True
        return len(vendors)


class LeadersContractSwitch(models.Model):
    _name = "leaders.contract.switch"
    _description = "Contract Model Switch Request"
    _inherit = ["mail.thread", "leaders.backoffice.mixin"]
    _order = "id desc"

    vendor_id = fields.Many2one("res.partner", required=True, index=True, readonly=True)
    from_model = fields.Selection(CONTRACT_MODELS, readonly=True)
    to_model = fields.Selection(CONTRACT_MODELS, required=True, readonly=True)
    note = fields.Text("Vendor note", readonly=True)
    state = fields.Selection([("pending", "To review"), ("approved", "Approved"), ("rejected", "Rejected")],
                             default="pending", required=True, tracking=True)
    reject_reason = fields.Text(tracking=True)

    def action_approve(self):
        self._leaders_check_backoffice("manager")
        for rec in self.filtered(lambda r: r.state == "pending"):
            # one model for the vendor: it applies to every product of theirs at once
            rec.vendor_id.sudo().write({"leaders_contract_model": rec.to_model,
                                        "leaders_contract_chosen_on": fields.Date.context_today(rec)})
            rec.state = "approved"

    def action_reject(self):
        self._leaders_check_backoffice("manager")
        for rec in self:
            if not rec.reject_reason:
                raise UserError(_("Please write why the switch is rejected; the vendor sees it."))
            rec.state = "rejected"


class ResUsersAccountType(models.Model):
    _inherit = "res.users"

    leaders_is_vendor = fields.Boolean(related="partner_id.commercial_partner_id.is_marketplace_vendor",
                                       string="Vendor", store=True)
