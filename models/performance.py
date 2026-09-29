from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

class VendorKPI(models.Model):
    _name = "leaders.vendor.kpi"
    _description = "Vendor KPI Snapshot"
    _order = "period desc, id desc"

    vendor_id = fields.Many2one("res.partner", required=True, index=True)
    period = fields.Date(required=True, index=True)
    order_count = fields.Integer()
    completion_rate = fields.Float()
    cancellation_rate = fields.Float()
    avg_rating = fields.Float()
    avg_ship_hours = fields.Float()

    _sql_constraints = [
        ("vendor_period_uniq", "unique(vendor_id, period)",
         "A vendor already has a KPI snapshot for this month."),
    ]

    @api.model
    def _period_start(self, day=None):
        """First day of the month ``day`` falls in."""
        day = day or fields.Date.context_today(self)
        return fields.Date.to_date(day).replace(day=1)

    @api.model
    def _compute_for_period(self, vendor, month_start):
        """KPI values for one vendor over one calendar month.

        Orders still ``pending`` are not counted: nothing has happened to them
        yet, so including them would drag every rate down for no reason.

        :return: dict of field values (without vendor_id/period).
        """
        vendor = vendor.commercial_partner_id
        month_start = self._period_start(month_start)
        month_end = month_start + relativedelta(months=1)
        orders = self.env["leaders.vendor.order"].sudo().search([
            ("vendor_id", "=", vendor.id),
            ("state", "!=", "pending"),
            ("date_order", ">=", fields.Datetime.to_datetime(month_start)),
            ("date_order", "<", fields.Datetime.to_datetime(month_end)),
        ])
        count = len(orders)
        completed = len(orders.filtered(lambda o: o.state == "completed"))
        cancelled = len(orders.filtered(lambda o: o.state == "cancelled"))
        shipped = orders.filtered(lambda o: o.date_shipped and o.date_order)
        ship_hours = [
            (o.date_shipped - o.date_order).total_seconds() / 3600.0 for o in shipped]
        ratings = self.env["leaders.marketplace.offer"].sudo().search([
            ("vendor_id", "=", vendor.id), ("state", "=", "active"),
        ]).mapped("rating")
        return {
            "order_count": count,
            "completion_rate": (completed / count * 100.0) if count else 0.0,
            "cancellation_rate": (cancelled / count * 100.0) if count else 0.0,
            "avg_ship_hours": (sum(ship_hours) / len(ship_hours)) if ship_hours else 0.0,
            "avg_rating": (sum(ratings) / len(ratings)) if ratings else 0.0,
        }

    @api.model
    def _upsert_for_period(self, vendor, month_start):
        """One row per (vendor, month); recomputing overwrites it in place."""
        vendor = vendor.commercial_partner_id
        month_start = self._period_start(month_start)
        values = self._compute_for_period(vendor, month_start)
        snapshot = self.sudo().search([
            ("vendor_id", "=", vendor.id), ("period", "=", month_start)], limit=1)
        if snapshot:
            snapshot.write(values)
            return snapshot
        return self.sudo().create(dict(values, vendor_id=vendor.id, period=month_start))

    @api.model
    def _recompute_vendors(self, vendors, months=2):
        """Refresh the last ``months`` snapshots (current month first)."""
        today = fields.Date.context_today(self)
        periods = [self._period_start(today) - relativedelta(months=offset)
                   for offset in range(months)]
        snapshots = self.browse()
        for vendor in vendors:
            for period in periods:
                snapshots |= self._upsert_for_period(vendor, period)
        return snapshots

    @api.model
    def cron_compute_kpis(self):
        """Daily refresh of this month and the previous one, for live vendors."""
        vendors = self.env["res.partner"].sudo().search([
            ("is_marketplace_vendor", "=", True),
            ("vendor_state", "in", ("approved", "frozen")),
        ])
        snapshots = self._recompute_vendors(vendors)
        snapshots._check_cancellation_warning()
        return True

    def action_recompute_kpis(self):
        """Back-office button: refresh the snapshots on screen."""
        self.env["leaders.vendor.penalty"]._leaders_check_backoffice("manager")
        for snapshot in self:
            self._upsert_for_period(snapshot.vendor_id, snapshot.period)
        self._check_cancellation_warning()
        return True

    @api.model
    def action_recompute_all_kpis(self):
        """Back-office action: refresh every live vendor, current and last month."""
        self.env["leaders.vendor.penalty"]._leaders_check_backoffice("manager")
        return self.cron_compute_kpis()

    def _check_cancellation_warning(self):
        """Raise a DRAFT warning penalty when cancellations run high.

        Off by default. Even switched on it only ever *prepares* a penalty: a
        manager still has to apply it, so no vendor is ever punished by a
        threshold alone.
        """
        get = self.env["ir.config_parameter"].sudo().get_param
        enabled = str(get("leaders_marketplace.kpi_auto_warning")).strip().lower()
        if enabled in ("", "false", "0", "none"):
            return False
        try:
            threshold = float(get("leaders_marketplace.kpi_cancellation_threshold", 20.0) or 0.0)
        except (TypeError, ValueError):
            threshold = 20.0
        if threshold <= 0:
            return False
        Penalty = self.env["leaders.vendor.penalty"].sudo()
        created = Penalty.browse()
        for snapshot in self:
            if snapshot.cancellation_rate <= threshold or not snapshot.order_count:
                continue
            reference = _(
                "Cancellation rate of %(rate).2f%% in %(period)s (threshold %(limit).2f%%).",
                rate=snapshot.cancellation_rate, period=snapshot.period,
                limit=threshold)
            if Penalty.search_count([
                ("vendor_id", "=", snapshot.vendor_id.id),
                ("level", "=", "warning"), ("state", "=", "draft"),
                ("reason", "=", reference),
            ]):
                continue
            created |= Penalty.create({
                "vendor_id": snapshot.vendor_id.id,
                "level": "warning",
                "reason": reference,
            })
        return created

