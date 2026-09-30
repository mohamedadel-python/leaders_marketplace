from datetime import timedelta

from dateutil.relativedelta import relativedelta
from odoo import api, fields, models, _
from odoo.exceptions import UserError

class VendorReserve(models.Model):
    """Amount held back from a vendor earning to cover potential returns.

    Released back to the vendor once the return window passes, or consumed
    when a return is processed after settlement.
    """
    _name = "leaders.vendor.reserve"
    _description = "Vendor Returns Reserve"
    _order = "id desc"

    vendor_id = fields.Many2one("res.partner", required=True, index=True)
    vendor_order_id = fields.Many2one("leaders.vendor.order", index=True)
    amount = fields.Monetary(required=True, currency_field="currency_id")
    consumed_amount = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", required=True)
    state = fields.Selection([
        ("held", "Held"), ("released", "Released"), ("consumed", "Consumed")
    ], default="held", index=True)
    transaction_hold_id = fields.Many2one(
        "leaders.wallet.transaction", string="Hold Transaction", copy=False, readonly=True,
        help="Negative wallet transaction that withholds this reserve.")
    transaction_release_id = fields.Many2one(
        "leaders.wallet.transaction", string="Release Transaction", copy=False, readonly=True,
        help="Positive wallet transaction that pays the reserve back.")
    release_date = fields.Date(
        compute="_compute_release_date",
        help="When the marketplace stops holding this back: the shipment date "
             "plus the customer return window plus the vendor response time.")

    @api.depends("vendor_order_id.date_shipped")
    def _compute_release_date(self):
        days = self._leaders_reserve_hold_days()
        for reserve in self:
            shipped = reserve.vendor_order_id.date_shipped
            reserve.release_date = (
                fields.Date.to_date(shipped) + timedelta(days=days)) if shipped else False

    @api.model
    def _leaders_reserve_hold_days(self):
        """Return window plus the vendor's response time, in days."""
        get = self.env["ir.config_parameter"].sudo().get_param
        def days(key, default):
            try:
                return int(get("leaders_marketplace.%s" % key, default) or 0)
            except (TypeError, ValueError):
                return default
        return days("return_window_days", 14) + days("return_vendor_sla_days", 3)

    @api.model
    def _leaders_reserve_enforced(self):
        """Whether reserves actually withhold money (off by default)."""
        param = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.enforce_returns_reserve")
        return str(param).strip().lower() not in ("", "false", "0", "none")

    def action_release(self):
        self.write({"state": "released"})

    def _leaders_pay_back(self):
        """Give the vendor back whatever the returns did not eat.

        The hold transaction took the whole reserve out of the vendor's
        available balance; this pays back what is left of it. A reserve fully
        consumed by returns is simply closed.
        """
        Wallet = self.env["leaders.wallet.transaction"]
        for reserve in self:
            if reserve.state != "held":
                continue
            left = reserve.amount - reserve.consumed_amount
            if reserve.transaction_hold_id and reserve.currency_id.compare_amounts(left, 0) > 0:
                reserve.transaction_release_id = Wallet.create({
                    "vendor_id": reserve.vendor_id.id,
                    "vendor_order_id": reserve.vendor_order_id.id,
                    "type": "reserve",
                    "amount": left,
                    "currency_id": reserve.currency_id.id,
                    "available_date": fields.Date.context_today(self),
                    "state": "available",
                    "reserve_id": reserve.id,
                    "reference": _("Reserve released %s", reserve.vendor_order_id.name or ""),
                })
            reserve.state = "released"
        return True

    @api.model
    def cron_release_reserves(self):
        """Release reserves whose return window has closed with no open return.

        Does nothing while the reserve is not enforced, since then no money was
        withheld and there is nothing to pay back.
        """
        if not self._leaders_reserve_enforced():
            return False
        deadline = fields.Datetime.now() - timedelta(days=self._leaders_reserve_hold_days())
        candidates = self.search([
            ("state", "=", "held"),
            ("vendor_order_id.date_shipped", "!=", False),
            ("vendor_order_id.date_shipped", "<=", deadline),
        ])
        open_states = ("requested", "approved", "escalated")
        blocked = set(self.env["leaders.vendor.return"].sudo().search([
            ("vendor_order_id", "in", candidates.vendor_order_id.ids),
            ("state", "in", open_states),
        ]).mapped("vendor_order_id").ids)
        candidates.filtered(
            lambda r: r.vendor_order_id.id not in blocked)._leaders_pay_back()
        return True

    @api.model
    def _consume_for_return(self, vendor_return):
        """Use the vendor's held reserves (oldest first) to cover a return deduction."""
        remaining = vendor_return.vendor_deduction
        reserves = self.search([
            ("vendor_id", "=", vendor_return.vendor_id.id), ("state", "=", "held"),
        ], order="id asc")
        for reserve in reserves:
            if remaining <= 0:
                break
            available = reserve.amount - reserve.consumed_amount
            used = min(available, remaining)
            reserve.consumed_amount += used
            remaining -= used
            if reserve.consumed_amount >= reserve.amount:
                reserve.state = "consumed"
        return vendor_return.vendor_deduction - max(remaining, 0.0)


