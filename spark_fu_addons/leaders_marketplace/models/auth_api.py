"""Auth API storage: rotating refresh tokens, password-reset codes, rate counters.

Only hashes are stored; the raw refresh token, reset code and reset token leave
the server once and are never readable again.
"""
import hmac
import secrets
import time
import uuid
from datetime import timedelta

from odoo import api, fields, models

from ..tools.auth_tokens import hash_token

REFRESH_DAYS = 7
REFRESH_DAYS_REMEMBER = 30
CODE_DIGITS = 6
CODE_MINUTES = 10
CODE_MAX_ATTEMPTS = 5
RESET_TOKEN_SECONDS = 900
FIXTURES_PARAM = "leaders_marketplace.auth_fixtures"  # staging only: fixed code and reset token for the demo user
FIXTURE_EMAIL = "demo@spark-fu.example"
FIXTURE_CODE = "123456"
FIXTURE_RESET_TOKEN = "mock-reset-token"
MASTER_CODE_PARAM = "leaders_marketplace.auth_master_code"  # testing: a 6-digit code every account accepts; empty = off


class LeadersAuthRefreshToken(models.Model):
    _name = "leaders.auth.refresh.token"
    _description = "Auth API Refresh Token"

    token_hash = fields.Char(required=True, index=True, readonly=True)
    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    family = fields.Char(required=True, index=True, readonly=True,
                         help="All tokens rotated from one login share a family.")
    remember = fields.Boolean(readonly=True)
    expires_at = fields.Datetime(required=True)
    revoked = fields.Boolean(default=False, index=True)
    user_agent = fields.Char()
    ip_address = fields.Char()

    @api.model
    def lifetime_seconds(self, remember):
        return (REFRESH_DAYS_REMEMBER if remember else REFRESH_DAYS) * 86400

    @api.model
    def issue(self, user, remember=False, family=None, user_agent="", ip_address=""):
        raw = secrets.token_urlsafe(48)
        self.sudo().create({
            "token_hash": hash_token(raw), "user_id": user.id,
            "family": family or uuid.uuid4().hex, "remember": remember,
            "expires_at": fields.Datetime.now() + timedelta(seconds=self.lifetime_seconds(remember)),
            "user_agent": (user_agent or "")[:500], "ip_address": ip_address or "",
        })
        return raw

    @api.model
    def rotate(self, raw, user_agent="", ip_address=""):
        """Return ``(new_raw, user, remember)`` or None when the token is not usable.

        Presenting an already-rotated token revokes its whole family.
        """
        record = self.sudo().search([("token_hash", "=", hash_token(raw))], limit=1)
        if not record:
            return None
        if record.revoked:
            self.sudo().search([("family", "=", record.family)]).write({"revoked": True})
            return None
        user = record.user_id
        if record.expires_at <= fields.Datetime.now() or not user.active:
            record.revoked = True
            return None
        record.revoked = True
        new_raw = self.issue(user, record.remember, record.family, user_agent, ip_address)
        return new_raw, user, record.remember

    @api.model
    def revoke_raw(self, raw, user_id=None):
        domain = [("token_hash", "=", hash_token(raw))]
        if user_id:
            domain.append(("user_id", "=", user_id))
        self.sudo().search(domain, limit=1).write({"revoked": True})

    @api.model
    def revoke_user(self, user):
        self.sudo().search([("user_id", "=", user.id), ("revoked", "=", False)]).write({"revoked": True})

    @api.model
    def cron_purge(self):
        cutoff = fields.Datetime.now() - timedelta(days=1)
        self.sudo().search(["|", ("expires_at", "<", cutoff), "&", ("revoked", "=", True),
                            ("write_date", "<", cutoff)]).unlink()


