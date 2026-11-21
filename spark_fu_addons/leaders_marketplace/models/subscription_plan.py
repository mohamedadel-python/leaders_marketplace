"""Subscription plans (project guide: the display subscription is priced by period and number of products). The admin
lists the plans; a vendor picks one on the welcome page or on the contract page; the platform confirms the payment
and the vendor's subscription is extended by the plan's period."""
from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class LeadersSubscriptionPlan(models.Model):
    _name = "leaders.subscription.plan"
    _description = "Vendor Subscription Plan"
    _order = "sequence, months, id"

    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, translate=True)
    months = fields.Integer("Period (months)", required=True, default=1)
    max_products = fields.Integer("Products", help="How many products the vendor may list. 0: no limit.")
    price = fields.Monetary(required=True, currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id, required=True)
    note = fields.Char("Short note", translate=True, help="One line under the plan on the vendor's page.")
    highlight = fields.Boolean("Best value", help="Shown with a ribbon on the vendor's page.")
    active = fields.Boolean(default=True)

    _sql_constraints = [("months_positive", "check(months > 0)", "The period is at least one month."),
                        ("price_positive", "check(price >= 0)", "The price cannot be negative.")]

    def _leaders_monthly(self):
        self.ensure_one()
        return self.price / self.months if self.months else self.price


class LeadersSubscriptionOrder(models.Model):
    """A vendor's subscription to a plan for one period: pending until the platform confirms the payment."""
    _name = "leaders.subscription.order"
    _description = "Vendor Subscription"
    _inherit = ["mail.thread", "leaders.backoffice.mixin"]
    _order = "id desc"

    name = fields.Char(readonly=True, copy=False, default=lambda self: _("New"))
    vendor_id = fields.Many2one("res.partner", required=True, index=True, readonly=True)
    plan_id = fields.Many2one("leaders.subscription.plan", required=True, readonly=True)
    months = fields.Integer(readonly=True)
    price = fields.Monetary(readonly=True, currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", readonly=True)
    state = fields.Selection([("pending", "Waiting for payment"), ("paid", "Paid"), ("cancelled", "Cancelled")],
                             default="pending", required=True, tracking=True, index=True)
    date_start = fields.Date("From", readonly=True)
    date_end = fields.Date("Until", readonly=True)
    paid_on = fields.Datetime(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            plan = self.env["leaders.subscription.plan"].browse(vals.get("plan_id"))
            vals.setdefault("months", plan.months)
            vals.setdefault("price", plan.price)
            vals.setdefault("currency_id", plan.currency_id.id)
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = "SUB-%05d" % (self.search_count([]) + 1)
        return super().create(vals_list)

    def action_mark_paid(self):
        """The platform received the money: the vendor's subscription runs one more period."""
        self._leaders_check_backoffice("finance")
        today = fields.Date.context_today(self)
        for rec in self.filtered(lambda r: r.state == "pending"):
            vendor = rec.vendor_id.commercial_partner_id.sudo()
            # a new period starts where the paid time ends (the free period counts as paid time)
            start = max(d for d in (today, vendor.leaders_subscription_until, vendor.leaders_free_until) if d)
            end = start + relativedelta(months=rec.months)
            vendor.write({"leaders_contract_model": "subscription", "leaders_subscription_plan_id": rec.plan_id.id,
                          "leaders_subscription_until": end})
            rec.write({"state": "paid", "date_start": start, "date_end": end, "paid_on": fields.Datetime.now()})
            vendor.message_post(
                body=_("Subscription %(plan)s paid: your products stay on the website until %(date)s.",
                       plan=rec.plan_id.name, date=end),
                partner_ids=(vendor.child_ids.filtered("email") or vendor).ids,
                message_type="comment", subtype_xmlid="mail.mt_comment")

    def action_cancel(self):
        self._leaders_check_backoffice("finance")
        self.filtered(lambda r: r.state == "pending").write({"state": "cancelled"})


class ResPartnerSubscriptionPlan(models.Model):
    _inherit = "res.partner"

    leaders_subscription_plan_id = fields.Many2one("leaders.subscription.plan", "Subscription plan", tracking=True)
    leaders_subscription_order_ids = fields.One2many("leaders.subscription.order", "vendor_id", "Subscriptions")

    def leaders_request_plan(self, plan):
        """The vendor picks a plan: one pending subscription to pay (a new pick replaces the unpaid one)."""
        vendor = self.commercial_partner_id.sudo()
        plan = plan.sudo().exists().filtered("active")
        if not plan:
            raise UserError(_("Choose one of the plans."))
        Order = self.env["leaders.subscription.order"].sudo()
        Order.search([("vendor_id", "=", vendor.id), ("state", "=", "pending")]).write({"state": "cancelled"})
        order = Order.create({"vendor_id": vendor.id, "plan_id": plan.id})
        vals = {"leaders_subscription_plan_id": plan.id}
        if not vendor.leaders_contract_model:
            vals.update(leaders_contract_model="subscription",
                        leaders_contract_chosen_on=fields.Date.context_today(self))
        if not vendor.leaders_subscription_until:  # covered by the free period until the first payment
            vals["leaders_subscription_until"] = vendor.leaders_free_until or fields.Date.context_today(self)
        vendor.write(vals)
        return order