class VendorPenalty(models.Model):
    _name = "leaders.vendor.penalty"
    _description = "Vendor Penalty"
    _inherit = ["mail.thread", "leaders.backoffice.mixin"]

    vendor_id = fields.Many2one("res.partner", required=True, index=True)
    level = fields.Selection([("warning","Warning"),("freeze","Freeze"),("ban","Ban")], required=True)
    reason = fields.Text(required=True)
    date_start = fields.Date(default=fields.Date.today)
    date_end = fields.Date()
    active = fields.Boolean(default=True)
    state = fields.Selection([
        ("draft", "Draft"), ("applied", "Applied"), ("lifted", "Lifted"),
    ], default="draft", required=True, index=True, tracking=True)
    amount = fields.Monetary(
        currency_field="currency_id", string="Fine",
        help="Optional fine deducted from the vendor's wallet when the penalty "
             "is applied. Leave at zero for a penalty with no money attached.")
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id)
    offer_state_snapshot = fields.Json(
        copy=False, readonly=True,
        help="State of each offer before this penalty suspended it, so lifting "
             "restores exactly what was there.")
    deactivated_user_ids = fields.Many2many(
        "res.users", string="Deactivated Users", copy=False, readonly=True,
        help="Users this penalty switched off, so lifting reactivates those and "
             "no others.")
    wallet_transaction_id = fields.Many2one(
        "leaders.wallet.transaction", string="Fine Transaction", copy=False, readonly=True)
    applied_by = fields.Many2one("res.users", readonly=True, copy=False)
    applied_date = fields.Datetime(readonly=True, copy=False)
    lifted_by = fields.Many2one("res.users", readonly=True, copy=False)
    lifted_date = fields.Datetime(readonly=True, copy=False)

    def _vendor_users(self):
        """Every portal user attached to this vendor, owner and staff alike.

        ``vendor_id.user_ids`` only covers users whose partner *is* the vendor
        record; portal users hang off child contacts, which is why banning used
        to switch nobody off.
        """
        self.ensure_one()
        return self.env["res.users"].sudo().search([
            ("partner_id.commercial_partner_id", "=", self.vendor_id.commercial_partner_id.id),
        ])

    def _suspend_offers(self):
        """Suspend the vendor's live offers, remembering what they were."""
        self.ensure_one()
        offers = self.env["leaders.marketplace.offer"].sudo().search([
            ("vendor_id", "=", self.vendor_id.id), ("state", "!=", "suspended"),
        ])
        self.sudo().offer_state_snapshot = {str(o.id): o.state for o in offers}
        offers.write({"state": "suspended"})
        return offers

    def _restore_offers(self):
        """Put the offers back exactly as the snapshot found them."""
        self.ensure_one()
        Offer = self.env["leaders.marketplace.offer"].sudo()
        for offer_id, state in (self.offer_state_snapshot or {}).items():
            offer = Offer.browse(int(offer_id)).exists()
            if offer and offer.state == "suspended":
                offer.state = state
        self.sudo().offer_state_snapshot = False

    def action_apply(self):
        """Enforce the penalty. Manager only."""
        self._leaders_check_backoffice("manager")
        Wallet = self.env["leaders.wallet.transaction"].sudo()
        for rec in self:
            if rec.state == "applied":
                continue
            if rec.state == "lifted":
                raise UserError(_(
                    "%s has already been lifted; create a new penalty instead.", rec.display_name))
            # Writing a partner needs base.group_partner_manager, which a
            # marketplace manager has no reason to hold; the back-office guard
            # above is what authorises this, so the writes go through sudo.
            vendor = rec.vendor_id.commercial_partner_id.sudo()
            # Before a ban switches their users off.
            recipients = self.env["leaders.notification"]._vendor_recipients(vendor)
            if rec.level == "warning":
                # A warning changes nothing but the record; it is a formal notice.
                vendor.message_post(
                    body=_("Marketplace warning: %s", rec.reason),
                    partner_ids=rec._vendor_users().partner_id.ids)
            elif rec.level == "freeze":
                vendor.vendor_state = "frozen"
                rec._suspend_offers()
            elif rec.level == "ban":
                vendor.vendor_state = "banned"
                rec._suspend_offers()
                users = rec._vendor_users().filtered("active")
                rec.sudo().deactivated_user_ids = [(6, 0, users.ids)]
                users.write({"active": False})
            if rec.amount:
                rec.sudo().wallet_transaction_id = Wallet.create({
                    "vendor_id": vendor.id,
                    "type": "penalty",
                    "amount": -abs(rec.amount),
                    "currency_id": rec.currency_id.id,
                    "available_date": fields.Date.context_today(rec),
                    "state": "available",
                    "reference": _("Penalty %s", rec.display_name),
                    "note": rec.reason,
                })
            rec.sudo().write({
                "state": "applied",
                "applied_by": self.env.user.id,
                "applied_date": fields.Datetime.now(),
            })
            rec.message_post(body=_(
                "Penalty applied by %(user)s.", user=self.env.user.display_name))
            self.env["leaders.notification"].notify("vendor_penalty_applied", rec, partners=recipients)
        return True

    def action_lift(self):
        """Undo an applied penalty. Manager only; the fine is not refunded."""
        self._leaders_check_backoffice("manager")
        for rec in self:
            if rec.state != "applied":
                continue
            vendor = rec.vendor_id.commercial_partner_id.sudo()
            if rec.level in ("freeze", "ban"):
                vendor.vendor_state = "approved"
                rec._restore_offers()
            # Read with active_test off: the whole point is that these users are
            # inactive, and a plain read would hand back an empty recordset.
            switched_off = rec.sudo().with_context(active_test=False).deactivated_user_ids
            if switched_off:
                switched_off.write({"active": True})
                rec.sudo().deactivated_user_ids = [(5, 0, 0)]
            rec.sudo().write({
                "state": "lifted",
                "lifted_by": self.env.user.id,
                "lifted_date": fields.Datetime.now(),
            })
            rec.message_post(body=_(
                "Penalty lifted by %(user)s.", user=self.env.user.display_name))
            self.env["leaders.notification"].notify_vendor("vendor_penalty_lifted", rec, vendor)
        return True

    @api.model
    def cron_lift_expired_penalties(self):
        """Lift applied penalties whose end date has passed."""
        expired = self.sudo().search([
            ("state", "=", "applied"),
            ("date_end", "!=", False),
            ("date_end", "<", fields.Date.context_today(self)),
        ])
        for penalty in expired:
            penalty.message_post(body=_("Penalty lifted automatically: its end date passed."))
        expired.action_lift()
        return True
