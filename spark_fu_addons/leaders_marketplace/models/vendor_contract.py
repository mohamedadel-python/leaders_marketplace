"""The vendor's contract model (project guide, feature 1): every vendor sells free for the first months, then
chooses near the end of the free period between a subscription (the buyer sees the vendor's name and deals with
them) and a commission (the name is hidden, the platform is in between). A later switch is a request the admin
reviews; it applies to all the vendor's products at once."""
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

REMIND_DAYS = 14  # the choice opens (and the vendor is reminded) this many days before the free period ends
CONTRACT_MODELS = [("subscription", "Subscription"), ("commission", "Commission")]
# Who the buyer sees on a commission vendor's products: the platform (default) or the vendor
COMMISSION_PUBLISHER_PARAM = "leaders_marketplace.commission_publisher"
# Equipment rent requests stay with the platform unless the admin opens them to the vendor
RENT_TO_VENDOR_PARAM = "leaders_marketplace.rent_requests_to_vendor"
# Marketplace settings: the length of the free period and the grace days after a subscription or free period ends
FREE_MONTHS_PARAM = "leaders_marketplace.free_period_months"
GRACE_DAYS_PARAM = "leaders_marketplace.subscription_grace_days"
DEFAULT_GRACE_DAYS = 7
REMIND_DAYS_PARAM = "leaders_marketplace.subscription_remind_days"
DEFAULT_SUBSCRIPTION_REMIND_DAYS = 7


