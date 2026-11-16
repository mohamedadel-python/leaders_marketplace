"""Auth API business logic: register, login, refresh, logout, password reset.

Controllers stay thin: they parse the request, call one method here and render
the result. Every failure is an ``AuthApiError`` carrying the contract code.
"""
import base64
import logging
import re
import secrets

import psycopg2
import requests

from odoo import Command, fields
from odoo.tools.misc import hmac as odoo_hmac
from odoo.exceptions import AccessDenied, ValidationError

from ..models.auth_api import FIXTURE_EMAIL
from ..tools.auth_errors import AuthApiError, validation_error
from ..tools.auth_tokens import access_expiry_seconds, encode_access_token

_logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
GOOGLE_CLIENT_PARAM = "leaders_marketplace.google_client_id"
GOOGLE_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
CODE_RE = re.compile(r"^\d{6}$")
REGISTER_TYPE_PARAM = "leaders_marketplace.auth_register_user_type"  # "portal" (default) or "internal"

# (limit, window seconds) per bucket kind
LIMITS = {
    "register_ip": (10, 60), "login_ip": (30, 60), "login_email": (10, 60),
    "refresh_ip": (60, 60), "forgot_ip": (10, 60), "forgot_email": (3, 600),
    "verify_ip": (30, 60), "reset_ip": (10, 60),
    "signup_verify_ip": (30, 60), "signup_resend_email": (3, 600),
}
SIGNUP_OTP_PARAM = "leaders_marketplace.auth_register_otp"  # "1": a new account confirms its email with a code


def _str(body, key):
    value = body.get(key)
    return value if isinstance(value, str) else ""


def check_strong_password(password):
    """The account's password change (profile-pages contract): 8+ characters, upper and lower case, a digit
    and a special character."""
    if (len(password) < 8 or not re.search(r"[A-Z]", password) or not re.search(r"[a-z]", password)
            or not re.search(r"\d", password) or not re.search(r"[^A-Za-z0-9]", password)):
        return ("Password must be at least 8 characters with an upper-case letter, a lower-case letter, "
                "a digit and a special character.")
    return None


def check_password_rules(password):
    if len(password) < 8 or not re.search(r"[A-Z]", password) or not re.search(r"\d", password):
        return "Password must be at least 8 characters with a capital letter and a digit."
    return None


