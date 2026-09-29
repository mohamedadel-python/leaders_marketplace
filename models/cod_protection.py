"""Cash-on-delivery protection (TASK 29 / A3).

Three independent, default-off protections around the COD checkout option:

* ``cod_max_amount``: COD is hidden/refused above this order total (0 = no limit).
* ``cod_require_otp``: the customer must verify a phone number with a 6-digit code
  (5 minutes, 5 attempts, stored hashed) before COD is accepted for that order.
* ``cod_refusal_block_count``: COD is hidden for a customer who refused that many
  COD parcels at the door (0 = off). Managers can reset the counter.

"Customer refused" (``FulfillmentGroup.action_customer_refused``) is the event that
feeds the counter. It is a post-shipping event and is unrelated to the customer
cancelling before shipping (A2, ``cancelled_by_type == 'customer'``), which never
counts as a refusal.
"""
import hmac
import logging
import re
import secrets
from datetime import timedelta

from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

OTP_DIGITS = 6
OTP_VALIDITY_MINUTES = 5
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60
OTP_MAX_SENDS_PER_HOUR = 5
_PHONE_RE = re.compile(r"^\+?[0-9]{8,15}$")


def _param(env, key, default=None):
    return env["ir.config_parameter"].sudo().get_param("leaders_marketplace." + key, default)