class LeadersAuthResetCode(models.Model):
    _name = "leaders.auth.reset.code"
    _description = "Auth API Password Reset Code"

    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    salt = fields.Char(required=True, readonly=True)
    code_hash = fields.Char(required=True, readonly=True)
    expires_at = fields.Datetime(required=True)
    attempts = fields.Integer(default=0)
    state = fields.Selection([("pending", "Pending"), ("verified", "Verified"), ("used", "Used"),
                              ("superseded", "Superseded")], default="pending", required=True, index=True)
    purpose = fields.Selection([("reset", "Password reset"), ("signup", "Email verification at sign-up")],
                               default="reset", required=True, index=True)
    reset_token_hash = fields.Char(index=True, readonly=True)
    reset_expires_at = fields.Datetime()

    @staticmethod
    def _hash_code(salt, user_id, code):
        return hash_token("%s:%s:%s" % (salt, user_id, code))

    @api.model
    def fixtures_enabled(self):
        return (self.env["ir.config_parameter"].sudo().get_param(FIXTURES_PARAM) or "").lower() in ("1", "true")

    @api.model
    def _fixture_user(self, user):
        return self.fixtures_enabled() and (user.login or "").lower() == FIXTURE_EMAIL

    @api.model
    def issue(self, user, purpose="reset"):
        """Supersede any open code of the user (same purpose) and return ``(record, raw_code)``."""
        self.sudo().search([("user_id", "=", user.id), ("state", "in", ("pending", "verified")),
                            ("purpose", "=", purpose)]).write({"state": "superseded"})
        code = "".join(secrets.choice("0123456789") for _i in range(CODE_DIGITS))
        if self._fixture_user(user):
            code = FIXTURE_CODE
        salt = secrets.token_hex(8)
        record = self.sudo().create({
            "user_id": user.id, "salt": salt, "code_hash": self._hash_code(salt, user.id, code), "purpose": purpose,
            "expires_at": fields.Datetime.now() + timedelta(minutes=CODE_MINUTES)})
        return record, code

    @api.model
    def latest_pending(self, user, purpose="reset"):
        return self.sudo().search([("user_id", "=", user.id), ("state", "=", "pending"), ("purpose", "=", purpose)],
                                  order="id desc", limit=1)

    def matches(self, code):
        self.ensure_one()
        if not code:
            return False
        master = (self.env["ir.config_parameter"].sudo().get_param(MASTER_CODE_PARAM) or "").strip()
        if len(master) == CODE_DIGITS and master.isdigit() and hmac.compare_digest(code, master):
            return True  # the shared testing code, accepted next to the code that was sent
        return hmac.compare_digest(self._hash_code(self.salt, self.user_id.id, code), self.code_hash)

    def mark_verified(self):
        self.ensure_one()
        raw = FIXTURE_RESET_TOKEN if self._fixture_user(self.user_id) else secrets.token_urlsafe(32)
        self.write({"state": "verified", "reset_token_hash": hash_token(raw),
                    "reset_expires_at": fields.Datetime.now() + timedelta(seconds=RESET_TOKEN_SECONDS)})
        return raw, RESET_TOKEN_SECONDS

    @api.model
    def by_reset_token(self, raw):
        return self.sudo().search([("reset_token_hash", "=", hash_token(raw))], order="id desc", limit=1)

    @api.model
    def cron_purge(self):
        cutoff = fields.Datetime.now() - timedelta(days=1)
        self.sudo().search([("create_date", "<", cutoff)]).unlink()


class LeadersAuthRate(models.Model):
    _name = "leaders.auth.rate"
    _description = "Auth API Rate Counter"

    bucket = fields.Char(required=True, index=True)
    window_start = fields.Integer(required=True, help="Window number since the epoch.")
    count = fields.Integer(default=0)

    _sql_constraints = [("bucket_window_unique", "unique(bucket, window_start)", "One counter per bucket and window.")]

    @api.model
    def hit(self, bucket, limit, window_seconds):
        """Count one request; False once ``limit`` is exceeded in the current window."""
        window = int(time.time()) // window_seconds
        query = """INSERT INTO leaders_auth_rate (bucket, window_start, count, create_date, write_date)
                   VALUES (%s, %s, 1, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
                   ON CONFLICT (bucket, window_start)
                   DO UPDATE SET count = leaders_auth_rate.count + 1 RETURNING count"""
        if self.pool.in_test_mode():  # tests share one cursor
            self.env.cr.execute(query, (bucket, window))
            return self.env.cr.fetchone()[0] <= limit
        # Own short transaction: the counter row is shared by every request from one IP, and inside
        # the request's transaction (REPEATABLE READ) concurrent requests fail with a serialization error.
        with self.pool.cursor() as cr:
            cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            cr.execute(query, (bucket, window))
            return cr.fetchone()[0] <= limit

    @api.model
    def set_default_cors_origins(self):
        """Allow the storefront origins unless the admin already configured the parameter."""
        params = self.env["ir.config_parameter"].sudo()
        if not params.get_param("leaders_marketplace.auth_cors_origin"):
            params.set_param("leaders_marketplace.auth_cors_origin", "http://localhost:3000,https://spark-fu.com")

    @api.model
    def cron_purge(self):
        self.env.cr.execute("DELETE FROM leaders_auth_rate WHERE write_date < NOW() AT TIME ZONE 'UTC' - INTERVAL '1 day'")


class ResUsersEmailVerified(models.Model):
    _inherit = "res.users"

    leaders_email_verified = fields.Boolean(
        "Email verified", default=True, copy=False,
        help="Off for a storefront account registered with email and password until the customer enters the code "
             "sent to that email (sign-up OTP). Google accounts are verified by Google.")
    leaders_google_sub = fields.Char(
        "Google ID", copy=False, index=True, readonly=True,
        help="The stable id (sub) of the Google account linked to this account: the next Google sign-in finds the "
             "account by it, whatever the email of the Google account becomes.")

    _sql_constraints = [("leaders_google_sub_unique", "unique(leaders_google_sub)",
                         "This Google account is already linked to another account.")]

