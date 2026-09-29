"""Customer order cancellation (TASK 29 / A2).

The customer cancels from the order page: the whole checkout (every vendor order
plus the company part) or just one seller's part, while nothing in that scope has
shipped. The vendor orders go through the very same back-office ``action_cancel``
the vendor and the back-office use (escrow reversal, refund draft, deliveries
cancelled = stock reservations released); the company part goes through the
standard sale order cancel, with a draft credit note / refund payment when it was
paid online. Nothing here posts money: finance posts the drafts.

``cancelled_by_type`` (customer / vendor / platform) tells who cancelled. A
customer cancellation is excluded from the vendor KPIs and penalties, and it is
NOT a Cash-on-Delivery refusal: a COD order cancelled by the customer before it
shipped is recorded with ``cancelled_by_type == 'customer'`` and
``payment_method == 'cod'``, which is what a later "COD refusal" counter must
tell apart from a refusal at the door (a refusal happens after shipping).
"""
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .sale import _SHIPPED_GROUP_STATES

CANCELLED_BY_TYPES = [
    ("customer", "Customer"), ("vendor", "Vendor"), ("platform", "Marketplace")]


class LeadersCancelReason(models.Model):
    _name = "leaders.cancel.reason"
    _description = "Order Cancellation Reason"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    applies_to = fields.Selection([
        ("customer", "Customer cancellations"),
        ("vendor", "Vendor cancellations"),
        ("both", "Both"),
    ], required=True, default="customer",
        help="Who can pick this reason. Only the customer cancellation uses the "
             "list today; the value is kept for vendor cancellations.")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    leaders_cancelled_by_type = fields.Selection(
        CANCELLED_BY_TYPES, string="Cancelled By", readonly=True, copy=False, index=True)
    leaders_cancel_reason_id = fields.Many2one(
        "leaders.cancel.reason", string="Cancellation Reason", readonly=True, copy=False)
    leaders_cancel_note = fields.Text(string="Cancellation Note", readonly=True, copy=False)
    leaders_cancel_date = fields.Datetime(string="Cancellation Date", readonly=True, copy=False)
    leaders_refund_payment_id = fields.Many2one(
        "account.payment", string="Cancellation Refund", readonly=True, copy=False,
        help="Draft refund prepared when the company part of a checkout paid online "
             "was cancelled by the customer. Finance posts it once the money is sent.")

    def _leaders_company_part_active(self):
        """The company part of a checkout is a real, still open sales order."""
        self.ensure_one()
        return (self.state in ("draft", "sent", "sale") and not self.marketplace_vendor_only
                and bool(self._marketplace_company_lines()))

    def _leaders_company_part_shipped(self):
        self.ensure_one()
        return any(p.state == "done" for p in self.picking_ids)

    def _leaders_cancel_company_part(self, reason, note=None):
        """Cancel the company part as the customer asked; prepare the refund if paid online."""
        self.ensure_one()
        if self.state == "cancel":
            return False
        if self._leaders_company_part_shipped():
            raise UserError(_("The company part of order %s is already shipped and can no longer be cancelled.", self.name))
        paid_online = self.leaders_payment_method != "cod"
        transactions = self.transaction_ids.filtered(lambda t: t.state == "done")
        posted_invoices = self.invoice_ids.filtered(
            lambda m: m.state == "posted" and m.move_type == "out_invoice")
        # What the customer already paid for this part, worked out before the cancel.
        paid = 0.0
        credit_notes = self.env["account.move"]
        if paid_online and transactions:
            if posted_invoices:
                paid = sum(inv.amount_total - inv.amount_residual for inv in posted_invoices)
            else:
                paid = min(self.amount_total, sum(transactions.mapped("amount")))
        self._action_cancel()
        for invoice in posted_invoices:
            # Draft credit note: finance reviews and posts it.
            credit_notes |= invoice._reverse_moves([{
                "ref": _("Cancelled order %s", self.name),
                "date": fields.Date.context_today(self),
            }], cancel=False)
        self.write({
            "leaders_cancelled_by_type": "customer",
            "leaders_cancel_date": fields.Datetime.now(),
            "leaders_cancel_note": note or False,
        })
        currency = self.currency_id
        if paid and not currency.is_zero(paid):
            self._leaders_prepare_company_refund(transactions, paid, credit_notes)
        elif credit_notes:
            self.message_post(body=_("Draft credit note prepared for the cancelled order."))
        return True

    def _leaders_prepare_company_refund(self, transactions, amount, credit_notes):
        self.ensure_one()
        if self.leaders_refund_payment_id:
            return self.leaders_refund_payment_id
        tx = transactions.filtered(lambda t: t.provider_id.journal_id)[:1]
        journal = tx.provider_id.journal_id or self.env["account.journal"].sudo().search([
            ("type", "=", "bank"), ("company_id", "=", self.company_id.id)], limit=1)
        if not journal:
            self.message_post(body=_(
                "No bank journal found to prepare the cancellation refund payment."))
            return False
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "outbound",
            "partner_type": "customer",
            "partner_id": self.partner_id.commercial_partner_id.id,
            "amount": amount,
            "currency_id": self.currency_id.id,
            "journal_id": journal.id,
            "memo": _("Refund cancelled %s", self.name),
            "leaders_cancelled_sale_id": self.id,
        })
        self.leaders_refund_payment_id = payment.id
        self.message_post(body=_(
            "Customer refund payment of %(amount)s prepared for the cancelled order; "
            "post it once the money is sent.", amount=amount))
        if credit_notes:
            self.message_post(body=_(
                "Draft credit note prepared: post it before the refund payment."))
        return payment