class CodOtp(models.Model):
    _name = "leaders.cod.otp"
    _description = "Cash on Delivery Phone Verification Code"
    _order = "id desc"

    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade", index=True)
    phone = fields.Char(required=True)
    salt = fields.Char(required=True)
    code_hash = fields.Char(required=True, help="Salted HMAC of the code; the code itself is never stored.")
    expires_at = fields.Datetime(required=True)
    attempts = fields.Integer(default=0)
    verified = fields.Boolean(default=False)

    # -- helpers ---------------------------------------------------------
    @api.model
    def _leaders_normalize_phone(self, phone):
        phone = re.sub(r"[\s\-().]", "", phone or "")
        if not _PHONE_RE.match(phone):
            raise UserError(_("Please enter a valid mobile number, digits only with an optional leading +."))
        return phone

    @api.model
    def _leaders_hash(self, salt, order_id, phone, code):
        return tools.hmac(self.env(su=True), "leaders_cod_otp", (salt, order_id, phone, code))

    # -- sending ---------------------------------------------------------
    @api.model
    def _leaders_send_otp(self, phone, code, partner=None):
        """Deliver ``code`` to ``phone``; returns True when a message was handed off.

        Uses the Odoo ``sms`` module when it is installed (never a dependency of
        this addon). Otherwise, or when the SMS gateway fails, it falls back to
        a log line and an email to the customer. THE FALLBACK LOGS THE CODE ON
        PURPOSE (INFO level): with no SMS gateway configured that is the only way
        an administrator can complete a COD checkout during a demo or test. Every
        other code path avoids logging the code.
        """
        body = _("Your Cash on Delivery verification code is %(code)s. It is valid for %(minutes)s minutes.",
                 code=code, minutes=OTP_VALIDITY_MINUTES)
        if "sms.sms" in self.env:
            try:
                sms = self.env["sms.sms"].sudo().create({"number": phone, "body": body})
                sms.send(unlink_failed=False, unlink_sent=False, raise_exception=False)
                if sms.state != "error":
                    return True
                _logger.warning("SMS gateway refused the COD code for %s; using the fallback.", phone)
            except Exception:  # noqa: BLE001 - never let a gateway problem break checkout
                _logger.exception("Could not send the COD code by SMS to %s; using the fallback.", phone)
        _logger.info("COD verification code for %s: %s (no SMS gateway; fallback)", phone, code)
        email = partner.email if partner else False
        if not email:
            return False
        self.env["mail.mail"].sudo().create({
            "subject": _("Your Cash on Delivery verification code"),
            "body_html": "<p>%s</p>" % tools.html_escape(body),
            "email_to": email,
            "auto_delete": True,
        })
        return True

    # -- lifecycle -------------------------------------------------------
    @api.model
    def leaders_send_code(self, order, phone):
        """Create a fresh code for (order, phone) and send it. Returns the record."""
        order = order.sudo()
        phone = self._leaders_normalize_phone(phone)
        now = fields.Datetime.now()
        recent = self.sudo().search([
            ("order_id", "=", order.id), ("create_date", ">=", now - timedelta(hours=1))])
        if len(recent) >= OTP_MAX_SENDS_PER_HOUR:
            raise UserError(_("Too many codes were requested. Please try again later."))
        if recent and recent[0].create_date > now - timedelta(seconds=OTP_RESEND_SECONDS):
            raise UserError(_("Please wait a minute before asking for a new code."))
        code = "".join(secrets.choice("0123456789") for _i in range(OTP_DIGITS))
        salt = secrets.token_hex(8)
        record = self.sudo().create({
            "order_id": order.id, "phone": phone, "salt": salt,
            "code_hash": self._leaders_hash(salt, order.id, phone, code),
            "expires_at": now + timedelta(minutes=OTP_VALIDITY_MINUTES),
        })
        if not self._leaders_send_otp(phone, code, partner=order.partner_id):
            record.unlink()
            raise UserError(_("We could not send the code. Please check your number or email and try again."))
        return record

    @api.model
    def leaders_verify_code(self, order, phone, code):
        """True when ``code`` is the live code of (order, phone); marks the order verified."""
        order = order.sudo()
        phone = self._leaders_normalize_phone(phone)
        record = self.sudo().search([
            ("order_id", "=", order.id), ("phone", "=", phone), ("verified", "=", False)], limit=1)
        if not record:
            raise UserError(_("Ask for a verification code first."))
        if record.expires_at < fields.Datetime.now():
            raise UserError(_("This code has expired. Please ask for a new one."))
        if record.attempts >= OTP_MAX_ATTEMPTS:
            raise UserError(_("Too many wrong attempts. Please ask for a new code."))
        code = re.sub(r"\s", "", code or "")
        expected = self._leaders_hash(record.salt, order.id, phone, code)
        if not (code.isdigit() and len(code) == OTP_DIGITS
                and hmac.compare_digest(expected, record.code_hash)):
            record.attempts += 1
            # Returns (not raises) so the attempt counter is kept by the transaction.
            return False
        record.verified = True
        order.write({"leaders_cod_phone_verified": True, "leaders_cod_phone": phone})
        return True

    @api.autovacuum
    def _gc_leaders_cod_otp(self):
        self.sudo().search([("create_date", "<", fields.Datetime.now() - timedelta(days=1))]).unlink()


class SaleOrder(models.Model):
    _inherit = "sale.order"

    leaders_cod_phone_verified = fields.Boolean(string="COD Phone Verified", copy=False, readonly=True)
    leaders_cod_phone = fields.Char(string="COD Verified Phone", copy=False, readonly=True)

    # -- settings ---------------------------------------------------------
    @api.model
    def _leaders_cod_max_amount(self):
        try:
            return max(float(_param(self.env, "cod_max_amount") or 0.0), 0.0)
        except (TypeError, ValueError):
            return 0.0

    @api.model
    def _leaders_cod_require_otp(self):
        return _param(self.env, "cod_require_otp") in ("True", "true", "1")

    @api.model
    def _leaders_cod_refusal_block_count(self):
        try:
            return max(int(_param(self.env, "cod_refusal_block_count") or 0), 0)
        except (TypeError, ValueError):
            return 0

    # -- availability -----------------------------------------------------
    def _leaders_cod_availability(self):
        """(allowed, message) for paying this order in cash on delivery."""
        self.ensure_one()
        if not _param(self.env, "cod_enabled"):
            return False, _("Cash on Delivery is not available.")
        limit = self._leaders_cod_max_amount()
        if limit:
            company = self.company_id or self.env.company
            total = self.currency_id._convert(
                self.amount_total, company.currency_id, company, self.date_order or fields.Date.today())
            if total > limit:
                return False, _(
                    "Cash on Delivery is not available for orders above %(limit)s. "
                    "Please pay online.",
                    limit=tools.format_amount(self.env, limit, company.currency_id))
        if self.partner_id.sudo().commercial_partner_id._leaders_cod_blocked():
            return False, _(
                "Cash on Delivery is not available for your account. Please pay online.")
        return True, ""

    def _leaders_cod_check_allowed_for_otp(self):
        """Same as availability, without the OTP requirement itself."""
        self.ensure_one()
        allowed, message = self._leaders_cod_availability()
        if not allowed:
            raise UserError(message)
        return True

    def _leaders_cod_check_allowed(self):
        """Raise UserError unless COD may be used for this order right now."""
        for order in self:
            allowed, message = order._leaders_cod_availability()
            if not allowed:
                raise UserError(message)
            if self._leaders_cod_require_otp() and not order.leaders_cod_phone_verified:
                raise UserError(_("Please verify your phone number to pay Cash on Delivery."))
        return True


