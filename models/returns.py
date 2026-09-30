import base64
from datetime import datetime, time, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import format_date
from odoo.tools.float_utils import float_compare, float_is_zero
from odoo.tools.mimetypes import guess_mimetype

PARAM_CENTER = "leaders_marketplace.returns_center_enabled"
PARAM_SHIPPING_FEE = "leaders_marketplace.return_shipping_fee"
PARAM_PICKUP_DAYS = "leaders_marketplace.return_pickup_max_days"
MAX_RETURN_PHOTOS = 5


class VendorReturn(models.Model):
    """Customer return of products bought from a marketplace vendor.

    Flow: requested -> (vendor approves) approved -> (goods back in vendor stock) received
          -> refunded.  A vendor rejection or a missed vendor deadline escalates the return
          to the platform, which approves or rejects it for good.
    """
    _name = "leaders.vendor.return"
    _description = "Marketplace Return"
    _inherit = ["mail.thread", "mail.activity.mixin", "leaders.backoffice.mixin"]
    _order = "id desc"

    name = fields.Char(default="New", copy=False, readonly=True)
    vendor_order_id = fields.Many2one("leaders.vendor.order", required=True, index=True, readonly=True)
    vendor_id = fields.Many2one(related="vendor_order_id.vendor_id", store=True, index=True)
    partner_id = fields.Many2one(related="vendor_order_id.partner_id", store=True, string="Customer")
    currency_id = fields.Many2one(related="vendor_order_id.currency_id", store=True)
    line_ids = fields.One2many("leaders.vendor.return.line", "return_id", copy=True)
    reason = fields.Text(required=True)
    vendor_note = fields.Text(string="Vendor Response", tracking=True)
    decision_note = fields.Text(string="Platform Decision", tracking=True)
    state = fields.Selection([
        ("requested", "Requested"),
        ("approved", "Approved"),
        ("rejected", "Rejected by Vendor"),
        ("escalated", "Escalated"),
        ("received", "Received"),
        ("refunded", "Refunded"),
        ("closed", "Closed (Rejected)"),
        ("cancelled", "Cancelled"),
        ("exchanged", "Exchanged"),
    ], default="requested", tracking=True, index=True)
    request_date = fields.Datetime(default=fields.Datetime.now, readonly=True)
    vendor_deadline = fields.Datetime(readonly=True, help="After this date an unanswered request escalates to the platform.")
    approved_by = fields.Selection([("vendor", "Vendor"), ("platform", "Platform")], readonly=True)
    picking_ids = fields.One2many("stock.picking", "leaders_return_id", string="Return Receipts")
    refund_amount = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    commission_refund = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    vendor_deduction = fields.Monetary(
        compute="_compute_amounts", store=True, currency_field="currency_id",
        help="Refund minus the commission given back: what the vendor effectively bears.")
    credit_note_id = fields.Many2one("leaders.vendor.invoice", string="Vendor Credit Note", readonly=True, copy=False)
    refund_move_id = fields.Many2one("account.move", string="Refund Journal Entry", readonly=True, copy=False)
    refund_payment_id = fields.Many2one("account.payment", string="Customer Refund Payment", readonly=True, copy=False)
    wallet_transaction_id = fields.Many2one("leaders.wallet.transaction", readonly=True, copy=False)
    refund_method = fields.Selection([("original", "Original Payment")], default="original")
    # -- D2: returns & disputes center ------------------------------------------
    reason_id = fields.Many2one("leaders.return.reason", string="Reason Code", index=True)
    photo_ids = fields.Many2many(
        "ir.attachment", "leaders_vendor_return_photo_rel", "return_id", "attachment_id",
        string="Photos", copy=False,
        help="Up to %s photos of the goods, added by the customer." % MAX_RETURN_PHOTOS)
    resolution = fields.Selection(
        [("refund", "Refund"), ("exchange", "Exchange")], default="refund", required=True, tracking=True,
        help="Exchange: once the goods are back the vendor sends the same product again at no charge; "
             "nothing is refunded.")
    customer_pays_shipping = fields.Boolean(
        readonly=True, copy=False, help="Copied from the reason: the return shipping fee is deducted from the refund.")
    shipping_fee_deducted = fields.Monetary(
        readonly=True, copy=False, currency_field="currency_id",
        help="Return shipping fee kept by the marketplace out of the customer's refund. It never reduces "
             "the vendor's deduction: the vendor bears the goods value as for any return.")
    net_refund_amount = fields.Monetary(
        compute="_compute_amounts", store=True, currency_field="currency_id",
        help="What the customer actually gets back: the refund minus the shipping fee.")
    pickup_date = fields.Date(
        help="Day the customer wants the marketplace to collect the goods (FBL returns only).")
    replacement_group_ids = fields.One2many(
        "leaders.fulfillment.group", "replacement_return_id", string="Replacement Shipments", readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        sla = int(self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.return_vendor_sla_days", 3) or 3)
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("leaders.vendor.return") or "New"
            vals.setdefault("vendor_deadline", fields.Datetime.now() + timedelta(days=sla))
            if vals.get("reason_id") and "customer_pays_shipping" not in vals:
                vals["customer_pays_shipping"] = self.env["leaders.return.reason"].browse(
                    vals["reason_id"]).customer_pays_shipping
        records = super().create(vals_list)
        records._check_returnable()
        records._center_after_create()
        for rec in records:
            self.env["leaders.notification"].notify_vendor("vendor_return_requested", rec, rec.vendor_id)
        return records

    @api.depends("line_ids.refund_amount", "line_ids.commission_refund", "shipping_fee_deducted")
    def _compute_amounts(self):
        for rec in self:
            rec.refund_amount = sum(rec.line_ids.mapped("refund_amount"))
            rec.commission_refund = sum(rec.line_ids.mapped("commission_refund"))
            rec.vendor_deduction = rec.refund_amount - rec.commission_refund
            rec.net_refund_amount = rec.refund_amount - rec.shipping_fee_deducted

    # -------------------------------------------------------------------------
    # D2: returns & disputes center (switch: returns_center_enabled)
    # -------------------------------------------------------------------------
    @api.model
    def _leaders_center_enabled(self):
        return self.env["ir.config_parameter"].sudo().get_param(PARAM_CENTER) in ("True", "true", "1")

    @api.model
    def _leaders_shipping_fee(self):
        try:
            return max(float(self.env["ir.config_parameter"].sudo().get_param(PARAM_SHIPPING_FEE) or 0.0), 0.0)
        except (TypeError, ValueError):
            return 0.0

    @api.model
    def _leaders_pickup_max_days(self):
        try:
            days = int(self.env["ir.config_parameter"].sudo().get_param(PARAM_PICKUP_DAYS) or 7)
        except (TypeError, ValueError):
            days = 7
        return days if days > 0 else 7

    @api.model
    def _leaders_photo_max_bytes(self):
        try:
            limit = int(self.env["ir.config_parameter"].sudo().get_param(
                "leaders_marketplace.vendor_image_max_mb", 5))
        except (TypeError, ValueError):
            limit = 5
        return max(limit, 1) * 1024 * 1024

    @api.constrains("photo_ids", "reason_id")
    def _check_photos(self):
        max_bytes = self._leaders_photo_max_bytes()
        for rec in self:
            if len(rec.photo_ids) > MAX_RETURN_PHOTOS:
                raise ValidationError(_("You can attach at most %s photos.", MAX_RETURN_PHOTOS))
            for photo in rec.photo_ids:
                raw = photo.raw or b""
                if not raw:
                    raise ValidationError(_("The uploaded image is empty."))
                if len(raw) > max_bytes:
                    raise ValidationError(_(
                        "The image is too large (max %(size)s MB).", size=max_bytes // (1024 * 1024)))
                mimetype = guess_mimetype(raw, default="")
                if not mimetype.startswith("image/") or "svg" in mimetype:
                    raise ValidationError(_("Only image files (PNG, JPEG, GIF, WebP) can be uploaded."))
            if rec.reason_id.requires_photo and not rec.photo_ids:
                raise ValidationError(_(
                    "Please attach at least one photo: it is required for the reason \"%s\".",
                    rec.reason_id.name))

    def _leaders_fbl_groups(self):
        """Fulfillment groups of the returned goods that ship from the platform (FBL)."""
        self.ensure_one()
        return self.line_ids.vendor_line_id.fulfillment_group_id.filtered(
            lambda g: g.fulfillment_type == "fbl")

    @api.constrains("pickup_date")
    def _check_pickup_date(self):
        for rec in self.filtered("pickup_date"):
            if not rec._leaders_fbl_groups():
                raise ValidationError(_("A pickup date can only be chosen for goods stored in the marketplace warehouse."))
            first = fields.Datetime.to_datetime(rec.request_date or fields.Datetime.now()).date()
            last = first + timedelta(days=rec._leaders_pickup_max_days())
            if not first <= rec.pickup_date <= last:
                raise ValidationError(_(
                    "Choose a pickup date between %(first)s and %(last)s.",
                    first=format_date(self.env, first), last=format_date(self.env, last)))

    def _exchange_shortfall(self):
        """Product names the vendor cannot replace right now (empty: stock covers the exchange)."""
        self.ensure_one()
        Quant = self.env["stock.quant"].sudo()
        vendor = self.vendor_id.commercial_partner_id
        wanted = {}
        for line in self.line_ids:
            group = line.vendor_line_id.fulfillment_group_id
            location = group.source_location_id if group else vendor._marketplace_stock_location("fbm")
            key = (line.product_id, location)
            wanted[key] = wanted.get(key, 0.0) + line.quantity
        short = []
        for (product, location), qty in wanted.items():
            available = Quant._get_available_quantity(product, location, owner_id=vendor)
            if float_compare(available, qty, precision_rounding=product.uom_id.rounding or 0.01) < 0:
                short.append(product.display_name)
        return short

    def _check_exchange(self):
        for rec in self.filtered(lambda r: r.resolution == "exchange"):
            if any(l.vendor_line_id.is_delivery or not l.product_id.is_storable for l in rec.line_ids):
                raise ValidationError(_("Only stocked products can be exchanged."))
            short = rec._exchange_shortfall()
            if short:
                raise ValidationError(_(
                    "The seller does not have enough stock to exchange: %s. Ask for a refund instead.",
                    ", ".join(short)))

    def _center_after_create(self):
        """Rules of the new request: stored fee, exchange feasibility (photos/pickup are constraints)."""
        for rec in self:
            rec._check_exchange()
            fee = 0.0
            if (self._leaders_center_enabled() and rec.customer_pays_shipping
                    and rec.resolution == "refund"):
                fee = min(self._leaders_shipping_fee(), rec.refund_amount)
                fee = rec.currency_id.round(fee) if rec.currency_id else fee
            if fee:
                rec.shipping_fee_deducted = fee
            if rec.photo_ids:
                rec.photo_ids.sudo().write({"res_model": self._name, "res_id": rec.id})

    def _leaders_deadline(self):
        """The date the customer is waiting for (vendor answer), or False."""
        self.ensure_one()
        return self.vendor_deadline if self.state == "requested" else False

    def _leaders_next_step(self):
        """What happens next, in words, for the customer (translated, one sentence)."""
        self.ensure_one()
        state = self.state
        if state == "requested":
            return _("Waiting for the seller to answer before %s.",
                     format_date(self.env, self.vendor_deadline) if self.vendor_deadline else _("the deadline"))
        if state == "approved":
            if self._leaders_fbl_groups():
                if self.pickup_date:
                    return _("Pickup scheduled on %s. Keep the goods packed and ready.",
                             format_date(self.env, self.pickup_date))
                return _("Send the goods back to the marketplace warehouse.")
            return _("Send the goods back to the seller.")
        if state == "rejected":
            return _("The seller declined your request. You can ask the marketplace to review it.")
        if state == "escalated":
            return _("The marketplace is reviewing your request.")
        if state == "received":
            if self.resolution == "exchange":
                return _("Goods received. Your replacement is being prepared.")
            return _("Goods received. Your refund is being processed.")
        if state == "refunded":
            if self.shipping_fee_deducted:
                return _("Refund completed: %(amount)s, after a shipping fee of %(fee)s.",
                         amount=self._leaders_money(self.net_refund_amount),
                         fee=self._leaders_money(self.shipping_fee_deducted))
            return _("Refund completed: %s.", self._leaders_money(self.refund_amount))
        if state == "exchanged":
            groups = self.replacement_group_ids.filtered(lambda g: g.state != "cancelled")
            return _("Your replacement is on its way (%s).", ", ".join(groups.mapped("name")) or self.name)
        if state == "closed":
            return _("Request closed. Nothing more to do.")
        return _("Request cancelled. Nothing more to do.")

    def _leaders_money(self, amount):
        self.ensure_one()
        return self.currency_id.format(amount) if self.currency_id else "%.2f" % amount

    def _leaders_timeline_steps(self):
        """[{label, status (done/current/todo), date}] of the request, for the detail page."""
        self.ensure_one()
        state = self.state
        answered = state not in ("requested",)
        approved = state in ("approved", "received", "refunded", "exchanged")
        end = {"refunded": _("Refunded"), "exchanged": _("Exchanged")}.get(
            state, _("Exchanged") if self.resolution == "exchange" else _("Refunded"))
        steps = [
            (_("Requested"), True, self.request_date),
            (_("Seller decision"), answered, False),
            (_("Goods returned"), state in ("received", "refunded", "exchanged"), False),
            (end, state in ("refunded", "exchanged"), False),
        ]
        if state in ("rejected", "closed", "cancelled"):
            steps = steps[:2] + [(dict(self._fields["state"]._description_selection(self.env))[state], True, False)]
        elif state == "escalated":
            steps[1] = (_("Marketplace decision"), False, False)
        result, current_set = [], False
        for label, done, date in steps:
            status = "done" if done else ("todo" if current_set else "current")
            current_set = current_set or not done
            result.append({"label": label, "status": status, "date": date})
        if approved and not any(s["status"] == "current" for s in result):
            result[-1]["status"] = "done"
        return result

    # -------------------------------------------------------------------------
    # Validation
    # -------------------------------------------------------------------------
    def _check_returnable(self, check_window=True):
        window = int(self.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.return_window_days", 14) or 0)
        for rec in self:
            order = rec.vendor_order_id
            if order.state not in ("fulfilled", "completed", "partially_fulfilled"):
                raise ValidationError(_("Only shipped vendor orders can be returned."))
            if not rec.line_ids:
                raise ValidationError(_("Select at least one product to return."))
            if check_window and window and order.date_shipped and fields.Datetime.now() > order.date_shipped + timedelta(days=window):
                raise ValidationError(_("The return window of %s days for this order has passed.", window))
            for line in rec.line_ids:
                if line.vendor_line_id.order_id != order:
                    raise ValidationError(_("Returned products must belong to the vendor order."))
                if float_compare(line.quantity, 0, precision_digits=4) <= 0:
                    raise ValidationError(_("Return quantities must be positive."))
                if float_compare(line.quantity, line.vendor_line_id._qty_returnable(exclude=rec),
                                 precision_rounding=line.product_id.uom_id.rounding or 0.01) > 0:
                    raise ValidationError(_("You cannot return more %s than was delivered.", line.product_id.display_name))

    # -------------------------------------------------------------------------
    # Decisions
    # -------------------------------------------------------------------------
    def action_vendor_approve(self, note=None):
        for rec in self:
            if rec.state != "requested":
                raise UserError(_("Only new return requests can be approved by the vendor."))
            rec._approve("vendor", note=note)

    def action_vendor_reject(self, note=None):
        for rec in self:
            if rec.state != "requested":
                raise UserError(_("Only new return requests can be rejected by the vendor."))
            if not (note or rec.vendor_note):
                raise UserError(_("Please explain why the return is rejected."))
            vals = {"state": "rejected"}
            if note:
                vals["vendor_note"] = note
            rec.write(vals)
            self.env["leaders.notification"].notify_customer("customer_return_rejected", rec, rec.partner_id)

    def action_escalate(self):
        for rec in self:
            if rec.state not in ("requested", "rejected"):
                raise UserError(_("Only pending or vendor-rejected returns can be escalated."))
            rec.state = "escalated"
            rec.message_post(body=_("Return escalated to the marketplace for a decision."))
            self.env["leaders.notification"].notify_vendor("vendor_return_escalated", rec, rec.vendor_id)

    def action_platform_approve(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self:
            if rec.state not in ("requested", "rejected", "escalated"):
                raise UserError(_("This return cannot be approved in its current state."))
            rec._approve("platform")
            self.env["leaders.notification"].notify_vendor("vendor_return_decided", rec, rec.vendor_id)

    def action_platform_reject(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self:
            if rec.state not in ("requested", "rejected", "escalated"):
                raise UserError(_("This return cannot be rejected in its current state."))
            if not rec.decision_note:
                raise UserError(_("Please write the platform decision before rejecting."))
            rec.state = "closed"
            self.env["leaders.notification"].notify_vendor("vendor_return_decided", rec, rec.vendor_id)
            self.env["leaders.notification"].notify_customer("customer_return_rejected", rec, rec.partner_id)

    def action_cancel(self):
        for rec in self:
            if rec.state in ("received", "refunded", "exchanged"):
                raise UserError(_("Received returns cannot be cancelled."))
            rec.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")).action_cancel()
            rec.state = "cancelled"

    def _approve(self, by, note=None):
        self.ensure_one()
        vals = {"state": "approved", "approved_by": by}
        if note:
            vals["vendor_note"] = note
        self.write(vals)
        # The return window only applies to the request date, not to late decisions.
        self._check_returnable(check_window=False)
        self._create_return_pickings()
        self.env["leaders.notification"].notify_customer("customer_return_approved", self, self.partner_id)

    @api.model
    def cron_escalate_overdue(self):
        overdue = self.search([("state", "=", "requested"), ("vendor_deadline", "<", fields.Datetime.now())])
        for rec in overdue:
            rec.action_escalate()
            rec.message_post(body=_("The vendor did not answer before the deadline."))

    # -------------------------------------------------------------------------
    # Goods back into the vendor's stock
    # -------------------------------------------------------------------------
    def _create_return_pickings(self):
        Picking = self.env["stock.picking"].sudo()
        customer_location = self.env.ref("stock.stock_location_customers")
        for rec in self:
            if rec.picking_ids.filtered(lambda p: p.state != "cancel"):
                continue
            vendor = rec.vendor_id.commercial_partner_id
            by_group = {}
            for line in rec.line_ids:
                group = line.vendor_line_id.fulfillment_group_id
                by_group.setdefault(group, self.env["leaders.vendor.return.line"])
                by_group[group] |= line
            for group, lines in by_group.items():
                if group:
                    destination = group.source_location_id
                    picking_type = group._picking_type().warehouse_id.in_type_id
                else:
                    destination = vendor._marketplace_stock_location("fbm")
                    picking_type = vendor.vendor_warehouse_id.in_type_id
                extra = {}
                if rec.pickup_date and group and group.fulfillment_type == "fbl":
                    # The customer's chosen collection day (09:00) is the receipt's scheduled date.
                    extra["scheduled_date"] = datetime.combine(rec.pickup_date, time(9, 0))
                picking = Picking.create(dict(extra, **{
                    "picking_type_id": picking_type.id,
                    "location_id": customer_location.id,
                    "location_dest_id": destination.id,
                    "partner_id": (rec.vendor_order_id.partner_shipping_id or rec.partner_id).id,
                    "origin": f"{rec.name} ({rec.vendor_order_id.name})",
                    # Returned goods stay the vendor's property.
                    "owner_id": vendor.id,
                    "leaders_return_id": rec.id,
                    "move_ids": [(0, 0, {
                        "name": rl.vendor_line_id.name,
                        "product_id": rl.product_id.id,
                        "product_uom_qty": rl.quantity,
                        "product_uom": (rl.vendor_line_id.product_uom or rl.product_id.uom_id).id,
                        "location_id": customer_location.id,
                        "location_dest_id": destination.id,
                        "restrict_partner_id": vendor.id,
                        "origin": rec.name,
                    }) for rl in lines],
                }))
                picking.action_confirm()

    def action_receive(self):
        """Confirm the returned goods arrived: validates the return receipts."""
        for rec in self:
            if rec.state != "approved":
                raise UserError(_("Only approved returns can be received."))
            for picking in rec.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")):
                for move in picking.move_ids:
                    move.quantity = move.product_uom_qty
                picking.move_ids.picked = True
                picking.with_context(skip_backorder=True, skip_sms=True)._action_done()
            rec._on_goods_received()

    def _on_goods_received(self):
        for rec in self:
            pickings = rec.picking_ids.filtered(lambda p: p.state != "cancel")
            if rec.state != "approved" or not pickings or any(p.state != "done" for p in pickings):
                continue
            rec.state = "received"
            if rec.resolution == "exchange":
                rec._process_exchange()
            else:
                rec._process_refund()

    def _process_exchange(self):
        """Goods are back: send the same product again, at no charge.

        No money moves: no refund entry, credit note, commission or wallet line,
        and the vendor order keeps its earning. Each source group of the returned
        lines gets a replacement fulfillment group (``is_replacement``, ignored by
        the vendor order state machine) with its own delivery from the vendor's
        stock to the customer. The moves carry no vendor order line, so
        ``qty_delivered`` is untouched: the customer holds the same quantity again
        and, unlike a refund, the exchange does not make it unreturnable.
        If the vendor's stock cannot cover the replacement any more, the request
        falls back to a refund (nothing is blocked while the goods sit in the
        warehouse) and the chatter says so.
        """
        Group = self.env["leaders.fulfillment.group"].sudo()
        Picking = self.env["stock.picking"].sudo()
        customer_location = self.env.ref("stock.stock_location_customers")
        for rec in self:
            if rec.state != "received":
                continue
            short = rec._exchange_shortfall()
            if short:
                rec.message_post(body=_(
                    "The seller no longer has enough stock to exchange %s: the return is refunded instead.",
                    ", ".join(short)))
                rec.resolution = "refund"
                rec._process_refund()
                continue
            order = rec.vendor_order_id
            vendor = rec.vendor_id.commercial_partner_id
            by_group = {}
            for line in rec.line_ids:
                by_group.setdefault(line.vendor_line_id.fulfillment_group_id, self.env["leaders.vendor.return.line"])
                by_group[line.vendor_line_id.fulfillment_group_id] |= line
            for source, lines in by_group.items():
                if source:
                    fulfillment_type, location = source.fulfillment_type, source.source_location_id
                else:
                    fulfillment_type, location = "fbm", vendor._marketplace_stock_location("fbm")
                group = Group.create({
                    "vendor_order_id": order.id, "fulfillment_type": fulfillment_type,
                    "source_location_id": location.id, "is_replacement": True,
                    "replacement_return_id": rec.id,
                })
                picking = Picking.create({
                    "picking_type_id": group._picking_type().id,
                    "location_id": location.id,
                    "location_dest_id": customer_location.id,
                    "partner_id": (order.partner_shipping_id or order.partner_id).id,
                    "origin": _("%(ret)s replacement (%(order)s)", ret=rec.name, order=order.name),
                    "owner_id": vendor.id,
                    "leaders_fulfillment_group_id": group.id,
                    "move_ids": [(0, 0, {
                        "name": rl.vendor_line_id.name,
                        "product_id": rl.product_id.id,
                        "product_uom_qty": rl.quantity,
                        "product_uom": (rl.vendor_line_id.product_uom or rl.product_id.uom_id).id,
                        "location_id": location.id,
                        "location_dest_id": customer_location.id,
                        "restrict_partner_id": vendor.id,
                        "origin": rec.name,
                    }) for rl in lines],
                })
                picking.action_confirm()
                picking.action_assign()
                group._sync_from_pickings()
            rec.state = "exchanged"
            rec.message_post(body=_(
                "Goods received. Replacement shipment(s) %s created at no charge.",
                ", ".join(rec.replacement_group_ids.mapped("name"))))

    # -------------------------------------------------------------------------
    # Money
    # -------------------------------------------------------------------------
    def _process_refund(self):
        Accounting = self.env["leaders.marketplace.accounting"]
        Wallet = self.env["leaders.wallet.transaction"].sudo()
        for rec in self:
            if rec.state != "received":
                continue
            order = rec.vendor_order_id
            earning = Wallet.search([
                ("vendor_order_id", "=", order.id), ("type", "in", ("earning", "cod_collected")),
            ], limit=1)
            # Commission on the returned quantity goes back to the vendor.
            self.env["leaders.commission.line"].sudo()._refund_for_return(rec)
            order.refunded_amount += rec.refund_amount
            order._recompute_amounts()

            cod_reversal = bool(earning) and earning.type == "cod_collected"
            if cod_reversal:
                # FBM + COD: the vendor already holds the cash and never had a
                # reserve. They now owe us LESS, not more -- a positive amount
                # relieves their debt by the commission share of the return.
                if rec.commission_refund:
                    rec.wallet_transaction_id = Wallet.create({
                        "vendor_id": order.vendor_id.id,
                        "vendor_order_id": order.id,
                        "type": "return",
                        "amount": rec.commission_refund,
                        "currency_id": order.currency_id.id,
                        "available_date": fields.Date.context_today(self),
                        "state": "available",
                        "reference": rec.name,
                    })
            elif earning:
                # Escrow was already released: take it back from the vendor's wallet.
                tier = order.vendor_id.vendor_tier_id
                rec.wallet_transaction_id = Wallet.create({
                    "vendor_id": order.vendor_id.id,
                    "vendor_order_id": order.id,
                    "type": "return",
                    "amount": -rec.vendor_deduction,
                    "currency_id": order.currency_id.id,
                    "available_date": fields.Date.context_today(self),
                    "state": "available",
                    "reference": rec.name,
                })
                if tier.return_policy == "reserve_first":
                    self.env["leaders.vendor.reserve"].sudo()._consume_for_return(rec)
            rec.refund_move_id = Accounting.post_return_refund_entry(
                rec, released=bool(earning) and not cod_reversal, cod_reversal=cod_reversal)
            rec.refund_payment_id = Accounting.create_customer_refund_payment(rec)
            credit_note = self.env["leaders.vendor.invoice"]._create_credit_note_for_return(rec)
            # Paid when the refund only lowered an unpaid balance; otherwise once the refund payment is posted.
            credit_note.payment_state = "paid" if rec.refund_move_id and not rec.refund_payment_id else "not_paid"
            rec.state = "refunded"
            self.env["leaders.notification"].notify_customer("customer_return_refunded", rec, rec.partner_id)
            rec.message_post(body=_(
                "Refund of %(amount)s processed. Commission given back to the vendor: %(commission)s.",
                amount=rec.refund_amount, commission=rec.commission_refund))


class VendorReturnLine(models.Model):
    _name = "leaders.vendor.return.line"
    _description = "Marketplace Return Line"

    return_id = fields.Many2one("leaders.vendor.return", required=True, ondelete="cascade", index=True)
    currency_id = fields.Many2one(related="return_id.currency_id", store=True)
    vendor_line_id = fields.Many2one("leaders.vendor.order.line", required=True)
    product_id = fields.Many2one(related="vendor_line_id.product_id", store=True)
    quantity = fields.Float(digits="Product Unit of Measure", default=1.0)
    refund_amount = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    commission_refund = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")

    @api.depends("quantity", "vendor_line_id.price_total", "vendor_line_id.product_uom_qty",
                 "vendor_line_id.qty_cancelled", "vendor_line_id.commission_amount")
    def _compute_amounts(self):
        for line in self:
            # price_total is on the kept quantity (the cancelled remainder is out of the order).
            ordered = line.vendor_line_id.product_uom_qty - line.vendor_line_id.qty_cancelled
            ratio = line.quantity / ordered if ordered else 0.0
            line.refund_amount = line.currency_id.round(line.vendor_line_id.price_total * ratio) if line.currency_id else line.vendor_line_id.price_total * ratio
            commission = line.vendor_line_id.commission_amount * ratio
            line.commission_refund = line.currency_id.round(commission) if line.currency_id else commission


class VendorDispute(models.Model):
    _name = "leaders.vendor.dispute"
    _description = "Marketplace Dispute"
    _inherit = ["mail.thread","mail.activity.mixin","leaders.backoffice.mixin"]

    name = fields.Char(default="New", copy=False)
    vendor_order_id = fields.Many2one("leaders.vendor.order", required=True, index=True)
    vendor_id = fields.Many2one(related="vendor_order_id.vendor_id", store=True, index=True)
    partner_id = fields.Many2one(related="vendor_order_id.partner_id", store=True)
    reason = fields.Text(required=True)
    decision = fields.Text()
    state = fields.Selection([
        ("open","Open"),("vendor_reply","Vendor Replied"),("escalated","Escalated"),
        ("resolved","Resolved"),("closed","Closed")
    ], default="open", tracking=True)
    vendor_deadline = fields.Datetime(
        readonly=True, copy=False,
        help="After this date an unanswered dispute escalates to the marketplace.")

    def _leaders_deadline(self):
        """The date the customer is waiting for (vendor reply), or False."""
        self.ensure_one()
        return self.vendor_deadline if self.state == "open" else False

    def _leaders_next_step(self):
        """What happens next, in words, for the customer (translated, one sentence)."""
        self.ensure_one()
        if self.state == "open":
            return _("Waiting for the seller to reply before %s.",
                     format_date(self.env, self.vendor_deadline) if self.vendor_deadline else _("the deadline"))
        if self.state == "vendor_reply":
            return _("The seller replied. Answer them, or ask the marketplace to step in.")
        if self.state == "escalated":
            return _("The marketplace is reviewing your dispute.")
        if self.state == "resolved":
            return _("Resolved by the marketplace. Nothing more to do.")
        return _("Dispute closed. Nothing more to do.")

    @api.model
    def _vendor_sla_deadline(self):
        days = int(self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.dispute_sla_days", 3) or 3)
        return fields.Datetime.now() + timedelta(days=days)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "leaders.vendor.dispute") or "New"
            vals.setdefault("vendor_deadline", self._vendor_sla_deadline())
        records = super().create(vals_list)
        for rec in records:
            self.env["leaders.notification"].notify_vendor("vendor_dispute_opened", rec, rec.vendor_id)
        return records

    def action_vendor_reply(self, message=None):
        """The vendor answers. The clock stops until the customer speaks again."""
        for rec in self:
            if rec.state not in ("open", "vendor_reply", "escalated"):
                raise UserError(_("This dispute is closed."))
            if message:
                rec.message_post(
                    body=message, subtype_xmlid="mail.mt_comment")
            if rec.state == "open":
                rec.state = "vendor_reply"
        return True

    def action_customer_message(self, message):
        """The customer adds to the conversation; the vendor's clock restarts."""
        for rec in self:
            if rec.state in ("resolved", "closed"):
                raise UserError(_("This dispute is closed."))
            rec.message_post(body=message, subtype_xmlid="mail.mt_comment")
            rec.vendor_deadline = rec._vendor_sla_deadline()
            if rec.state == "vendor_reply":
                rec.state = "open"
        return True

    def action_escalate(self):
        """Hand the dispute to the marketplace: the customer, or the cron."""
        for rec in self:
            if rec.state not in ("open", "vendor_reply"):
                raise UserError(_("Only an open dispute can be escalated."))
            rec.state = "escalated"
            rec.message_post(body=_("Dispute escalated to the marketplace for a decision."))
            self.env["leaders.notification"].notify_vendor("vendor_dispute_escalated", rec, rec.vendor_id)
        return True

    def action_resolve(self, decision=None):
        """Reviewer decides. A decision in words is required."""
        self._leaders_check_backoffice("reviewer")
        for rec in self:
            if decision:
                rec.decision = decision
            if not rec.decision:
                raise UserError(_("Please write the marketplace decision before resolving."))
            rec.state = "resolved"
            rec.message_post(body=_("Marketplace decision: %s", rec.decision))
            self.env["leaders.notification"].notify_vendor("vendor_dispute_resolved", rec, rec.vendor_id)
        return True

    def action_close(self):
        """Nothing more to do; no decision was needed."""
        self._leaders_check_backoffice("reviewer")
        for rec in self:
            rec.state = "closed"
        return True

    @api.model
    def cron_escalate_overdue_disputes(self):
        overdue = self.search([
            ("state", "=", "open"),
            ("vendor_deadline", "!=", False),
            ("vendor_deadline", "<", fields.Datetime.now()),
        ])
        for rec in overdue:
            rec.action_escalate()
            rec.message_post(body=_("The vendor did not answer before the deadline."))
        return True