class AuthService:

    def __init__(self, env):
        self.env = env
        self.Token = env["leaders.auth.refresh.token"]
        self.Code = env["leaders.auth.reset.code"]

    # ---------------------------------------------------------- helpers
    def throttle(self, kind, identity):
        limit, window = LIMITS[kind]
        if not self.env["leaders.auth.rate"].sudo().hit("%s:%s" % (kind, identity), limit, window):
            raise AuthApiError(429, "rate_limited", "Too many requests. Wait a moment.")

    def _login_id(self, email):
        self.env.cr.execute("SELECT id FROM res_users WHERE LOWER(login) = %s ORDER BY id LIMIT 1", (email,))
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _find_user(self, email):
        """Case-insensitive lookup, archived users included."""
        return self.env["res.users"].sudo().with_context(active_test=False).browse(self._login_id(email) or [])

    def _phone_taken(self, phone):
        return bool(self.env["res.users"].sudo().with_context(active_test=False).search_count(
            [("partner_id.phone", "=", phone)]))

    @staticmethod
    def _is_generated_svg(image):
        head = base64.b64decode((image if isinstance(image, bytes) else image.encode())[:48])
        return head.lstrip().startswith((b"<?xml", b"<svg"))

    def _avatar(self, user):
        partner = user.partner_id
        image = partner.image_1920
        # Odoo gives every internal user a generated initials SVG; that is "no picture" for the client
        if not image or self._is_generated_svg(image):
            return None
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        # /web/image would show Odoo's placeholder to the storefront (a visitor may not read a customer): a signed
        # link of this partner's current picture instead. It changes with every change of the contact.
        return "%s/account/avatar/%s/%s" % (base, partner.id, self.avatar_signature(partner))

    def avatar_signature(self, partner):
        stamp = fields.Datetime.to_string(partner.sudo().write_date or fields.Datetime.now())
        return odoo_hmac(self.env(su=True), "leaders_marketplace.avatar", "%s:%s" % (partner.id, stamp))[:32]

    def session_payload(self, user):
        user = user.sudo()
        return {
            "accessToken": encode_access_token(self.env, user), "tokenType": "Bearer",
            "expiresIn": access_expiry_seconds(self.env),
            "user": {"id": str(user.id), "name": user.name, "email": user.email or user.login,
                     "avatar": self._avatar(user), "type": self.account_type(user)},
        }

    @staticmethod
    def account_type(user):
        """``vendor`` once the account's company is an approved (or frozen) marketplace vendor, else ``customer``;
        an application still under review keeps the account a customer."""
        company = user.sudo().partner_id.commercial_partner_id
        return "vendor" if company.is_marketplace_vendor and company.vendor_state in ("approved", "frozen") else "customer"

    # --------------------------------------------------------- register
    def _register_group(self):
        """Group given to a self-registered user: ``portal`` by default, ``internal`` by setting."""
        kind = self.env["ir.config_parameter"].sudo().get_param(REGISTER_TYPE_PARAM) or "portal"
        return self.env.ref("base.group_user" if kind == "internal" else "base.group_portal")

    def register(self, body, user_agent="", ip=""):
        self.throttle("register_ip", ip)
        name = _str(body, "name").strip()
        email = _str(body, "email").strip().lower()
        phone = re.sub(r"\s", "", _str(body, "phone"))
        password = _str(body, "password")
        errors = {}
        if not 2 <= len(name) <= 80:
            errors["name"] = "Name must be 2 to 80 characters."
        if not EMAIL_RE.match(email):
            errors["email"] = "Invalid email"
        if not PHONE_RE.match(phone):
            errors["phone"] = "Phone must be in E.164 format, e.g. +966501234567."
        rule = check_password_rules(password)
        if rule:
            errors["password"] = rule
        if errors:
            raise validation_error(errors)
        otp = self.signup_otp_enabled()
        existing = self._find_user(email)
        if existing and otp and existing.active and not existing.leaders_email_verified:
            # registered before but never confirmed: the new password counts and a fresh code is sent
            existing.sudo().write({"name": name, "password": password})
            return self._send_signup_code(existing)
        if existing:
            raise AuthApiError(409, "email_taken", "An account with this email already exists.")
        if self._phone_taken(phone):
            raise AuthApiError(409, "phone_taken", "An account with this phone number already exists.")
        try:
            with self.env.cr.savepoint():  # a concurrent sign-up with the same login hits the unique key
                user = self.env["res.users"].sudo().with_context(no_reset_password=True).create({
                    "name": name, "login": email, "email": email, "password": password, "phone": phone,
                    "groups_id": [Command.set([self._register_group().id])],
                    "leaders_email_verified": not otp,
                })
                self.mark_customer(user)
        except (psycopg2.IntegrityError, ValidationError):
            if self._login_id(email):
                raise AuthApiError(409, "email_taken", "An account with this email already exists.")
            if self._phone_taken(phone):
                raise AuthApiError(409, "phone_taken", "An account with this phone number already exists.")
            raise
        if otp:  # the account opens only once the code sent to the email is entered
            return self._send_signup_code(user)
        return self._open_session(user, False, user_agent, ip)

    # ------------------------------------------------- sign-up email code
    def _send_queue_now(self):
        """A code is useless an hour later: wake the email queue now instead of waiting for its next run."""
        cron = self.env.ref("mail.ir_cron_mail_scheduler_action", raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    def signup_otp_enabled(self):
        return (self.env["ir.config_parameter"].sudo().get_param(SIGNUP_OTP_PARAM) or "").lower() in ("1", "true")

    def _send_signup_code(self, user):
        _record, code = self.Code.issue(user, "signup")
        try:
            template = self.env.ref("leaders_marketplace.mail_template_auth_signup_code")
            template.sudo().with_context(signup_code=code, lang=user.lang).send_mail(
                user.id, email_values={"email_to": user.email or user.login, "recipient_ids": []})
            self._send_queue_now()
        except Exception:  # never reveal delivery problems to the caller
            _logger.exception("Could not queue the sign-up code email for user %s", user.id)
        from ..models.auth_api import CODE_MINUTES
        return {"verification": "email", "email": user.email or user.login, "expiresIn": CODE_MINUTES * 60}

    def verify_signup(self, body, user_agent="", ip=""):
        """The code of the sign-up email: confirms the email and opens the session."""
        self.throttle("signup_verify_ip", ip)
        email = _str(body, "email").strip().lower()
        code = _str(body, "code").strip()
        errors = {}
        if not EMAIL_RE.match(email):
            errors["email"] = "Invalid email"
        if not CODE_RE.match(code):
            errors["code"] = "The code must be exactly 6 digits."
        if errors:
            raise validation_error(errors)
        user = self._find_user(email)
        record = self.Code.latest_pending(user, "signup") if user and user.active else self.Code
        if not record:
            raise AuthApiError(400, "invalid_code", "The code is incorrect.")
        if record.attempts >= 5:
            raise AuthApiError(429, "too_many_attempts", "Too many attempts. Request a new code.")
        if record.expires_at <= fields.Datetime.now():
            raise AuthApiError(400, "code_expired", "The code has expired. Request a new one.")
        if not record.matches(code):
            record.attempts += 1
            raise AuthApiError(400, "invalid_code", "The code is incorrect.")
        record.state = "used"
        user.sudo().leaders_email_verified = True
        return self._open_session(user, body.get("remember") is True, user_agent, ip)

    def resend_signup(self, body, ip=""):
        """Send the sign-up code again; answers the same whether or not the account exists."""
        self.throttle("register_ip", ip)
        email = _str(body, "email").strip().lower()
        if not EMAIL_RE.match(email):
            raise validation_error({"email": "Invalid email"})
        self.throttle("signup_resend_email", email)
        user = self._find_user(email)
        if user and user.active and not user.leaders_email_verified:
            self._send_signup_code(user)

    def logout_all(self, user):
        """Sign out of every device: all the refresh tokens of the user are revoked."""
        self.Token.revoke_user(user)

    # ------------------------------------------------------------ login
    def login(self, body, user_agent="", ip=""):
        email = _str(body, "email").strip().lower()
        password = _str(body, "password")
        remember = body.get("remember") is True
        self.throttle("login_ip", ip)
        errors = {}
        if not EMAIL_RE.match(email):
            errors["email"] = "Invalid email"
        if not password:
            errors["password"] = "Password is required."
        if errors:
            raise validation_error(errors)
        self.throttle("login_email", email)
        bad = AuthApiError(401, "invalid_credentials", "Email or password is wrong.")
        user = self._find_user(email)
        if not user:
            raise bad
        users = self.env["res.users"].sudo()
        # _check_credentials and _update_last_login act on self.env.user, so run them as the candidate
        candidate = user.with_user(user)
        try:
            with users._assert_can_auth(user=email):
                candidate._check_credentials({"type": "password", "password": password}, {"interactive": False})
        except AccessDenied:
            raise bad
        if not user.active:
            raise AuthApiError(403, "account_disabled", "This account has been disabled.")
        if not user.leaders_email_verified:  # signed up but never entered the email code: send a fresh one
            self._send_signup_code(user)
            raise AuthApiError(403, "email_not_verified", "Confirm your email first: we sent you a new code.")
        candidate._update_last_login()
        return self._open_session(user, remember, user_agent, ip)

    # ----------------------------------------------------------- google
    def google(self, body, user_agent="", ip=""):
        """Sign in with a Google ID token; the first sign-in creates the account like a registration."""
        self.throttle("login_ip", ip)
        client_id = (self.env["ir.config_parameter"].sudo().get_param(GOOGLE_CLIENT_PARAM) or "").strip()
        if not client_id:
            raise AuthApiError(503, "google_disabled", "Google sign-in is not available.")
        token = _str(body, "idToken").strip()
        if not token:
            raise validation_error({"idToken": "Required."})
        bad = AuthApiError(401, "invalid_google_token", "Google sign-in failed. Try again.")
        try:
            answer = requests.get(GOOGLE_TOKENINFO, params={"id_token": token}, timeout=10)
        except requests.RequestException:
            _logger.exception("Google token check failed")
            raise AuthApiError(502, "google_unavailable", "Google sign-in is not reachable. Try again.")
        claims = answer.json() if answer.status_code == 200 else {}
        email = (claims.get("email") or "").strip().lower()
        if (claims.get("aud") != client_id or claims.get("iss") not in GOOGLE_ISSUERS
                or str(claims.get("email_verified")).lower() != "true" or not EMAIL_RE.match(email)):
            raise bad
        user = self._find_user(email)
        if user and not user.active:
            raise AuthApiError(403, "account_disabled", "This account has been disabled.")
        if not user:
            # the mobile number is required with Google too: it is needed for deliveries and site visits
            phone = re.sub(r"\s", "", _str(body, "phone"))
            if not PHONE_RE.match(phone):
                raise AuthApiError(422, "phone_required", "Enter your mobile number to finish signing up.",
                                   fields={"phone": "Phone must be in E.164 format, e.g. +966501234567."})
            if self._phone_taken(phone):
                raise AuthApiError(409, "phone_taken", "An account with this phone number already exists.")
            name = (claims.get("name") or email.split("@")[0]).strip()[:80]
            user = self.env["res.users"].sudo().with_context(no_reset_password=True).create({
                "name": name if len(name) >= 2 else email, "login": email, "email": email, "phone": phone,
                "groups_id": [Command.set([self._register_group().id])],
            })
            self.mark_customer(user)
        elif not user.leaders_email_verified:
            user.sudo().leaders_email_verified = True  # Google has verified the email
        return self._open_session(user, body.get("remember") is True, user_agent, ip)

    @staticmethod
    def mark_customer(user):
        """A storefront account is a customer from sign-up: it shows under Sales > Customers, not only in Contacts
        (Odoo counts a customer only after a first order or invoice)."""
        partner = user.sudo().partner_id
        if "customer_rank" in partner._fields and not partner.customer_rank:
            partner.customer_rank = 1

    def _open_session(self, user, remember, user_agent, ip):
        """Return ``(SessionResponse, raw refresh token, remember)``."""
        self.env["leaders.customer.request"].attach_guest_requests(user)
        raw = self.Token.issue(user, remember, user_agent=user_agent, ip_address=ip)
        return self.session_payload(user), raw, remember

    # ---------------------------------------------------------- refresh
    def refresh(self, raw, user_agent="", ip=""):
        self.throttle("refresh_ip", ip)
        rotated = self.Token.rotate(raw, user_agent, ip) if raw else None
        if not rotated:
            raise AuthApiError(401, "session_expired", "Your session has expired. Sign in again.")
        new_raw, user, remember = rotated
        return self.session_payload(user), new_raw, remember

    def logout(self, raw, user_id):
        """Revoke the refresh token, but only when it belongs to the user holding the Bearer token."""
        if raw:
            self.Token.revoke_raw(raw, user_id)

    # --------------------------------------------------- password reset
    def forgot(self, body, ip=""):
        self.throttle("forgot_ip", ip)
        email = _str(body, "email").strip().lower()
        if not EMAIL_RE.match(email):
            raise validation_error({"email": "Invalid email"})
        fixture = self.Code.fixtures_enabled() and email == FIXTURE_EMAIL
        if not fixture:  # the staging demo user is exempt so scripted tests can repeat the flow
            self.throttle("forgot_email", email)  # counted whether or not the account exists
        user = self._find_user(email)
        if fixture and not user:
            user = self._create_fixture_user()
        if not user or not user.active:
            return
        _record, code = self.Code.issue(user)
        try:
            template = self.env.ref("leaders_marketplace.mail_template_auth_reset_code")
            template.sudo().with_context(reset_code=code, lang=user.lang).send_mail(
                user.id, email_values={"email_to": user.email or user.login, "recipient_ids": []})
            self._send_queue_now()
        except Exception:  # never reveal delivery problems to the caller
            _logger.exception("Could not queue the password reset email for user %s", user.id)

    def _create_fixture_user(self):
        return self.env["res.users"].sudo().with_context(no_reset_password=True).create({
            "name": "Demo User", "login": FIXTURE_EMAIL, "email": FIXTURE_EMAIL,
            "password": secrets.token_urlsafe(16) + "A1", "groups_id": [Command.set([self._register_group().id])]})

    def verify(self, body, ip=""):
        self.throttle("verify_ip", ip)
        email = _str(body, "email").strip().lower()
        code = _str(body, "code").strip()
        errors = {}
        if not EMAIL_RE.match(email):
            errors["email"] = "Invalid email"
        if not CODE_RE.match(code):
            errors["code"] = "The code must be exactly 6 digits."
        if errors:
            raise validation_error(errors)
        user = self._find_user(email)
        record = self.Code.latest_pending(user) if user and user.active else self.Code
        if not record:
            raise AuthApiError(400, "invalid_code", "The code is incorrect.")
        if record.attempts >= 5:
            raise AuthApiError(429, "too_many_attempts", "Too many attempts. Request a new code.")
        if record.expires_at <= fields.Datetime.now():
            raise AuthApiError(400, "code_expired", "The code has expired. Request a new one.")
        if not record.matches(code):
            record.attempts += 1
            raise AuthApiError(400, "invalid_code", "The code is incorrect.")
        token, expires_in = record.mark_verified()
        return {"resetToken": token, "expiresIn": expires_in}

    def reset(self, body, ip=""):
        self.throttle("reset_ip", ip)
        token = _str(body, "resetToken")
        password = _str(body, "password")
        errors = {}
        if not token:
            errors["resetToken"] = "Reset token is required."
        rule = check_password_rules(password)
        if rule:
            errors["password"] = rule
        if errors:
            raise validation_error(errors)
        record = self.Code.by_reset_token(token)
        if not record or record.state != "verified":
            raise AuthApiError(400, "invalid_reset_token", "This reset link is no longer valid.")
        if record.reset_expires_at <= fields.Datetime.now():
            raise AuthApiError(400, "reset_token_expired", "This reset link is no longer valid.")
        user = record.user_id.sudo()
        user.password = password
        record.state = "used"
        self.Token.revoke_user(user)