class ResPartnerContract(models.Model):
    _inherit = "res.partner"

    leaders_contract_model = fields.Selection(CONTRACT_MODELS, string="Contract model", tracking=True,
                                              help="Chosen by the vendor near the end of the free period.")
    leaders_free_until = fields.Date("Free period until", help="Starts when the vendor is activated.")
    leaders_contract_chosen_on = fields.Date("Contract chosen on", readonly=True)
    leaders_contract_reminded = fields.Boolean(copy=False)
    leaders_subscription_until = fields.Date(
        "Subscription paid until", tracking=True,
        help="Subscription vendors: the last day their subscription covers. The day after, their products leave the "
             "website until the date is extended. Empty: no end date.")
    leaders_listing_paused = fields.Boolean(
        "Products hidden (subscription ended)", readonly=True, copy=False, tracking=True,
        help="The subscription (or the free period, when no model was chosen) ended: the vendor's products are off "
             "the website. They come back as they were when the subscription is extended.")
    leaders_paused_snapshot = fields.Json(copy=False, readonly=True)
    leaders_subscription_reminded_until = fields.Date(copy=False, readonly=True)
    # True for every partner by default (the vendors before this page included); the approval of a vendor
    # application sets it off, so only a newly approved vendor gets the welcome page
    leaders_contract_onboarded = fields.Boolean(
        "Saw the model choice", copy=False, default=True,
        help="The vendor passed the welcome page where a new vendor picks the free period, a commission or a "
             "subscription. Until then the vendor dashboard opens that page first.")

    @api.model
    def _leaders_free_months(self):
        """The free period, in months (marketplace settings; the join page's value when never set)."""
        raw = self.env["ir.config_parameter"].sudo().get_param(FREE_MONTHS_PARAM)
        if raw not in (None, False, "") and str(raw).isdigit():
            return int(raw)
        return max(self.env["leaders.vendor.join.model"].sudo().search([]).mapped("free_months") or [3])

    @api.model
    def _leaders_subscription_remind_days(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(REMIND_DAYS_PARAM)
        return int(raw) if raw not in (None, False, "") and str(raw).isdigit() else DEFAULT_SUBSCRIPTION_REMIND_DAYS

    def _leaders_paid_until(self):
        """The last day the vendor's products are covered: the subscription's end, or the free period's end while no
        model is chosen. None for a commission vendor (paid per sale) or when no date is set."""
        vendor = self.commercial_partner_id
        if vendor.leaders_contract_model == "subscription":
            return vendor.leaders_subscription_until or None
        if not vendor.leaders_contract_model:
            return vendor.leaders_free_until or None
        return None

    def _leaders_subscription_notice(self):
        """What the vendor dashboard says about the subscription: None, or a dict with ``state`` ("hidden": the
        products are off the website; "grace": ended, products still shown; "ending": ends within the reminder
        days), the end date, the days left and whether the vendor may switch to a commission."""
        vendor = self.commercial_partner_id
        end = vendor._leaders_paid_until()
        if not end:
            return None
        today = fields.Date.context_today(self)
        days = (end - today).days
        if vendor.leaders_listing_paused:
            state = "hidden"
        elif days < 0:
            state = "grace"
        elif days <= self._leaders_subscription_remind_days():
            state = "ending"
        else:
            return None
        pending_switch = self.env["leaders.contract.switch"].sudo().search_count(
            [("vendor_id", "=", vendor.id), ("state", "=", "pending")])
        return {"state": state, "end": end, "days": days, "hidden_on": end + timedelta(days=self._leaders_grace_days()),
                "free": not vendor.leaders_contract_model, "can_switch": not pending_switch}

    @api.model
    def _leaders_grace_days(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(GRACE_DAYS_PARAM)
        return int(raw) if raw not in (None, False, "") and str(raw).isdigit() else DEFAULT_GRACE_DAYS

    def leaders_onboard_choice(self, choice):
        """The welcome page of a newly approved vendor: keep the free period (and choose later), or pick a
        commission or a subscription now. A subscription picked now is covered until the free period ends."""
        vendor = self.commercial_partner_id.sudo()
        if choice not in ("free", "commission", "subscription"):
            raise UserError(_("Unknown contract model."))
        vals = {"leaders_contract_onboarded": True}
        if choice != "free" and not vendor.leaders_contract_model:
            vals.update(leaders_contract_model=choice, leaders_contract_chosen_on=fields.Date.context_today(self))
            if choice == "subscription" and not vendor.leaders_subscription_until:
                vals["leaders_subscription_until"] = vendor.leaders_free_until or fields.Date.context_today(self)
        vendor.write(vals)

    def _leaders_subscription_lapsed(self, today=None):
        """The subscription, or the free period of a vendor who never chose a model, ended. Commission vendors pay
        per sale, so they never lapse."""
        vendor = self.commercial_partner_id
        today = today or fields.Date.context_today(self)
        if vendor.leaders_contract_model == "subscription":
            end = vendor.leaders_subscription_until
        elif not vendor.leaders_contract_model:
            end = vendor.leaders_free_until
        else:
            return False
        # the grace days of the marketplace settings come after the last covered day
        return bool(end and today > end + timedelta(days=self._leaders_grace_days()))

    def _leaders_pause_listing(self):
        """Take the vendor's products off the website, remembering what was live."""
        Offer = self.env["leaders.marketplace.offer"].sudo()
        Tmpl = self.env["product.template"].sudo()
        for vendor in self.sudo().filtered(lambda v: not v.leaders_listing_paused):
            offers = Offer.search([("vendor_id", "=", vendor.id), ("state", "=", "active")])
            templates = Tmpl.search([("marketplace_owner_vendor_id", "=", vendor.id), ("is_published", "=", True)])
            vendor.leaders_paused_snapshot = {"offers": offers.ids, "templates": templates.ids}
            offers.write({"state": "suspended"})
            templates.write({"is_published": False})
            vendor.leaders_listing_paused = True
            vendor.message_post(
                body=_("Your subscription ended: your products are hidden from the website until it is renewed."),
                partner_ids=(vendor.child_ids.filtered("email") or vendor).ids,
                message_type="comment", subtype_xmlid="mail.mt_comment")

    def _leaders_resume_listing(self):
        """Put back exactly what the pause took off (an offer suspended later for another reason stays off)."""
        Offer = self.env["leaders.marketplace.offer"].sudo()
        Tmpl = self.env["product.template"].sudo()
        for vendor in self.sudo().filtered("leaders_listing_paused"):
            snap = vendor.leaders_paused_snapshot or {}
            if vendor.vendor_state == "approved":
                Offer.browse(snap.get("offers") or []).exists().filtered(
                    lambda o: o.state == "suspended").write({"state": "active"})
            Tmpl.browse(snap.get("templates") or []).exists().filtered(
                lambda t: t.marketplace_state == "published").write({"is_published": True})
            vendor.write({"leaders_listing_paused": False, "leaders_paused_snapshot": False})
            vendor.message_post(body=_("Your subscription is active again: your products are back on the website."),
                                partner_ids=(vendor.child_ids.filtered("email") or vendor).ids,
                                message_type="comment", subtype_xmlid="mail.mt_comment")

    @api.model
    def cron_pause_lapsed_subscriptions(self):
        """Daily: hide the products of vendors whose subscription ended, bring back the renewed ones."""
        vendors = self.sudo().search([("is_marketplace_vendor", "=", True),
                                      ("vendor_state", "in", ("approved", "frozen")), ("parent_id", "=", False)])
        lapsed = vendors.filtered(lambda v: v._leaders_subscription_lapsed())
        lapsed._leaders_pause_listing()
        (vendors - lapsed).filtered("leaders_listing_paused")._leaders_resume_listing()
        (vendors - lapsed)._leaders_remind_subscription_end()
        return len(lapsed)

    def _leaders_remind_subscription_end(self):
        """Tell a subscription vendor, once per period, that the subscription ends soon (the free period has its
        own reminder: cron_remind_contract_choice)."""
        today = fields.Date.context_today(self)
        limit = self._leaders_subscription_remind_days()
        for vendor in self.filtered(lambda v: v.leaders_contract_model == "subscription" and v.leaders_subscription_until):
            end = vendor.leaders_subscription_until
            if vendor.leaders_subscription_reminded_until == end or not 0 <= (end - today).days <= limit:
                continue
            vendor.message_post(
                body=_("Your subscription ends on %(date)s. Renew it from your vendor portal (Contract model) so your "
                       "products stay on the website, or ask to switch to a commission.", date=end),
                partner_ids=(vendor.child_ids.filtered("email") or vendor).ids,
                message_type="comment", subtype_xmlid="mail.mt_comment")
            vendor.leaders_subscription_reminded_until = end

    def write(self, vals):
        res = super().write(vals)
        # the admin extends the date or switches the model: the products come back at once
        if {"leaders_subscription_until", "leaders_free_until", "leaders_contract_model"} & set(vals):
            self.filtered(lambda v: v.leaders_listing_paused and not v._leaders_subscription_lapsed())._leaders_resume_listing()
        return res

    def _leaders_effective_contract(self):
        """The contract model that rules today: a vendor in the free period (no model chosen yet) works as a
        subscription vendor, i.e. under their own name and with the orders straight to them."""
        return self.commercial_partner_id.leaders_contract_model or "subscription"

    def _leaders_sells_direct(self):
        """Subscription (and free-period) vendors get their orders directly, without the admin in between."""
        return self._leaders_effective_contract() == "subscription"

    def _leaders_name_hidden(self):
        """Commission vendors sell under the platform's name (the default; the marketplace settings can show the
        vendor's name instead)."""
        if self.commercial_partner_id.leaders_contract_model != "commission":
            return False
        return self.env["ir.config_parameter"].sudo().get_param(COMMISSION_PUBLISHER_PARAM, "platform") != "vendor"

    @api.model
    def _leaders_rent_requests_to_vendor(self):
        return self.env["ir.config_parameter"].sudo().get_param(RENT_TO_VENDOR_PARAM, "0") in ("1", "True", "true")

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
        vals = {"leaders_contract_model": code, "leaders_contract_chosen_on": fields.Date.context_today(self)}
        if code == "subscription" and not vendor.leaders_subscription_until:
            vals["leaders_subscription_until"] = vendor.leaders_free_until or fields.Date.context_today(self)
        vendor.sudo().write(vals)

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