class ResPartner(models.Model):
    _inherit = "res.partner"

    leaders_cod_refusal_count = fields.Integer(
        string="COD Refusals", readonly=True, copy=False,
        help="Cash-on-delivery parcels this customer refused at the door. Customer "
             "cancellations before shipping do not count.")

    def _leaders_cod_blocked(self):
        """True when this customer reached the refusal threshold (0 = feature off)."""
        self.ensure_one()
        threshold = self.env["sale.order"]._leaders_cod_refusal_block_count()
        return bool(threshold and self.commercial_partner_id.leaders_cod_refusal_count >= threshold)

    def action_reset_cod_refusals(self):
        if not self.env.su and not self.env.user.has_group("leaders_marketplace.group_marketplace_manager"):
            raise AccessError(_("Only marketplace managers can reset the COD refusal counter."))
        for partner in self:
            old = partner.leaders_cod_refusal_count
            partner.sudo().leaders_cod_refusal_count = 0
            partner.sudo().message_post(body=_(
                "COD refusal counter reset by %(user)s (was %(old)s).",
                user=self.env.user.display_name, old=old))
        return True


class StockPicking(models.Model):
    _inherit = "stock.picking"

    leaders_refused_group_id = fields.Many2one(
        "leaders.fulfillment.group", string="Refused Shipment", index=True, copy=False,
        help="The shipment the customer refused at the door; this receipt brings the goods back.")


