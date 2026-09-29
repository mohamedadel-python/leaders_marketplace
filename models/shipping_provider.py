"""B6: provider-agnostic shipping carrier framework.

A ``leaders.shipping.provider`` is a carrier integration point with two
overridable hooks:

* ``_create_shipment(group)``  -> ``dict(awb=, label_pdf=bytes|None, tracking_url=)``
* ``_parse_webhook(payload, headers)`` -> list of normalized events
  ``dict(event_id=, awb=, group_ref=, status=, raw_status=, occurred_at=)``

Normalized statuses: created, picked_up, in_transit, delivered, returned,
refused, failed. Two providers ship with the module: ``manual`` (today's
behaviour: the vendor types the tracking number, no label, no webhook) and
``dummy`` (deterministic fake AWB + tiny PDF label + simple JSON webhook, used
by the tests; its data record is inactive).

Extension point (a carrier addon, no change to this module)::

    # class ShippingProviderAcme(models.Model):
    #     _inherit = "leaders.shipping.provider"
    #
    #     provider_type = fields.Selection(
    #         selection_add=[("acme", "ACME Express")], ondelete={"acme": "set default"})
    #
    #     def _create_shipment(self, group):
    #         if self.provider_type != "acme":
    #             return super()._create_shipment(group)
    #         # call the carrier API with self.credentials (JSON text) here
    #         return {"awb": "...", "label_pdf": b"%PDF...", "tracking_url": "https://..."}
    #
    #     def _parse_webhook(self, payload, headers):
    #         if self.provider_type != "acme":
    #             return super()._parse_webhook(payload, headers)
    #         # payload is the RAW request body (bytes), already HMAC verified
    #         return [{"event_id": "...", "awb": "...", "status": "delivered",
    #                  "raw_status": "DLV", "occurred_at": "2026-01-01T10:00:00Z"}]
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

PARAM_ENABLED = "leaders_marketplace.shipping_providers_enabled"
NORMALIZED_STATUSES = [
    ("created", "Created"), ("picked_up", "Picked Up"), ("in_transit", "In Transit"),
    ("delivered", "Delivered"), ("returned", "Returned"), ("refused", "Refused"),
    ("failed", "Failed"),
]
_STATUS_RANK = {"created": 0, "picked_up": 1, "in_transit": 2, "delivered": 3,
                "returned": 3, "refused": 3, "failed": 1}
WEBHOOK_MAX_BYTES = 1024 * 1024
WEBHOOK_MAX_AGE = 300  # seconds, only enforced when X-Timestamp is sent
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class LeadersShippingProvider(models.Model):
    _name = "leaders.shipping.provider"
    _description = "Shipping Provider"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    code = fields.Char(required=True, copy=False, index=True,
                       help="Used in the webhook URL: /api/v1/shipping/webhook/<code>.")
    provider_type = fields.Selection(
        [("manual", "Manual (vendor enters tracking)"), ("dummy", "Dummy (tests)")],
        required=True, default="manual",
        help="Carrier addons add their own values (selection_add) and override "
             "_create_shipment and _parse_webhook.")
    active = fields.Boolean(default=True)
    credentials = fields.Text(
        groups="leaders_marketplace.group_marketplace_manager", copy=False,
        help="Carrier credentials (JSON text). Only marketplace managers can read it; "
             "never exposed to portal or API users.")
    webhook_secret = fields.Char(
        groups="leaders_marketplace.group_marketplace_manager", copy=False,
        help="Shared secret for the HMAC-SHA256 signature (header X-Signature, hex) of the "
             "raw webhook body.")
    webhook_url = fields.Char(compute="_compute_webhook_url")
    event_ids = fields.One2many("leaders.shipping.event", "provider_id")

    _sql_constraints = [("code_uniq", "unique(code)", "The provider code must be unique.")]

    @api.constrains("code")
    def _check_code(self):
        for rec in self:
            if not CODE_RE.match(rec.code or ""):
                raise ValidationError(_(
                    "The provider code may only contain lowercase letters, digits, - and _."))

    @api.depends("code")
    def _compute_webhook_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for rec in self:
            rec.webhook_url = "%s/api/v1/shipping/webhook/%s" % (base, rec.code) if rec.code else False

    # ------------------------------------------------------------------ settings
    @api.model
    def _leaders_enabled(self):
        value = self.env["ir.config_parameter"].sudo().get_param(PARAM_ENABLED)
        return value in ("True", "true", "1")

    @api.model
    def _leaders_portal_providers(self):
        """Providers a vendor may pick to create a label (none while the setting is off)."""
        if not self._leaders_enabled():
            return self.sudo().browse()
        return self.sudo().search([("active", "=", True), ("provider_type", "!=", "manual")])

    def action_generate_secret(self):
        if not self.env.su and not self.env.user.has_groups("leaders_marketplace.group_marketplace_manager"):
            raise AccessError(_("Only marketplace managers can generate a webhook secret."))
        for rec in self:
            rec.sudo().webhook_secret = secrets.token_hex(32)
        return True

    # ------------------------------------------------------------------ hooks
    def _create_shipment(self, group):
        """Create the shipment at the carrier. Override per provider type.

        Returns ``dict(awb=str|False, label_pdf=bytes|None, tracking_url=str|False)``.
        """
        self.ensure_one()
        if self.provider_type == "dummy":
            awb = "DUM%s" % hashlib.sha1((group.name or str(group.id)).encode()).hexdigest()[:10].upper()
            return {"awb": awb, "label_pdf": self._dummy_label(awb, group),
                    "tracking_url": "https://dummy.example/track/%s" % awb}
        # manual: the vendor's own tracking number, no label.
        return {"awb": group.tracking_ref or False, "label_pdf": None, "tracking_url": False}

    def _parse_webhook(self, payload, headers):
        """Turn the raw (HMAC verified) body into normalized events. Override per type.

        ``payload`` is the raw request body (bytes). Returns a list of dicts with
        ``event_id``, ``awb`` (or ``group_ref``), ``status`` (normalized),
        ``raw_status`` and ``occurred_at``. Raise ValueError for a malformed body.
        """
        self.ensure_one()
        if self.provider_type != "dummy":
            return []
        data = json.loads(payload or b"{}")
        items = data.get("events") if isinstance(data, dict) and "events" in data else [data]
        if not isinstance(items, list):
            raise ValueError("events must be a list")
        events = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("event must be an object")
            raw = str(item.get("raw_status") or item.get("status") or "")
            status = str(item.get("status") or "")
            if status not in dict(NORMALIZED_STATUSES):
                raise ValueError("unknown status")
            events.append({
                "event_id": str(item.get("id") or item.get("event_id") or ""),
                "awb": str(item.get("awb") or ""),
                "group_ref": str(item.get("group_ref") or ""),
                "status": status, "raw_status": raw,
                "occurred_at": str(item.get("occurred_at") or ""),
            })
        return events

    @staticmethod
    def _dummy_label(awb, group):
        text = "DUMMY LABEL %s %s" % (awb, group.name or "")
        stream = "BT /F1 14 Tf 20 100 Td (%s) Tj ET" % text.replace("(", "").replace(")", "")
        objs = [
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 150] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            "<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        out, offsets = "%PDF-1.4\n", []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += "%d 0 obj\n%s\nendobj\n" % (i, body)
        xref = len(out)
        out += "xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        out += "".join("%010d 00000 n \n" % o for o in offsets)
        out += "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
        return out.encode("latin-1")

    # ------------------------------------------------------------------ webhook
    def _verify_signature(self, body, headers):
        """True when X-Signature is the hex HMAC-SHA256 of the raw body (constant time)."""
        self.ensure_one()
        secret = (self.sudo().webhook_secret or "").encode()
        signature = (headers.get("X-Signature") or "").strip()
        if signature.lower().startswith("sha256="):
            signature = signature[7:]
        if not secret or not signature:
            return False
        signed = body
        timestamp = (headers.get("X-Timestamp") or "").strip()
        if timestamp:
            try:
                if abs(time.time() - int(timestamp)) > WEBHOOK_MAX_AGE:
                    return False
            except ValueError:
                return False
            signed = timestamp.encode() + b"." + body
        expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.lower())

    def _process_webhook(self, body, headers):
        """Parse and apply a verified webhook body. Returns (counts dict, http status)."""
        self.ensure_one()
        provider = self.sudo()
        try:
            events = provider._parse_webhook(body, headers)
        except Exception:  # malformed body: never leak parser internals
            return {"error": "invalid_payload"}, 400
        counts = {"received": len(events), "applied": 0, "ignored": 0, "duplicates": 0, "unknown": 0}
        body_hash = hashlib.sha256(body).hexdigest()
        for event in events:
            counts[provider._apply_event(event, body_hash)] += 1
        status = 202 if counts["received"] and counts["unknown"] == counts["received"] else 200
        return counts, status

    def _apply_event(self, event, body_hash):
        """Idempotently apply one normalized event. Returns applied/ignored/duplicates/unknown."""
        self.ensure_one()
        Event = self.env["leaders.shipping.event"].sudo()
        Group = self.env["leaders.fulfillment.group"].sudo()
        status = event.get("status")
        event_id = event.get("event_id") or hashlib.sha256(
            ("%s|%s|%s|%s" % (body_hash, event.get("awb"), event.get("group_ref"), status)).encode()
        ).hexdigest()
        if Event.search_count([("provider_id", "=", self.id), ("event_id", "=", event_id)]):
            return "duplicates"
        awb = (event.get("awb") or "").strip()
        ref = (event.get("group_ref") or "").strip()
        group = Group.browse()
        if awb:
            group = Group.search([("shipping_provider_id", "=", self.id), ("awb", "=", awb)], limit=1)
        if not group and ref:
            group = Group.search([("shipping_provider_id", "=", self.id), ("name", "=", ref)], limit=1)
        vals = {
            "provider_id": self.id, "event_id": event_id, "group_id": group.id or False,
            "awb": awb or False, "raw_status": (event.get("raw_status") or "")[:100],
            "status": status if status in dict(NORMALIZED_STATUSES) else False,
            "payload_hash": body_hash, "occurred_at": (event.get("occurred_at") or "")[:40],
        }
        try:
            with self.env.cr.savepoint():
                record = Event.create(vals)
        except Exception:  # unique race with a concurrent replay
            return "duplicates"
        if not group:
            record.note = "Unknown shipment, ignored."
            return "unknown"
        try:
            with self.env.cr.savepoint():
                outcome = group._apply_shipping_status(self, status)
        except (UserError, ValidationError, AccessError) as error:
            record.write({"processed": False, "note": str(error.args[0])[:500]})
            return "ignored"
        record.write({"processed": outcome[0], "note": outcome[1]})
        return "applied" if outcome[0] else "ignored"


class LeadersShippingEvent(models.Model):
    _name = "leaders.shipping.event"
    _description = "Shipping Event"
    _order = "id desc"

    provider_id = fields.Many2one("leaders.shipping.provider", required=True, ondelete="cascade", index=True)
    group_id = fields.Many2one("leaders.fulfillment.group", ondelete="cascade", index=True)
    event_id = fields.Char(required=True, index=True)
    awb = fields.Char()
    raw_status = fields.Char()
    status = fields.Selection(NORMALIZED_STATUSES)
    occurred_at = fields.Char()
    payload_hash = fields.Char()
    received_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    processed = fields.Boolean(default=True, help="False when the event was logged but no action was taken.")
    note = fields.Char()

    _sql_constraints = [("provider_event_uniq", "unique(provider_id, event_id)",
                         "This shipping event was already received.")]


class FulfillmentGroupShipping(models.Model):
    _inherit = "leaders.fulfillment.group"

    shipping_provider_id = fields.Many2one("leaders.shipping.provider", ondelete="restrict", copy=False)
    awb = fields.Char(string="AWB", copy=False, index=True)
    label_attachment_id = fields.Many2one("ir.attachment", string="Shipping Label", copy=False,
                                          ondelete="set null")
    tracking_url = fields.Char(copy=False)
    shipping_status = fields.Selection(NORMALIZED_STATUSES, copy=False, tracking=True)
    shipping_event_ids = fields.One2many("leaders.shipping.event", "group_id", string="Shipping Events")

    _SHIPPING_OPEN_STATES = ("allocated", "picking", "packing", "ready")

    def action_create_shipment(self):
        """Create the shipment at the chosen provider and store AWB, label and tracking."""
        self._leaders_check_backoffice("manager")
        if not self.env["leaders.shipping.provider"]._leaders_enabled():
            raise UserError(_("Shipping providers are not enabled."))
        for rec in self:
            if rec.state not in rec._SHIPPING_OPEN_STATES:
                raise UserError(_("A shipment can only be created before the group ships."))
            if rec.awb:
                raise UserError(_("A shipment was already created for %s.", rec.name))
            provider = rec.shipping_provider_id or self.env["leaders.shipping.provider"].sudo().search(
                [("code", "=", "manual")], limit=1)
            if not provider or not provider.active:
                raise UserError(_("Choose an active shipping provider."))
            result = provider.sudo()._create_shipment(rec) or {}
            awb = (result.get("awb") or "").strip()
            vals = {"shipping_provider_id": provider.id, "tracking_url": result.get("tracking_url") or False}
            if awb:
                vals.update({"awb": awb, "tracking_ref": awb, "shipping_status": "created"})
            label = result.get("label_pdf")
            if label:
                vals["label_attachment_id"] = self.env["ir.attachment"].sudo().create({
                    "name": "label-%s.pdf" % (awb or rec.name), "raw": label,
                    "mimetype": "application/pdf", "res_model": rec._name, "res_id": rec.id,
                }).id
            rec.sudo().write(vals)
            rec.message_post(body=_("Shipment created with %(provider)s (AWB %(awb)s).",
                                    provider=provider.name, awb=awb or "-"))
        return True

    def action_download_label(self):
        self.ensure_one()
        if not self.label_attachment_id:
            raise UserError(_("This shipment has no label."))
        return {"type": "ir.actions.act_url", "target": "self",
                "url": "/web/content/%s?download=true" % self.label_attachment_id.id}

    def _apply_shipping_status(self, provider, status):
        """Apply one carrier status. Returns (processed, note); runs as sudo."""
        self.ensure_one()
        rank = _STATUS_RANK
        if not self.shipping_status or rank.get(status, 0) >= rank.get(self.shipping_status, 0):
            self.shipping_status = status
        carrier = self.env.ref("base.user_root")
        label = _("Carrier %s", provider.name)
        if status == "picked_up":
            if self.fulfillment_type == "fbl":
                return False, "FBL parcels are shipped by the marketplace warehouse."
            if self.state not in self._SHIPPING_OPEN_STATES:
                return True, "Already shipped."
            self.action_ship(tracking_ref=self.awb)
            self.message_post(body=_("%s picked up the parcel: shipped.", label))
            return True, "Shipped."
        if status == "delivered":
            if self.state == "shipped" and self._leaders_fbm_delivery_mode() == "customer_confirm":
                self.action_delivered(confirmed_by=carrier)
            elif self.state == "shipped":
                self.action_report_delivery(reported_by=carrier)
            elif self.state == "delivery_reported":
                self.action_delivered(confirmed_by=carrier)
            else:
                return False, "Not shipped yet, delivery ignored."
            self.message_post(body=_("%s reported the parcel as delivered.", label))
            return True, "Delivered."
        if status in ("returned", "refused"):
            if self.state == "shipped" and self.vendor_order_id.payment_method == "cod":
                self.action_customer_refused(reason=_("%s: parcel %s.", provider.name, status), by=carrier)
                return True, "Refused flow applied."
            self.message_post(body=_("%(carrier)s reports the parcel as %(status)s.",
                                     carrier=label, status=status))
            return True, "Logged only."
        return True, "Status recorded."