class WalletTransaction(models.Model):
    _name = "leaders.wallet.transaction"
    _description = "Vendor Wallet Transaction"
    _order = "id desc"

    vendor_id = fields.Many2one("res.partner", required=True, index=True)
    vendor_order_id = fields.Many2one("leaders.vendor.order", index=True)
    type = fields.Selection([
        ("earning","Earning"),("commission","Commission"),("settlement","Settlement"),
        ("bonus","Target Bonus"),
        ("return","Return"),("penalty","Penalty"),("fulfillment_fee","Fulfillment Fee"),
        ("storage_fee","Storage Fee"),("adjustment","Adjustment"),
        ("fee_tax","Tax on Marketplace Fees"),
        ("cod_collected","COD Collected (Vendor Held)"),
        ("reserve","Returns Reserve"),
        ("cancelled_qty","Cancelled Quantity"),
    ], required=True)
    amount = fields.Monetary(required=True, currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", required=True)
    available_date = fields.Date()
    state = fields.Selection([("hold","On Hold"),("available","Available"),("settled","Settled")], default="hold", index=True)
    reference = fields.Char()
    settlement_id = fields.Many2one("leaders.settlement", ondelete="set null")
    note = fields.Text()
    move_id = fields.Many2one(
        "account.move", string="Journal Entry", readonly=True, copy=False,
        help="Entry booked for a fee or adjustment, so the vendor payable "
             "account follows the wallet.")
    reserve_id = fields.Many2one(
        "leaders.vendor.reserve", string="Returns Reserve", index=True, copy=False,
        help="Reserve this transaction holds back or pays out.")

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        Accounting = self.env["leaders.marketplace.accounting"]
        # Fees and adjustments move money between the marketplace and the vendor
        # with no other document behind them, so they carry their own entry.
        for record in records.filtered(
                lambda t: t.type in Accounting._LEADERS_FEE_TYPES):
            Accounting.post_vendor_fee_entry(record)
        return records

    def write(self, vals):
        forbidden = {"vendor_id","vendor_order_id","type","amount","currency_id"}
        if forbidden.intersection(vals) and any(rec.id for rec in self):
            raise UserError(_("Wallet transactions are immutable. Create an adjustment instead."))
        return super().write(vals)

    def unlink(self):
        raise UserError(_("Wallet transactions cannot be deleted."))

    @api.model
    def create_vendor_earning(self, vendor_order):
        if self.search_count([("vendor_order_id","=",vendor_order.id),("type","=","earning")]):
            return False
        # Make sure fulfilment fees and commission are up to date before earning.
        vendor_order._apply_fulfillment_fees()
        vendor_order._recompute_amounts()

        tier = vendor_order.vendor_id.vendor_tier_id
        # Escrow release policy is per tier: on_ship / on_deliver / deliver_plus_days.
        policy = getattr(tier, "escrow_release_policy", False) or "deliver_plus_days"
        if policy == "on_ship" or policy == "on_deliver":
            available_date = fields.Date.today()
        else:
            days = getattr(tier, "escrow_release_days", 0) or vendor_order.vendor_id.vendor_hold_days or 14
            available_date = fields.Date.today() + timedelta(days=days)

        earning = self.create({
            "vendor_id":vendor_order.vendor_id.id,
            "vendor_order_id":vendor_order.id,
            "type":"earning",
            "amount":vendor_order.vendor_net,
            "currency_id":vendor_order.currency_id.id,
            "available_date":available_date,
            "state":"hold",
            "reference":vendor_order.name
        })
        # Reserve for potential returns, held back per tier reserve_percent.
        reserve_pct = getattr(tier, "reserve_percent", 0.0) or 0.0
        if reserve_pct:
            Reserve = self.env["leaders.vendor.reserve"].sudo()
            reserve_amount = vendor_order.vendor_net * reserve_pct / 100.0
            reserve = Reserve.create({
                "vendor_id": vendor_order.vendor_id.id,
                "vendor_order_id": vendor_order.id,
                "amount": reserve_amount,
                "currency_id": vendor_order.currency_id.id,
            })
            # Off by default: historically the reserve was recorded but nothing
            # was ever withheld, so the vendor received the full earning.
            if Reserve._leaders_reserve_enforced() and reserve_amount:
                reserve.transaction_hold_id = self.create({
                    "vendor_id": vendor_order.vendor_id.id,
                    "vendor_order_id": vendor_order.id,
                    "type": "reserve",
                    "amount": -reserve_amount,
                    "currency_id": vendor_order.currency_id.id,
                    "available_date": available_date,
                    "state": "hold",
                    "reserve_id": reserve.id,
                    "reference": _("Returns reserve %s", vendor_order.name),
                })
        # Post the REAL journal entry (escrow -> commission/fee income + vendor payable).
        self.env["leaders.marketplace.accounting"].post_earning_entry(vendor_order)
        return earning

    @api.model
    def create_vendor_cod_due(self, vendor_order):
        """FBM + Cash on Delivery: the vendor already collected the cash directly,
        so nothing is held back -- it's theirs. Their commission/fee is settled
        against this at the period settlement instead of an escrow release."""
        if self.search_count([("vendor_order_id","=",vendor_order.id),("type","=","cod_collected")]):
            return False
        vendor_order._apply_fulfillment_fees()
        vendor_order._recompute_amounts()
        due = self.create({
            "vendor_id": vendor_order.vendor_id.id,
            "vendor_order_id": vendor_order.id,
            "type": "cod_collected",
            "amount": vendor_order.vendor_net,
            "currency_id": vendor_order.currency_id.id,
            "available_date": fields.Date.today(),
            "state": "available",
            "reference": vendor_order.name,
        })
        # Only posts a journal entry when a per-order commission/fee actually
        # applies (the normal case defers commission to the settlement period).
        self.env["leaders.marketplace.accounting"].post_cod_commission_entry(vendor_order)
        return due

    @api.model
    def cron_release_holds(self):
        self.search([
            ("state","=","hold"),("available_date","!=",False),
            ("available_date","<=",fields.Date.today())
        ]).write({"state":"available"})

class Settlement(models.Model):
    _name = "leaders.settlement"
    _description = "Vendor Settlement"
    _inherit = ["mail.thread", "leaders.backoffice.mixin"]
    _order = "id desc"

    name = fields.Char(default="New", copy=False, readonly=True)
    vendor_id = fields.Many2one("res.partner", required=True, index=True)
    period_from = fields.Date(required=True)
    period_to = fields.Date(required=True)
    transaction_ids = fields.One2many("leaders.wallet.transaction","settlement_id")
    currency_id = fields.Many2one("res.currency", required=True, default=lambda self: self.env.company.currency_id)
    gross_amount = fields.Monetary(compute="_compute_amounts", currency_field="currency_id", store=True)
    deductions = fields.Monetary(compute="_compute_amounts", currency_field="currency_id", store=True)
    net_amount = fields.Monetary(compute="_compute_amounts", currency_field="currency_id", store=True)
    vendor_bill_id = fields.Many2one("account.move")
    payment_id = fields.Many2one("account.payment")
    commission_move_id = fields.Many2one("account.move", string="Commission Journal Entry", copy=False)
    commission_invoice_offset_move_id = fields.Many2one(
        "account.move", string="Invoice Netting Entry", copy=False, readonly=True,
        help="Reclassification Dr Vendor payable / Cr Customer receivable that "
             "settles the marketplace invoice against what the marketplace owes the vendor.")
    commission_invoice_id = fields.Many2one(
        "account.move", string="Marketplace Invoice", copy=False, readonly=True,
        help="Tax invoice issued to the vendor for the commission and fees of this "
             "settlement (setting: Commission Invoicing = invoice).")
    commission_achievement_ids = fields.One2many("leaders.commission.achievement", "settlement_id", copy=False)
    payment_move_id = fields.Many2one("account.move", string="Payout Journal Entry", copy=False)
    payout_journal_id = fields.Many2one(
        "account.journal", string="Payout Journal",
        domain="[('type', 'in', ('bank', 'cash'))]",
        help="Journal used to pay this settlement. Left empty, the first "
             "bank or cash journal of the company is used, as before.")
    payout_bank_id = fields.Many2one(
        "res.partner.bank", string="Payout Bank Account",
        related="vendor_id.commercial_partner_id.vendor_payout_bank_id", readonly=True,
        help="The vendor's current payout account.")
    state = fields.Selection([
        ("draft","Draft"),("approved","Approved"),("paid","Paid"),("cancelled","Cancelled")
    ], default="draft", tracking=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name","New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("leaders.settlement") or "New"
        return super().create(vals_list)

    @api.depends("transaction_ids.amount","transaction_ids.type")
    def _compute_amounts(self):
        for rec in self:
            # cod_collected is money the vendor already holds (FBM+COD) -- it must
            # never be summed into the payout math, only the period commission
            # deduction on it (a normal "commission" transaction) is. It stays
            # visible in transaction_ids for audit only.
            payable = rec.transaction_ids.filtered(lambda t: t.type != "cod_collected")
            pos = sum(t.amount for t in payable if t.amount > 0)
            neg = -sum(t.amount for t in payable if t.amount < 0)
            rec.gross_amount = pos
            rec.deductions = neg
            rec.net_amount = pos-neg

    def action_collect_transactions(self):
        for rec in self:
            txs = self.env["leaders.wallet.transaction"].search([
                ("vendor_id","=",rec.vendor_id.id),("state","=","available"),
                ("available_date",">=",rec.period_from),("available_date","<=",rec.period_to),
                ("settlement_id","=",False)
            ])
            txs.write({"settlement_id":rec.id})
            rec._create_period_commission(txs)

    def _create_period_commission(self, earning_transactions):
        """Apply the vendor's one commission plan for the settlement period.

        A vendor is covered by at most one active plan at a time (a target
        plan compares period sales with its target; an achievement plan
        applies a rate or target tiers to one metric). The computed amount
        is always deducted from the settlement -- commission plans never
        pay the vendor a bonus. The loop below stays plan-generic (it works
        the same whether 0 or 1 plan applies).
        """
        Commission = self.env["leaders.commission.rule"]
        Wallet = self.env["leaders.wallet.transaction"]
        for settlement in self:
            if Wallet.search_count([
                ("settlement_id", "=", settlement.id), ("type", "=", "commission"),
            ]):
                continue
            earnings = earning_transactions.filtered(
                lambda tx: tx.vendor_id == settlement.vendor_id and tx.type in ("earning", "cod_collected"))
            returns = earning_transactions.filtered(
                lambda tx: tx.vendor_id == settlement.vendor_id and tx.type in ("return", "cancelled_qty"))
            # Returns are already negative, so this nets the period's sales down
            # by whatever was given back to the customer (minus the commission
            # already refunded on it).
            sales_amount = sum(earnings.mapped("amount")) + sum(returns.mapped("amount"))
            plans = Commission.applicable_vendor_plan(
                settlement.vendor_id, settlement.period_from, settlement.period_to)
            if not plans:
                settlement._apply_no_plan_commission(sales_amount)
                continue
            for plan in plans:
                if plan.type == "target" and not plan.target_commission_ids:
                    continue
                if plan.type == "achieve" and not plan.achieve_type:
                    continue
                targets = plan._true_up_target_periods(
                    settlement.period_from, settlement.period_to)
                if not targets:
                    # No plan target period (or a flat-rate achievement plan):
                    # linear, settlement by settlement, exactly as before.
                    settlement._apply_plan_period(
                        plan, settlement.period_from, settlement.period_to,
                        sales_amount, sales_amount, earnings.vendor_order_id,
                        target=False, already_charged=0.0)
                    continue
                for target in targets:
                    settlement._apply_plan_target_period(plan, target, earning_transactions)

    def _leaders_no_plan_commission_mode(self):
        """"none" (default: a vendor without a plan pays nothing) or "rate"."""
        mode = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.no_plan_commission_mode", "none")
        return mode if mode in ("none", "rate") else "none"

    def _apply_no_plan_commission(self, sales_amount):
        """Charge the vendor's tier rate when no commission plan covers them.

        Off by default, which is how the marketplace has always behaved: no
        plan meant no commission, even though the vendor's tier advertises a
        rate. Switched to "rate", the tier percentage (or the marketplace
        default) is taken on the same netted sales the plans are judged on,
        as an ordinary negative "commission" transaction -- so the settlement
        totals and ``post_settlement_commission`` pick it up unchanged.
        """
        self.ensure_one()
        if self._leaders_no_plan_commission_mode() != "rate":
            return False
        percent = self.env["leaders.commission.rule"]._marketplace_default_percent(
            self.vendor_id)
        amount = sales_amount * percent / 100.0
        # Deduction only, like every commission plan: a period that nets out
        # negative through returns is never paid back here.
        if amount <= 0:
            return False
        return self.env["leaders.wallet.transaction"].create({
            "vendor_id": self.vendor_id.id,
            "type": "commission",
            "amount": -amount,
            "currency_id": self.currency_id.id,
            "available_date": fields.Date.context_today(self),
            "state": "available",
            "settlement_id": self.id,
            "reference": _("Marketplace rate"),
            "note": _(
                "No commission plan - tier rate %(percent).2f%% on period sales "
                "of %(sales)s.", percent=percent, sales=sales_amount),
        })

    def _apply_plan_target_period(self, plan, target, collected_transactions):
        """True up one plan target period against everything it has earned.

        The tier is judged on the metric accumulated from the target period's
        first day up to the end of this settlement -- not on whatever slice the
        vendor chose to settle -- and what earlier settlements already charged
        for the same target is subtracted. Slicing a quarter into ninety daily
        settlements therefore costs exactly what settling it once costs.
        """
        self.ensure_one()
        vendor = self.vendor_id
        window_from = target.date_from
        window_to = min(self.period_to, target.date_to)
        cumulative = self._cumulative_plan_transactions(vendor, window_from, window_to)
        cumulative_sales = sum(cumulative.mapped("amount"))
        # What this settlement itself contributed inside the target period.
        slice_txs = collected_transactions.filtered(
            lambda tx: tx.vendor_id == vendor
            and tx.type in ("earning", "cod_collected", "return", "cancelled_qty")
            and tx.available_date and window_from <= tx.available_date <= window_to)
        already = sum(self.env["leaders.commission.achievement"].search([
            ("rule_id", "=", plan.id),
            ("vendor_id", "=", vendor.id),
            ("target_id", "=", target.id),
            ("cancelled", "=", False),
            ("settlement_id.state", "!=", "cancelled"),
        ]).mapped("commission_amount"))
        self._apply_plan_period(
            plan, window_from, window_to,
            sales_amount=sum(slice_txs.mapped("amount")),
            cumulative_sales=cumulative_sales,
            # Achievement metrics count orders that earned, exactly as before;
            # a return transaction points back at an order already counted.
            vendor_orders=cumulative.filtered(
                lambda tx: tx.type in ("earning", "cod_collected")).vendor_order_id,
            target=target, already_charged=already)

    def _cumulative_plan_transactions(self, vendor, date_from, date_to):
        """Sales transactions of ``vendor`` counting toward a target period.

        Everything available inside the window that is committed to a settlement
        that still stands -- including the one being built right now, whose
        transactions were attached just before the commission runs. Money left
        behind by a cancelled settlement does not count, matching the payout math.
        """
        return self.env["leaders.wallet.transaction"].search([
            ("vendor_id", "=", vendor.id),
            ("type", "in", ("earning", "cod_collected", "return", "cancelled_qty")),
            ("available_date", ">=", date_from),
            ("available_date", "<=", date_to),
            ("settlement_id", "!=", False),
            ("settlement_id.state", "!=", "cancelled"),
        ])

    def _apply_plan_period(self, plan, period_from, period_to, sales_amount,
                           cumulative_sales, vendor_orders, target, already_charged):
        """Record one achievement row and deduct whatever is still owed."""
        self.ensure_one()
        Wallet = self.env["leaders.wallet.transaction"]
        if target:
            reached, target_amount, completion, tier = plan._cumulative_period_commission(
                self.vendor_id, cumulative_sales, vendor_orders,
                period_from, period_to, target)
        else:
            reached, target_amount, completion, tier = plan._period_commission(
                self.vendor_id, sales_amount, vendor_orders, period_from, period_to)
        # Deduction only: a tier lost to later returns is never refunded here.
        amount = max(reached - already_charged, 0.0)
        self.env["leaders.commission.achievement"].create({
            "rule_id": plan.id,
            "vendor_id": self.vendor_id.id,
            "settlement_id": self.id,
            "period_from": period_from,
            "period_to": period_to,
            "sales_amount": sales_amount,
            "target_amount": target_amount,
            "achievement_percent": completion * 100.0,
            "target_commission_id": tier.id if tier else False,
            "target_id": target.id if target else False,
            "cumulative_sales": cumulative_sales,
            "already_charged": already_charged,
            "commission_amount": amount,
        })
        if amount <= 0:
            return False
        return Wallet.create({
            "vendor_id": self.vendor_id.id,
            "type": "commission",
            "amount": -amount,
            "currency_id": self.currency_id.id,
            "available_date": fields.Date.context_today(self),
            "state": "available",
            "settlement_id": self.id,
            "reference": plan.name,
            "note": _("Period sales: %(sales)s; rate per achievement", sales=sales_amount)
            if plan.type == "achieve" and not target else _(
                "Period sales: %(sales)s; target completion: %(completion).2f%%",
                sales=cumulative_sales if target else sales_amount,
                completion=completion * 100.0,
            ),
        })

    @api.model
    def _leaders_auto_settlements_enabled(self):
        param = self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.auto_settlements")
        return str(param).strip().lower() not in ("", "false", "0", "none")

    @api.model
    def _leaders_period_for(self, frequency, today):
        """Closed period that ends the day before ``today`` for this frequency.

        Returns ``(period_from, period_to)``, or False when ``today`` is not the
        first day after a period boundary -- so a daily cron only acts once per
        period instead of every morning.
        """
        yesterday = today - timedelta(days=1)
        if frequency == "daily":
            return yesterday, yesterday
        if frequency == "weekly":
            # Monday opens a new week: settle the one that just closed.
            if today.weekday() != 0:
                return False
            return yesterday - timedelta(days=6), yesterday
        if frequency == "biweekly":
            if today.day == 16:
                return today.replace(day=1), yesterday
            if today.day == 1:
                last = today - relativedelta(months=1)
                return last.replace(day=16), yesterday
            return False
        # monthly
        if today.day != 1:
            return False
        return (today - relativedelta(months=1)).replace(day=1), yesterday

    @api.model
    def cron_auto_create_settlements(self):
        """Open a DRAFT settlement per vendor when their payout period closes.

        Off by default. Even switched on it only drafts and collects: approving
        and paying stay in human hands.
        """
        if not self._leaders_auto_settlements_enabled():
            return False
        today = fields.Date.context_today(self)
        vendors = self.env["res.partner"].sudo().search([
            ("is_marketplace_vendor", "=", True), ("vendor_state", "=", "approved"),
        ])
        Wallet = self.env["leaders.wallet.transaction"].sudo()
        created = self.browse()
        for vendor in vendors:
            frequency = vendor.vendor_tier_id.payout_frequency or "monthly"
            period = self._leaders_period_for(frequency, today)
            if not period:
                continue
            period_from, period_to = period
            overlapping = self.sudo().search_count([
                ("vendor_id", "=", vendor.id), ("state", "!=", "cancelled"),
                ("period_from", "<=", period_to), ("period_to", ">=", period_from),
            ])
            if overlapping:
                continue
            if not Wallet.search_count([
                ("vendor_id", "=", vendor.id), ("state", "=", "available"),
                ("settlement_id", "=", False),
                ("available_date", ">=", period_from), ("available_date", "<=", period_to),
            ]):
                continue
            settlement = self.sudo().create({
                "vendor_id": vendor.id,
                "period_from": period_from, "period_to": period_to,
                "currency_id": vendor.company_currency_id.id or self.env.company.currency_id.id,
            })
            settlement.action_collect_transactions()
            settlement.message_post(body=_(
                "Settlement opened automatically for the %(frequency)s payout period.",
                frequency=frequency))
            created |= settlement
        return created

    def _leaders_payout_journal(self):
        """Journal that pays this settlement; falls back to today's choice."""
        self.ensure_one()
        if self.payout_journal_id:
            return self.payout_journal_id
        return self.env["account.journal"].search(
            [("type", "in", ("bank", "cash")), ("company_id", "=", self.env.company.id)], limit=1)

    def action_cancel(self):
        """Undo a settlement that should not have been created.

        Wallet transactions are immutable, so nothing is deleted: the earnings
        and fees it collected are simply released back to the pool, and the
        commission it charged is neutralised by an equal and opposite
        adjustment. The commission journal entry is reversed, and this
        settlement's achievements are flagged so the cumulative true-up stops
        counting them as already charged.
        """
        self._leaders_check_backoffice("finance")
        Wallet = self.env["leaders.wallet.transaction"]
        for rec in self:
            if rec.state == "cancelled":
                continue
            if rec.state not in ("draft", "approved"):
                raise UserError(_(
                    "Only a draft or approved settlement can be cancelled; %(name)s "
                    "is %(state)s.", name=rec.name,
                    state=dict(rec._fields["state"]._description_selection(self.env)).get(rec.state)))
            if rec.payment_move_id:
                raise UserError(_(
                    "%s has already been paid; reverse the payout before cancelling.",
                    rec.name))
            invoiced = rec.commission_invoice_id.state == "posted"
            commissions = rec.transaction_ids.filtered(lambda t: t.type == "commission")
            charged = sum(commissions.mapped("amount"))
            # The tax charged on the marketplace invoice stays here, like the commission.
            taxes = rec.transaction_ids.filtered(lambda t: t.type == "fee_tax")
            # Release everything else back to the pool for a future settlement.
            (rec.transaction_ids - commissions - taxes).write({
                "settlement_id": False, "state": "available",
            })
            # The credit note covers what the fee entries would have; no journal entries then.
            Tx = Wallet.with_context(leaders_no_fee_entry=True) if invoiced else Wallet
            if invoiced:
                self.env["leaders.marketplace.accounting"].reverse_settlement_invoice(rec)
                for tax in taxes:
                    Tx.create({
                        "vendor_id": rec.vendor_id.id, "type": "fee_tax", "amount": -tax.amount,
                        "currency_id": rec.currency_id.id, "state": "available",
                        "available_date": fields.Date.context_today(self),
                        "settlement_id": rec.id, "reference": _("Cancelled %s", rec.name),
                        "note": _("Tax of the credited marketplace invoice."),
                    })
            if charged:
                # The commission wallet tx never had an entry of its own (the approval
                # entry books it, and is reversed below), so the neutralising adjustment
                # must not book one either, or the payable is credited twice.
                Wallet.with_context(leaders_no_fee_entry=True).create({
                    "vendor_id": rec.vendor_id.id,
                    "type": "adjustment",
                    "amount": -charged,          # charged is negative: this credits it back
                    "currency_id": rec.currency_id.id,
                    "available_date": fields.Date.context_today(self),
                    "state": "available",
                    "settlement_id": rec.id,
                    "reference": _("Cancelled %s", rec.name),
                    "note": _("Neutralises the commission of the cancelled settlement."),
                })
            if rec.commission_move_id and rec.commission_move_id.state == "posted":
                rec.commission_move_id._reverse_moves(cancel=True)
            rec.commission_achievement_ids.write({"cancelled": True})
            rec.state = "cancelled"
            rec.message_post(body=_("Settlement cancelled; its transactions were released."))
        return True

    def action_approve(self):
        self._leaders_check_backoffice("finance")
        self.write({"state":"approved"})
        Accounting = self.env["leaders.marketplace.accounting"]
        for rec in self:
            if Accounting._commission_invoicing_mode() == "invoice":
                Accounting.post_settlement_invoice(rec)
            else:
                Accounting.post_settlement_commission(rec)
            key = "vendor_settlement_debt" if rec.net_amount < 0 else "vendor_settlement_approved"
            self.env["leaders.notification"].notify_vendor(key, rec, rec.vendor_id)

    def action_mark_paid(self):
        self._leaders_check_backoffice("finance")
        Change = self.env["leaders.vendor.bank.change"]
        for rec in self:
            if Change._blocks_payout(rec.vendor_id):
                raise UserError(_(
                    "%(vendor)s has a payout account change waiting for review. "
                    "Approve or reject it before paying %(name)s.",
                    vendor=rec.vendor_id.display_name, name=rec.name))
        for rec in self:
            if rec.net_amount < 0:
                raise UserError(_(
                    "This vendor owes the marketplace %(amount)s for this period "
                    "(cash collected on delivery). Use 'Register Vendor Payment' once received.",
                    amount=-rec.net_amount))
            # Post the real payout journal entry (vendor payable -> bank).
            self.env["leaders.marketplace.accounting"].post_settlement_payment(rec)
            rec.transaction_ids.write({"state":"settled"})
            rec.state = "paid"
            self.env["leaders.notification"].notify_vendor("vendor_settlement_paid", rec, rec.vendor_id)

    def action_register_vendor_payment(self):
        """The vendor owes the marketplace (COD+FBM commission exceeded their
        other earnings for the period); register the money received from them."""
        self._leaders_check_backoffice("finance")
        for rec in self:
            if rec.net_amount >= 0:
                raise UserError(_("This settlement has no vendor debt to collect."))
            self.env["leaders.marketplace.accounting"].post_vendor_debt_collection(rec)
            rec.transaction_ids.write({"state":"settled"})
            rec.state = "paid"