class FulfillmentGroup(models.Model):
    _name = "leaders.fulfillment.group"
    _inherit = ["leaders.fulfillment.group", "leaders.backoffice.mixin"]

    is_cod = fields.Boolean(compute="_compute_is_cod", string="Cash on Delivery")
    refused_at_delivery = fields.Boolean(readonly=True, copy=False, index=True)
    refusal_picking_ids = fields.One2many("stock.picking", "leaders_refused_group_id", string="Refusal Returns")

    @api.depends("vendor_order_id.payment_method")
    def _compute_is_cod(self):
        for rec in self:
            rec.is_cod = rec.vendor_order_id.payment_method == "cod"

    def _leaders_create_refusal_picking(self):
        """Receipt bringing the refused goods back to the group's source location.

        Same pattern as vendor returns: from Customers into the source location,
        owner stays the vendor. FBM goods are received at once (the vendor
        answers for their own courier); an FBL receipt is left open for the
        warehouse to validate when the parcel is back on the shelf.
        """
        self.ensure_one()
        customer_location = self.env.ref("stock.stock_location_customers")
        order = self.vendor_order_id
        vendor = self.vendor_id.commercial_partner_id
        picking = self.env["stock.picking"].sudo().create({
            "picking_type_id": self._picking_type().warehouse_id.in_type_id.id,
            "location_id": customer_location.id,
            "location_dest_id": self.source_location_id.id,
            "partner_id": (order.partner_shipping_id or order.partner_id).id,
            "origin": _("Refused at delivery: %(group)s (%(order)s)", group=self.name, order=order.name),
            "owner_id": vendor.id,
            "leaders_refused_group_id": self.id,
            "move_ids": [(0, 0, {
                "name": line.name,
                "product_id": line.product_id.id,
                "product_uom_qty": line.product_uom_qty,
                "product_uom": (line.product_uom or line.product_id.uom_id).id,
                "location_id": customer_location.id,
                "location_dest_id": self.source_location_id.id,
                "restrict_partner_id": vendor.id,
                "origin": order.name,
            }) for line in self.line_ids],
        })
        picking.action_confirm()
        if self.fulfillment_type == "fbm":
            for move in picking.move_ids:
                move.quantity = move.product_uom_qty
            picking.move_ids.picked = True
            picking.with_context(skip_backorder=True, skip_sms=True, skip_immediate=True)._action_done()
        return picking

    def action_customer_refused(self, reason=None, by=None):
        """The customer refused a shipped Cash-on-Delivery parcel at the door.

        Only a ``shipped`` group qualifies. A ``delivery_reported`` group (FBM in
        customer-confirm mode) is refused on purpose: the vendor has already
        claimed the parcel was handed over, so a refusal then is a delivery
        dispute (existing flow), not a door refusal.

        Effects: the goods come back to the source location (owner stays the
        vendor); the group and its vendor order are cancelled with
        ``cancelled_by_type = customer`` ("refused at delivery"); no earning, no
        wallet transaction and no refund (COD money was never held); an existing
        collection entry is reversed by the shared cancel path; the customer's
        ``leaders_cod_refusal_count`` grows by one.
        """
        self._leaders_check_backoffice("manager")
        user = by or self.env.user
        text = (reason or "").strip()
        reason_record = self.env.ref("leaders_marketplace.cancel_reason_refused_at_delivery", raise_if_not_found=False)
        for rec in self:
            order = rec.vendor_order_id
            if order.payment_method != "cod":
                raise UserError(_("Only Cash on Delivery shipments can be refused at the door."))
            if rec.state != "shipped":
                raise UserError(_("Only a shipped group can be marked as refused by the customer."))
            blocked = order.fulfillment_group_ids.filtered(
                lambda g: g != rec and g.state in ("delivery_reported", "delivered"))
            if blocked:
                raise UserError(_(
                    "%(order)s already has a delivered shipment; handle this refusal as a return.",
                    order=order.name))
        for order in self.vendor_order_id:
            shipped = order.fulfillment_group_ids.filtered(lambda g: g.state == "shipped")
            label = _("Refused at delivery")
            order.sudo().write({
                "cancelled_by_type": "customer",
                "cancel_reason_id": reason_record.id if reason_record else False,
                "cancel_note": text or False,
                "cancel_reason": "%s: %s" % (label, text) if text else label,
                "cancelled_by": user.id,
                "cancel_date": fields.Datetime.now(),
            })
            # Receipts first: the shared cancel path only cancels open pickings.
            pickings = self.env["stock.picking"]
            for group in shipped:
                pickings |= group._leaders_create_refusal_picking()
            order.sudo().with_context(leaders_cod_refusal=True).action_cancel()
            shipped.sudo().write({"refused_at_delivery": True})
            customer = order.partner_id.commercial_partner_id.sudo()
            customer.leaders_cod_refusal_count += 1
            order.sudo().message_post(body=_(
                "The customer refused the Cash on Delivery parcel at the door (%(groups)s). "
                "Goods returning to %(where)s through %(pickings)s. No earning was created.",
                groups=", ".join(shipped.mapped("name")),
                where=", ".join(shipped.mapped("source_location_id.display_name")),
                pickings=", ".join(pickings.mapped("name"))))
        return True