class VendorOrder(models.Model):
    _inherit = "leaders.vendor.order"

    cancelled_by_type = fields.Selection(
        CANCELLED_BY_TYPES, readonly=True, copy=False, index=True,
        help="Who cancelled the order. Customer cancellations are left out of the "
             "vendor KPIs and penalties. Empty on orders cancelled before this existed.")
    cancel_reason_id = fields.Many2one(
        "leaders.cancel.reason", string="Cancellation Reason", readonly=True, copy=False)
    cancel_note = fields.Text(readonly=True, copy=False)
    cancel_date = fields.Datetime(readonly=True, copy=False)

    # ---------------------------------------------------------------- rules
    @api.model
    def _customer_cancel_enabled(self):
        return self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.customer_can_cancel_orders") in ("True", "true", "1")

    @api.model
    def _customer_cancel_window_hours(self):
        try:
            return max(int(self.env["ir.config_parameter"].sudo().get_param(
                "leaders_marketplace.cancel_window_hours") or 0), 0)
        except (TypeError, ValueError):
            return 0

    def _customer_cancel_parts(self):
        """Every vendor order of this order's checkout (just itself when it has none)."""
        self.ensure_one()
        return self.sale_order_id.sudo().vendor_order_ids or self

    def _customer_part_shipped(self):
        self.ensure_one()
        return (self.state in ("fulfilled", "completed", "partially_fulfilled")
                or bool(self.fulfillment_group_ids.filtered(
                    lambda g: g.state in _SHIPPED_GROUP_STATES)))

    def _customer_cancel_scope_status(self, scope):
        """Why the customer cannot cancel this scope, or ``None`` when they can.

        ``scope`` is ``vendor`` (this seller's part) or ``all`` (whole checkout).
        Returns one of ``off``, ``cancelled``, ``shipped``, ``window``.
        """
        self.ensure_one()
        if not self._customer_cancel_enabled():
            return "off"
        if scope == "all":
            parts = self._customer_cancel_parts()
            sale = self.sale_order_id.sudo()
            company = sale if sale and sale._leaders_company_part_active() else sale.browse()
            active = parts.filtered(lambda v: v.state != "cancelled")
            if not active and not company:
                return "cancelled"
            if any(v._customer_part_shipped() for v in parts) or (
                    sale and sale._leaders_company_part_shipped()):
                return "shipped"
            started = sale.date_order if sale else False
        else:
            if self.state == "cancelled":
                return "cancelled"
            if self._customer_part_shipped():
                return "shipped"
            started = False
        started = started or self.date_order
        hours = self._customer_cancel_window_hours()
        if hours and started and fields.Datetime.now() > started + timedelta(hours=hours):
            return "window"
        return None

    def _customer_cancel_info(self):
        """What the customer's order page needs to draw the cancel buttons."""
        self.ensure_one()
        enabled = self._customer_cancel_enabled()
        info = {"enabled": enabled, "all": None, "vendor": None, "has_other_parts": False}
        if not enabled:
            return info
        info["all"] = self._customer_cancel_scope_status("all")
        info["vendor"] = self._customer_cancel_scope_status("vendor")
        sale = self.sale_order_id.sudo()
        others = self._customer_cancel_parts().filtered(
            lambda v: v != self and v.state != "cancelled")
        info["has_other_parts"] = bool(others) or bool(
            sale and sale._leaders_company_part_active())
        return info

    def _customer_cancel_reason(self, reason_id):
        Reason = self.env["leaders.cancel.reason"].sudo()
        try:
            reason_id = int(reason_id or 0)
        except (TypeError, ValueError):
            reason_id = 0
        reason = Reason.search([
            ("id", "=", reason_id), ("active", "=", True),
            ("applies_to", "in", ("customer", "both"))], limit=1)
        if not reason:
            raise UserError(_("Please choose a reason for cancelling."))
        return reason

    # -------------------------------------------------------------- the action
    def action_customer_cancel(self, reason_id, note=None, scope="vendor"):
        """The customer cancels this seller's part or the whole checkout.

        Ownership is the caller's job (the portal route). Everything is checked
        before anything changes, so a refusal leaves the order untouched.
        """
        self.ensure_one()
        if scope not in ("vendor", "all"):
            raise UserError(_("Unknown cancellation scope."))
        status = self._customer_cancel_scope_status(scope)
        if status == "off":
            raise UserError(_("Customers cannot cancel orders on this marketplace."))
        if status == "cancelled":
            raise UserError(_("This order is already cancelled."))
        if status == "shipped":
            raise UserError(_(
                "This order has already shipped and can no longer be cancelled. "
                "Request a return after delivery."))
        if status == "window":
            raise UserError(_(
                "The time to cancel this order has passed (%s hours after ordering).",
                self._customer_cancel_window_hours()))
        reason = self._customer_cancel_reason(reason_id)
        note = (note or "").strip() or False
        sale = self.sale_order_id.sudo()
        if scope == "all":
            orders = self._customer_cancel_parts().filtered(lambda v: v.state != "cancelled")
        else:
            orders = self
        orders = orders.sudo()
        text = reason.name if not note else "%s: %s" % (reason.name, note)
        now = fields.Datetime.now()
        for order in orders:
            order.write({
                "cancelled_by_type": "customer", "cancel_reason_id": reason.id,
                "cancel_note": note, "cancel_date": now, "cancel_reason": text,
            })
        # The customer gets one confirmation (below), not the generic cancel mail per order.
        orders.with_context(leaders_customer_cancel=True).action_cancel()
        company_cancelled = False
        if scope == "all" and sale and sale._leaders_company_part_active():
            sale.write({"leaders_cancel_reason_id": reason.id})
            company_cancelled = sale._leaders_cancel_company_part(reason, note)
            if company_cancelled:
                sale.message_post(body=_(
                    "Order cancelled by the customer. Reason: %(reason)s", reason=text))
        products = orders.line_ids.product_id
        if products:
            self.env["leaders.marketplace.offer"].sudo()._recompute_buybox_winner(
                product_ids=products.ids)
        Notification = self.env["leaders.notification"]
        for order in orders:
            order.message_post(body=_(
                "Order cancelled by the customer. Reason: %(reason)s", reason=text))
            Notification.notify_vendor("vendor_order_cancelled_by_customer", order, order.vendor_id)
        Notification.notify_customer(
            "customer_order_cancelled_by_customer", self, self.partner_id)
        return True
