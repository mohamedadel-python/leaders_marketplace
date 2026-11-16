"""B4: vendor write API (keys, rate limit, idempotency).

Keys are NOT Odoo's native ``res.users.apikeys``: the ``auth="bearer"`` mechanism
of Odoo 18 only accepts *global* keys, i.e. a key that also opens the whole
JSON-2/XML-RPC surface of its user. A marketplace key must only reach the vendor
API, be revocable, have a read/write scope and keep a "last used" date, so it has
its own model. Only a SHA-256 of the key and its 8 character prefix are stored;
the plaintext is shown once at creation.
"""
import hashlib
import hmac
import re
import secrets
import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.http import request

PARAM_ENABLED = "leaders_marketplace.vendor_api_write_enabled"
PARAM_RATE = "leaders_marketplace.vendor_api_rate_limit_per_minute"
KEY_PREFIX = "lmk_"
KEY_RE = re.compile(r"^lmk_([0-9a-f]{8})_[A-Za-z0-9_-]{20,}$")
IDEMPOTENCY_HOURS = 24


class LeadersVendorApiKey(models.Model):
    _name = "leaders.vendor.api.key"
    _description = "Vendor API Key"
    _order = "id desc"

    name = fields.Char(required=True)
    vendor_id = fields.Many2one("res.partner", required=True, index=True, ondelete="cascade",
                                domain=[("is_marketplace_vendor", "=", True)])
    user_id = fields.Many2one("res.users", string="Created By", required=True, ondelete="cascade")
    scope = fields.Selection([("read", "Read only"), ("write", "Read and write")],
                             required=True, default="read")
    key_prefix = fields.Char(readonly=True, index=True)
    key_hash = fields.Char(readonly=True, copy=False)
    last_used = fields.Datetime(readonly=True)
    revoked_date = fields.Datetime(readonly=True)
    is_revoked = fields.Boolean(compute="_compute_is_revoked", search="_search_is_revoked")

    @api.depends("revoked_date")
    def _compute_is_revoked(self):
        for rec in self:
            rec.is_revoked = bool(rec.revoked_date)

    def _search_is_revoked(self, operator, value):
        positive = (operator == "=") == bool(value)
        return [("revoked_date", "!=" if positive else "=", False)]

    # ------------------------------------------------------------------ settings
    @api.model
    def _leaders_api_enabled(self):
        value = self.env["ir.config_parameter"].sudo().get_param(PARAM_ENABLED)
        return value in ("True", "true", "1")

    @api.model
    def _leaders_rate_limit(self):
        """Requests per minute per key; 0 (or a missing / invalid parameter) = no limit."""
        value = self.env["ir.config_parameter"].sudo().get_param(PARAM_RATE)
        try:
            return max(int(value or 0), 0)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------ keys
    @staticmethod
    def _hash_key(plaintext):
        return hashlib.sha256(plaintext.encode()).hexdigest()

    @api.model
    def _leaders_generate(self, vendor, user, name, scope):
        """Create a key. Returns (record, plaintext); the plaintext is not recoverable later."""
        name = (name or "").strip()
        if not name:
            raise UserError(_("Please give the key a name."))
        if len(name) > 100:
            raise UserError(_("The key name is too long."))
        if scope not in ("read", "write"):
            raise UserError(_("Unknown key scope."))
        prefix = secrets.token_hex(4)
        plaintext = "%s%s_%s" % (KEY_PREFIX, prefix, secrets.token_urlsafe(32))
        rec = self.sudo().create({
            "name": name, "vendor_id": vendor.id, "user_id": user.id, "scope": scope,
            "key_prefix": prefix, "key_hash": self._hash_key(plaintext),
        })
        return rec, plaintext

    @api.model
    def _leaders_authenticate(self, token):
        """The valid (not revoked) key record for ``token``, else an empty recordset."""
        match = KEY_RE.match(token or "")
        if not match:
            return self.browse()
        digest = self._hash_key(token)
        for rec in self.sudo().search([("key_prefix", "=", match.group(1)),
                                       ("revoked_date", "=", False)]):
            if hmac.compare_digest(rec.key_hash or "", digest):
                if not rec.user_id.active or not rec.vendor_id.is_marketplace_vendor:
                    return self.browse()
                return rec
        return self.browse()

    def _leaders_touch(self):
        """Record the use (at most once a minute per key, to avoid a write per call)."""
        self.ensure_one()
        now = fields.Datetime.now()
        if not self.last_used or now - self.last_used >= timedelta(seconds=60):
            self.env.cr.execute(
                "UPDATE leaders_vendor_api_key SET last_used = %s WHERE id = %s", [now, self.id])
            self.invalidate_recordset(["last_used"])

    def action_revoke(self):
        for rec in self.sudo().filtered(lambda k: not k.revoked_date):
            rec.revoked_date = fields.Datetime.now()
        return True

    # ------------------------------------------------------------------ rate limit
    @api.model
    def _leaders_now(self):
        """Seconds since the epoch (a hook the tests patch)."""
        return time.time()

    def _leaders_rate_hit(self):
        """Count one call. Returns (allowed, limit, remaining, retry_after_seconds)."""
        self.ensure_one()
        limit = self._leaders_rate_limit()
        if not limit:
            return True, 0, 0, 0
        now = self._leaders_now()
        window = int(now // 60)
        # One atomic upsert; concurrent calls of the same key serialise on the row.
        self.env.cr.execute("""
            INSERT INTO leaders_vendor_api_rate (key_id, window_start, count)
            VALUES (%s, %s, 1)
            ON CONFLICT (key_id, window_start) DO UPDATE SET count = leaders_vendor_api_rate.count + 1
            RETURNING count""", [self.id, window])
        count = self.env.cr.fetchone()[0]
        retry_after = max(int((window + 1) * 60 - now) + 1, 1)
        return count <= limit, limit, max(limit - count, 0), retry_after

    def _leaders_lock(self):
        """Serialise the write calls of one key (makes Idempotency-Key checks race free)."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM leaders_vendor_api_key WHERE id = %s FOR UPDATE", [self.id])

    @api.model
    def _cron_cleanup(self):
        """Drop replay records older than 24 hours and old rate windows."""
        cutoff = fields.Datetime.now() - timedelta(hours=IDEMPOTENCY_HOURS)
        self.env["leaders.vendor.api.idempotency"].sudo().search(
            [("create_date", "<", cutoff)]).unlink()
        window = int(self._leaders_now() // 60) - 120
        self.env.cr.execute("DELETE FROM leaders_vendor_api_rate WHERE window_start < %s", [window])
        self.env.cr.execute(
            "DELETE FROM leaders_shipping_webhook_rate WHERE window_start < %s", [window])
        return True


class LeadersVendorApiRate(models.Model):
    _name = "leaders.vendor.api.rate"
    _description = "Vendor API Rate Counter"

    key_id = fields.Many2one("leaders.vendor.api.key", required=True, ondelete="cascade", index=True)
    window_start = fields.Integer(required=True, help="Minute number since the epoch.")
    count = fields.Integer(default=0)

    _sql_constraints = [
        ("key_window_unique", "unique(key_id, window_start)", "One counter per key and minute."),
    ]


class LeadersVendorApiIdempotency(models.Model):
    _name = "leaders.vendor.api.idempotency"
    _description = "Vendor API Idempotency Record"

    key_id = fields.Many2one("leaders.vendor.api.key", required=True, ondelete="cascade", index=True)
    idempotency_key = fields.Char(required=True)
    request_hash = fields.Char(required=True, help="Hash of method, path and JSON body.")
    status = fields.Integer(required=True)
    body = fields.Text()

    _sql_constraints = [
        ("key_idem_unique", "unique(key_id, idempotency_key)", "Idempotency key already used."),
    ]


class MarketplaceOfferApiHelpers(models.Model):
    _inherit = "leaders.marketplace.offer"

    def _leaders_vendor_set_price(self, price):
        """A vendor changes the regular price (what the portal edit form does).

        The offer goes back to draft so it is reviewed again, exactly like the
        portal edit. An unchanged price is a no-op.
        """
        self.ensure_one()
        if price < 0:
            raise UserError(_("The price cannot be negative."))
        if self.currency_id.compare_amounts(price, self.price) == 0:
            return False
        self.sudo().write({"price": price, "state": "draft", "reject_reason": False})
        return True


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _auth_method_leaders_vendor_bearer(cls):
        """Route auth for URLs that accept a marketplace key OR a native Odoo key.

        A marketplace key (``lmk_...``) is validated by the controller itself (it
        must answer with the JSON error format); any other token goes through the
        stock ``bearer`` check, so the pre-existing API behaves as before.
        """
        header = request.httprequest.headers.get("Authorization") or ""
        if re.match(r"^bearer\s+lmk_", header, re.IGNORECASE):
            cls._auth_method_public()
            return
        cls._auth_method_bearer()
