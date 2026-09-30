from html import escape
import json
from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

class CommissionRule(models.Model):
    _name = "leaders.commission.rule"
    _description = "Marketplace Commission Rule"
    _inherit = ["mail.thread", "mail.activity.mixin", "leaders.backoffice.mixin"]
    _order = "priority desc, id"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True, tracking=True)
    priority = fields.Integer(default=10)
    # Legacy values remain readable during upgrades; only vendor is used by the engine.
    scope = fields.Selection([
        ("vendor", "Vendor"), ("category", "Legacy: Category"),
        ("tier", "Legacy: Vendor Tier"), ("global", "Legacy: Global"),
    ], default="vendor", required=True)
    vendor_id = fields.Many2one("res.partner", index=True, tracking=True)
    vendor_ids = fields.Many2many(
        "res.partner", "leaders_commission_rule_vendor_rel", "rule_id", "vendor_id",
        string="Vendors", tracking=True,
    )
    state = fields.Selection([
        ("draft", "Draft"), ("approved", "Approved"), ("done", "Done"),
        ("cancel", "Cancelled"),
    ], default="draft", required=True, tracking=True)
    periodicity = fields.Selection([
        ("month", "Monthly"), ("quarter", "Quarterly"), ("year", "Yearly"),
    ], default="quarter", required=True)
    type = fields.Selection([
        ("target", "Targets"), ("achieve", "Achievements"),
    ], default="target", required=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    rule_type = fields.Selection([
        ("percent","Percentage"),("fixed","Fixed"),("compound","Compound")
    ], default="percent", required=True)
    percent = fields.Float(default=10.0)
    fixed_amount = fields.Monetary(currency_field="currency_id")
    plan_effect = fields.Selection([
        ("deduction", "Extra Deduction"),
    ], default="deduction", required=True, tracking=True,
        help="What the target achievement of this plan produces at settlement time: "
             "an extra deduction taken from the vendor's settlement. Commission plans "
             "never pay the vendor a bonus.")
    target_amount = fields.Monetary(currency_field="currency_id", help="Order amount that represents 100% completion.")
    commission_amount = fields.Monetary(
        string="On Target Commission", currency_field="currency_id",
        help="Commission paid when the target reaches 100%.", default=1000,
    )
    on_target_commission = fields.Monetary(
        related="commission_amount", string="On Target Commission", currency_field="currency_id",
    )
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)
    achieve_type = fields.Selection([
        ("amount_sold", "Amount Sold"), ("qty_sold", "Quantity Sold"),
        ("order_count", "Order Count"), ("listing_days", "Listing Days"),
    ], string="Metric", help="Metric this plan pays or deducts on. Used when Based on = Achievements.")
    achieve_is_tiered = fields.Boolean(
        string="Use Target Tiers",
        help="Pay a fixed amount per completion tier of a target for this metric "
             "(the Completion Tiers below), instead of a flat rate per unit.",
    )
    achieve_rate = fields.Float(
        default=0.05, help="Commission per unit of the metric. Used when not tiered.",
    )
    achieve_target_amount = fields.Monetary(
        currency_field="currency_id",
        help="Metric value that represents 100% completion. Used when tiered.",
    )
    date_from = fields.Date()
    date_to = fields.Date()
    commission_line_ids = fields.One2many("leaders.commission.line", "rule_id")
    achievement_ids = fields.One2many("leaders.commission.achievement", "rule_id")
    # Legacy tier model of the pre-4.0.5 plan; superseded by target_commission_ids.
    plan_line_ids = fields.One2many("leaders.commission.plan.line", "rule_id", copy=True)
    target_commission_ids = fields.One2many("leaders.commission.target.commission", "plan_id", copy=True)
    target_ids = fields.One2many("leaders.commission.target", "plan_id", copy=True)
    vendor_plan_ids = fields.One2many("leaders.commission.plan.vendor", "plan_id", copy=True)
    commission_order_count = fields.Integer(compute="_compute_commission_stats")
    total_commission_amount = fields.Monetary(compute="_compute_commission_stats", currency_field="currency_id")
    chart_html = fields.Html(compute="_compute_chart_html", sanitize=False)
    target_commission_graph = fields.Text(compute="_compute_target_commission_graph")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("vendor_id") and not vals.get("vendor_ids"):
                vals["vendor_ids"] = [(6, 0, [vals["vendor_id"]])]
        plans = super().create(vals_list)
        plans._sync_target_periods()
        for plan in plans.filtered(
            lambda record: not record.target_commission_ids
            and (record.type == "target" or record.achieve_is_tiered)
        ):
            plan.target_commission_ids = [
                fields.Command.create({"target_rate": 0, "amount": 0}),
                fields.Command.create({"target_rate": 0.5, "amount": 0}),
                fields.Command.create({"target_rate": 1, "amount": plan.commission_amount or 1}),
            ]
        return plans

    def write(self, vals):
        result = super().write(vals)
        if {"date_from", "date_to", "periodicity", "target_amount"}.intersection(vals):
            self._sync_target_periods()
        return result

    def _sync_target_periods(self):
        for plan in self:
            if not plan.date_from or not plan.date_to:
                continue
            step = {
                "month": relativedelta(months=1),
                "quarter": relativedelta(months=3),
                "year": relativedelta(years=1),
            }[plan.periodicity]
            current = plan.date_from
            commands = [fields.Command.clear()]
            while current <= plan.date_to:
                end = min(current + step - relativedelta(days=1), plan.date_to)
                commands.append(fields.Command.create({
                    "name": plan._period_name(current),
                    "date_from": current,
                    "date_to": end,
                    "amount": plan.target_amount,
                }))
                current += step
            plan.target_ids = commands

    def _period_name(self, start):
        """Label of the target period that begins on ``start``."""
        self.ensure_one()
        if self.periodicity == "month":
            return start.strftime("%b %Y")
        if self.periodicity == "quarter":
            return "Q%s %s" % ((start.month - 1) // 3 + 1, start.year)
        return str(start.year)

    @api.depends("achievement_ids.commission_amount", "achievement_ids.vendor_id")
    def _compute_commission_stats(self):
        for record in self:
            record.commission_order_count = len(record.achievement_ids)
            record.total_commission_amount = sum(record.achievement_ids.mapped("commission_amount"))

    # Legacy server-rendered chart of plan_line_ids; the form uses the
    # commission_graph widget on target_commission_graph instead.
    @api.depends("plan_line_ids.target_completion", "plan_line_ids.commission_amount")
    def _compute_chart_html(self):
        for record in self:
            lines = record.plan_line_ids.sorted("target_completion")
            if not lines:
                record.chart_html = "<div class='text-muted p-3'>Add commission tiers to see the chart.</div>"
                continue
            width, height = 640, 260
            left, top, right, bottom = 52, 18, 18, 42
            chart_width = width - left - right
            chart_height = height - top - bottom
            max_x = max(lines.mapped("max_amount") or [100.0])
            max_y = max(
                [line.max_amount * line.commission_percentage / 100.0 for line in lines]
                or [1.0]
            )
            max_x = max(max_x, 100.0)
            max_y = max(max_y, 1.0)

            def point(line):
                x = left + (line.max_amount / max_x) * chart_width
                amount = line.max_amount * line.commission_percentage / 100.0
                y = top + chart_height - (amount / max_y) * chart_height
                return x, y

            points = [point(line) for line in lines]
            polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
            grid = []
            for step in range(0, 6):
                y = top + chart_height - (step / 5) * chart_height
                value = max_y * step / 5
                grid.append(
                    f"<line x1='{left}' y1='{y:.1f}' x2='{width - right}' y2='{y:.1f}' "
                    "stroke='#e5e7eb' stroke-width='1'/><text x='4' y='"
                    f"{y + 4:.1f}' fill='#6b7280' font-size='11'>{escape(f'{value:,.0f}')}</text>"
                )
            labels = []
            dots = []
            for line, (x, y) in zip(lines, points):
                labels.append(
                    f"<text x='{x:.1f}' y='{height - 16}' text-anchor='middle' "
                    f"fill='#6b7280' font-size='11'>{line.max_amount:g}</text>"
                )
                dots.append(
                    f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4' fill='#714B67' "
                    f"stroke='white' stroke-width='2'><title>{line.commission_percentage:g}%</title></circle>"
                )
            area_points = f"{left},{top + chart_height} {polyline} {width - right},{top + chart_height}"
            chart_label = escape(_("Commission plan chart"))
            record.chart_html = (
                "<div class='lm-commission-chart'>"
                f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{chart_label}'>"
                f"{''.join(grid)}<line x1='{left}' y1='{top}' x2='{left}' y2='{top + chart_height}' stroke='#9ca3af'/>"
                f"<line x1='{left}' y1='{top + chart_height}' x2='{width - right}' y2='{top + chart_height}' stroke='#9ca3af'/>"
                f"<polygon points='{area_points}' fill='#8fb3f4' fill-opacity='0.7'/><polyline points='{polyline}' fill='none' stroke='#3973d6' stroke-width='3' stroke-linejoin='round'/>"
                f"{''.join(dots)}{''.join(labels)}</svg></div>"
            )

    @api.constrains("type", "achieve_type", "achieve_is_tiered")
    def _check_achieve_type(self):
        for plan in self:
            if plan.type != "achieve":
                continue
            if not plan.achieve_type:
                raise ValidationError(_("Pick a metric for this achievement plan."))
            if plan.achieve_type == "listing_days" and plan.achieve_is_tiered:
                raise ValidationError(_("Listing Days cannot use target tiers; it only supports a flat rate."))

    @api.constrains("vendor_id", "vendor_ids", "vendor_plan_ids", "state", "date_from", "date_to", "scope")
    def _check_one_plan_per_vendor(self):
        """A vendor is covered by exactly one active commission plan at a time.

        Whichever single plan they're put in decides their whole commission
        (its own metric, rate or tiers) -- they can't be added to a second
        plan, even for a different metric, while one is already active for
        an overlapping period.
        """
        for plan in self:
            if plan.scope != "vendor" or plan.state not in ("approved", "done"):
                continue
            vendors = (plan.vendor_id | plan.vendor_ids | plan.vendor_plan_ids.vendor_id).mapped(
                "commercial_partner_id")
            if not vendors:
                continue
            candidates = self.search([
                ("id", "!=", plan.id), ("scope", "=", "vendor"), ("state", "in", ("approved", "done")),
            ])
            for other in candidates:
                other_vendors = (other.vendor_id | other.vendor_ids | other.vendor_plan_ids.vendor_id).mapped(
                    "commercial_partner_id")
                overlap = vendors & other_vendors
                if not overlap:
                    continue
                starts_before_other_ends = not plan.date_from or not other.date_to or plan.date_from <= other.date_to
                ends_after_other_starts = not plan.date_to or not other.date_from or plan.date_to >= other.date_from
                if starts_before_other_ends and ends_after_other_starts:
                    raise ValidationError(_(
                        "%(vendor)s is already covered by another active commission plan (%(plan)s) for an "
                        "overlapping period. A vendor can only be in one commission plan at a time.",
                        vendor=overlap[:1].display_name, plan=other.name))

    def action_approve(self):
        self._leaders_check_backoffice("finance")
        self.write({"state": "approved"})

    def action_mark_done(self):
        self._leaders_check_backoffice("finance")
        self.write({"state": "done"})

    def action_reset_to_draft(self):
        self._leaders_check_backoffice("finance")
        self.write({"state": "draft"})

    def action_cancel(self):
        self._leaders_check_backoffice("finance")
        self.write({"state": "cancel"})

    @api.depends("target_commission_ids.target_rate", "target_commission_ids.amount", "currency_id")
    def _compute_target_commission_graph(self):
        for plan in self:
            values = [
                {"x": round(line.target_rate * 100), "y": line.amount}
                for line in plan.target_commission_ids.sorted("target_rate")
            ]
            if values:
                values.append({"x": values[-1]["x"] + 50, "y": values[-1]["y"]})
            plan.target_commission_graph = json.dumps({
                "values": values,
                "currency": plan.currency_id.symbol,
            })

    def action_view_commissions(self):
        """Periods this plan already paid out, one record per settlement."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Plan Achievements",
            "res_model": "leaders.commission.achievement",
            "view_mode": "list,form",
            "domain": [("rule_id", "=", self.id)],
            "context": {"default_rule_id": self.id},
        }

    def action_view_commission_chart(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Commission Chart",
            "res_model": "leaders.commission.plan.line",
            "view_mode": "graph,list",
            "views": [
                (self.env.ref("leaders_marketplace.view_commission_plan_line_graph").id, "graph"),
                (False, "list"),
            ],
            "domain": [("rule_id", "=", self.id)],
        }

    @api.model
    def applicable_vendor_rule(self, vendor_order):
        """Commission rule that prices this vendor order, if the vendor has one."""
        return self._vendor_rule_at(
            vendor_order.vendor_id, fields.Date.to_date(vendor_order.date_order))

    @api.model
    def _vendor_rule_at(self, vendor, date=None):
        """Highest-priority commission rule of ``vendor`` in force on ``date``."""
        date = date or fields.Date.context_today(self)
        vendor = vendor.commercial_partner_id
        rules = self.search([
            ("active", "=", True),
            ("scope", "=", "vendor"),
            "|", ("vendor_id", "=", vendor.id), ("vendor_ids", "in", vendor.id),
            "|", ("date_from", "=", False), ("date_from", "<=", date),
            "|", ("date_to", "=", False), ("date_to", ">=", date),
        ])
        return rules[:1]

    @api.model
    def _marketplace_default_percent(self, vendor):
        """Commission percentage when the vendor has no dedicated rule.

        Falls back to the vendor tier rate, then to the marketplace default.
        """
        tier_rate = vendor.commercial_partner_id.vendor_tier_id.commission_rate
        if tier_rate:
            return tier_rate
        param = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.default_commission_rate", 0.0)
        try:
            return float(param or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @api.model
    def marketplace_rate_for_vendor(self, vendor, date=None):
        """Effective percentage the marketplace keeps, for display to the vendor.

        Fixed and compound rules have no single percentage, so only their
        percentage part is reported; the rule itself remains authoritative.
        """
        rule = self._vendor_rule_at(vendor, date)
        if not rule:
            return self._marketplace_default_percent(vendor)
        if rule.rule_type == "fixed":
            return 0.0
        return rule.percent

    @api.model
    def marketplace_commission_on(self, vendor, amount, date=None):
        """Commission the marketplace would keep on ``amount`` sold by ``vendor``."""
        rule = self._vendor_rule_at(vendor, date)
        if not rule:
            return amount * self._marketplace_default_percent(vendor) / 100.0
        if rule.rule_type == "fixed":
            return rule.fixed_amount
        commission = amount * rule.percent / 100.0
        if rule.rule_type == "compound":
            commission += rule.fixed_amount
        return commission

    @api.model
    def applicable_vendor_plan(self, vendor, period_from, period_to):
        """Plan (at most one, see ``_check_one_plan_per_vendor``) that prices

        ``vendor`` over the settlement period. The vendor may be attached
        either through the Sales People tab (``vendor_plan_ids``) or through
        the legacy vendor fields. Returned as a recordset (0 or 1 record) so
        the settlement can apply it the same way regardless.
        """
        vendor = vendor.commercial_partner_id
        return self.search([
            ("active", "=", True), ("scope", "=", "vendor"),
            ("state", "in", ("approved", "done")),
            "|", "|", ("vendor_plan_ids.vendor_id", "=", vendor.id),
            ("vendor_ids", "in", vendor.id), ("vendor_id", "=", vendor.id),
            "|", ("date_from", "=", False), ("date_from", "<=", period_to),
            "|", ("date_to", "=", False), ("date_to", ">=", period_from),
        ])

    def _period_target_amount(self, period_from, period_to):
        """Target of the plan period overlapping the settlement period."""
        self.ensure_one()
        target = self.target_ids.filtered(
            lambda period: period.date_from <= period_to and period.date_to >= period_from
        )[:1]
        return target.amount if target else self.target_amount

    def _target_commission_for(self, completion):
        """Highest commission level reached by ``completion`` (1.0 == 100%)."""
        self.ensure_one()
        return self.target_commission_ids.filtered(
            lambda line: line.target_rate <= completion
        )[-1:]

    def _achieve_metric_value(self, vendor, vendor_orders, period_from, period_to):
        """Raw value reached by this plan's metric over the settlement period."""
        self.ensure_one()
        if self.achieve_type == "order_count":
            # A fully-returned order kept nothing sold, so it no longer counts.
            orders = vendor_orders.filtered(lambda o: o._net_qty_sold() > 0)
            return float(len(orders))
        if self.achieve_type == "listing_days":
            domain = [
                ("vendor_id", "=", vendor.id),
                ("date", ">=", period_from), ("date", "<=", period_to),
            ]
            return float(self.env["leaders.marketplace.offer.publish.log"].search_count(domain))
        matched = vendor_orders.line_ids.filtered(lambda line: not line.is_delivery)
        if self.achieve_type == "qty_sold":
            return sum(max(line.qty_kept - line.qty_returned, 0.0) for line in matched)
        # amount_sold: net the subtotal by the same returned-quantity ratio the
        # return itself was refunded on, so a partial return only reduces its share.
        # ``price_subtotal`` is already on the kept quantity (a cancelled remainder is gone).
        total = 0.0
        for line in matched:
            ratio = (line.qty_kept - line.qty_returned) / line.qty_kept if line.qty_kept else 0.0
            total += line.price_subtotal * max(ratio, 0.0)
        return total

    def _achievement_commission(self, vendor, vendor_orders, period_from, period_to):
        """Commission of an achievement plan: a flat rate per unit of the metric,

        or a target for it with its own completion tiers.
        Returns ``(amount, target_amount, completion, target_commission)``, matching
        the shape :meth:`_period_commission` uses for a target-type plan.
        """
        self.ensure_one()
        value = self._achieve_metric_value(vendor, vendor_orders, period_from, period_to)
        if not self.achieve_is_tiered:
            return value * self.achieve_rate, 0.0, 0.0, self.env["leaders.commission.target.commission"]
        completion = value / self.achieve_target_amount if self.achieve_target_amount else 0.0
        tier = self.target_commission_ids.filtered(lambda t: t.target_rate <= completion).sorted("target_rate")[-1:]
        return (tier.amount if tier else 0.0), self.achieve_target_amount, completion, tier

    def _cumulative_period_commission(self, vendor, cumulative_sales, vendor_orders,
                                      window_from, window_to, target):
        """Outcome of the plan for one *plan target period*, cumulatively.

        Same shape as :meth:`_period_commission`, but the metric covers the
        target period from its start rather than one settlement. The caller
        subtracts what earlier settlements already charged for the same target,
        so a tier is billed once no matter how the vendor slices settlements.
        """
        self.ensure_one()
        if self.type == "achieve":
            return self._achievement_commission(vendor, vendor_orders, window_from, window_to)
        target_amount = target.amount if target else self._period_target_amount(window_from, window_to)
        completion = cumulative_sales / target_amount if target_amount else 0.0
        tier = self._target_commission_for(completion)
        return (tier.amount if tier else 0.0), target_amount, completion, tier

    def _true_up_target_periods(self, period_from, period_to):
        """Plan target periods overlapping a settlement, oldest first.

        Empty when the plan has no target periods, or when it is a flat-rate
        achievement plan -- both keep the settlement-by-settlement calculation.
        """
        self.ensure_one()
        if self.type == "achieve" and not self.achieve_is_tiered:
            return self.env["leaders.commission.target"]
        return self.target_ids.filtered(
            lambda t: t.date_from <= period_to and t.date_to >= period_from
        ).sorted("date_from")

    def _period_commission(self, vendor, sales_amount, vendor_orders, period_from, period_to):
        """Outcome of the plan for one settlement period.

        Returns ``(amount, target_amount, completion, target_commission)``.
        ``amount`` is always positive; it is deducted from the vendor's
        settlement (see :attr:`plan_effect`).
        """
        self.ensure_one()
        if self.type == "achieve":
            return self._achievement_commission(vendor, vendor_orders, period_from, period_to)
        target_amount = self._period_target_amount(period_from, period_to)
        completion = sales_amount / target_amount if target_amount else 0.0
        tier = self._target_commission_for(completion)
        return (tier.amount if tier else 0.0), target_amount, completion, tier

class CommissionLine(models.Model):
    _name = "leaders.commission.line"
    _description = "Marketplace Commission Line"
    _order = "id desc"

    vendor_order_id = fields.Many2one("leaders.vendor.order", required=True, ondelete="cascade", index=True)
    vendor_line_id = fields.Many2one("leaders.vendor.order.line", ondelete="cascade", index=True)
    vendor_id = fields.Many2one(related="vendor_order_id.vendor_id", store=True, index=True)
    rule_id = fields.Many2one("leaders.commission.rule")
    currency_id = fields.Many2one(related="vendor_order_id.currency_id", store=True)
    base_amount = fields.Monetary(currency_field="currency_id")
    commission_amount = fields.Monetary(currency_field="currency_id")
    vendor_amount = fields.Monetary(currency_field="currency_id")
    state = fields.Selection([
        ("hold","On Hold"),("available","Available"),("settled","Settled"),("cancelled","Cancelled")
    ], default="hold", index=True)

    @api.model
    def generate_for_vendor_order(self, vendor_order):
        # Plans are evaluated once per settlement period. Orders stay gross.
        return True

    @api.model
    def _refund_for_return(self, vendor_return):
        """Keep a return audit line; period plans are recalculated at settlement."""
        for return_line in vendor_return.line_ids:
            if not return_line.refund_amount:
                continue
            self.create({
                "vendor_order_id": vendor_return.vendor_order_id.id,
                "vendor_line_id": return_line.vendor_line_id.id,
                "base_amount": -return_line.refund_amount,
                "commission_amount": 0.0,
                "vendor_amount": -return_line.refund_amount,
                "state": "hold",
            })


class CommissionPlanLine(models.Model):
    _name = "leaders.commission.plan.line"
    _description = "Vendor Commission Plan Tier"
    _order = "target_completion, id"

    rule_id = fields.Many2one("leaders.commission.rule", required=True, ondelete="cascade")
    name = fields.Char(string="Commission Tier")
    min_amount = fields.Monetary(string="Minimum Amount", required=True, currency_field="currency_id")
    max_amount = fields.Monetary(string="Maximum Amount", required=True, currency_field="currency_id")
    commission_percentage = fields.Float(string="Commission Percentage", default=0.0, required=True)
    target_completion = fields.Float(string="Target completion (%)")
    commission_amount = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one(related="rule_id.currency_id", store=True)

    @api.constrains("min_amount", "max_amount", "commission_percentage")
    def _check_range(self):
        for line in self:
            if line.min_amount > line.max_amount:
                raise ValidationError(_("The minimum amount cannot exceed the maximum amount."))
            if line.commission_percentage < 0 or line.commission_percentage > 100:
                raise ValidationError(_("Commission percentage must be between 0 and 100."))


class CommissionTarget(models.Model):
    _name = "leaders.commission.target"
    _description = "Commission Plan Target"
    _order = "date_from, id"

    plan_id = fields.Many2one("leaders.commission.rule", required=True, ondelete="cascade")
    name = fields.Char(required=True, readonly=True)
    date_from = fields.Date(required=True, readonly=True)
    date_to = fields.Date(required=True, readonly=True)
    amount = fields.Monetary(default=0, required=True, currency_field="currency_id")
    currency_id = fields.Many2one(related="plan_id.currency_id", store=True)


class CommissionTargetCommission(models.Model):
    _name = "leaders.commission.target.commission"
    _description = "Commission Plan Target Commission"
    _order = "target_rate, id"

    plan_id = fields.Many2one("leaders.commission.rule", required=True, ondelete="cascade")
    target_rate = fields.Float("Target completion (%)", default=1, required=True)
    amount_rate = fields.Float("OTC %", help="On Target Commission rate")
    amount = fields.Monetary("Commission", default=0, required=True, currency_field="currency_id")
    currency_id = fields.Many2one(related="plan_id.currency_id", store=True)

    @api.onchange("amount")
    def _onchange_amount(self):
        for record in self:
            record.amount_rate = record.plan_id.commission_amount and record.amount / record.plan_id.commission_amount or 1

    @api.onchange("amount_rate")
    def _onchange_amount_rate(self):
        for record in self:
            record.amount = record.plan_id.commission_amount * record.amount_rate


class CommissionPlanVendor(models.Model):
    _name = "leaders.commission.plan.vendor"
    _description = "Commission Plan Vendor"
    _order = "id"

    plan_id = fields.Many2one("leaders.commission.rule", required=True, ondelete="cascade")
    vendor_id = fields.Many2one(
        "res.partner", string="Vendor", required=True,
        domain="[('is_marketplace_vendor', '=', True)]",
    )
    date_from = fields.Date("From", related="plan_id.date_from", store=True, readonly=False)
    date_to = fields.Date("To", related="plan_id.date_to", store=True, readonly=False)
    other_plans = fields.Many2many("leaders.commission.rule", compute="_compute_other_plans")

    _sql_constraints = [
        ("vendor_uniq", "unique(plan_id, vendor_id)", "The vendor is already present in the plan."),
    ]

    @api.depends("vendor_id", "plan_id")
    def _compute_other_plans(self):
        for record in self:
            record.other_plans = self.env["leaders.commission.rule"].search([
                ("id", "!=", record.plan_id.id),
                ("vendor_plan_ids.vendor_id", "=", record.vendor_id.id),
                ("state", "in", ("draft", "approved")),
            ])


class CommissionAchievement(models.Model):
    _name = "leaders.commission.achievement"
    _description = "Vendor Commission Plan Achievement"
    _order = "period_to desc, id desc"

    rule_id = fields.Many2one("leaders.commission.rule", required=True, ondelete="restrict", index=True)
    vendor_id = fields.Many2one("res.partner", required=True, index=True)
    settlement_id = fields.Many2one("leaders.settlement", required=True, ondelete="restrict", index=True)
    period_from = fields.Date(required=True)
    period_to = fields.Date(required=True)
    sales_amount = fields.Monetary(required=True, currency_field="currency_id")
    target_amount = fields.Monetary(required=True, currency_field="currency_id")
    achievement_percent = fields.Float(string="Achievement (%)", required=True)
    target_commission_id = fields.Many2one(
        "leaders.commission.target.commission", string="Achieved Level", ondelete="set null")
    target_id = fields.Many2one(
        "leaders.commission.target", string="Plan Target Period", index=True, ondelete="set null",
        help="Plan target period this row trues up. Empty for plans without "
             "target periods and for flat-rate achievement plans, which stay "
             "settlement-by-settlement.")
    cumulative_sales = fields.Monetary(
        currency_field="currency_id", string="Cumulative Sales",
        help="Metric accumulated from the start of the target period up to the "
             "end of this settlement -- what the tier is judged on.")
    already_charged = fields.Monetary(
        currency_field="currency_id", string="Already Charged",
        help="Commission already taken for the same plan target period in "
             "earlier settlements; subtracted so a tier is only charged once.")
    cancelled = fields.Boolean(
        default=False, index=True,
        help="Set when the settlement that produced this row was cancelled. "
             "Cancelled rows are ignored by the cumulative true-up, so the "
             "commission they charged can be taken again.")
    effect = fields.Selection(related="rule_id.plan_effect", store=True)
    # Legacy tier of the pre-4.0.5 plan model; kept readable for old records.
    plan_line_id = fields.Many2one("leaders.commission.plan.line", string="Achieved Tier")
    commission_amount = fields.Monetary(required=True, currency_field="currency_id")
    currency_id = fields.Many2one(related="rule_id.currency_id", store=True)
