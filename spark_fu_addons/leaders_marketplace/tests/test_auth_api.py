"""Auth API contract: register, login, refresh rotation, logout, password reset."""
import json
import re
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests import tagged
from odoo.tests.common import HttpCase

PASSWORD = "Strong-pass1"
OK_USER = {"name": "Sara Ali", "email": "Sara.Ali@Example.com", "phone": "+966501234567", "password": PASSWORD}
EMAIL = "sara.ali@example.com"


class AuthApiCommon(HttpCase):

    def setUp(self):
        super().setUp()
        param = self.env["ir.config_parameter"].sudo()
        param.set_param("leaders_marketplace.auth_cookie_secure", "False")
        param.set_param("base.login_cooldown_after", "0")  # Odoo's own cooldown is process-wide; one test turns it on
        param.set_param("leaders_marketplace.auth_register_otp", "0")  # TestAuthSignupOtp covers the email code
        param.set_param("leaders_marketplace.auth_master_code", "")  # TestAuthMasterCode covers the shared code
        if hasattr(self.registry, "_login_failures"):
            del self.registry._login_failures
        self.param = param

    def post(self, path, body=None, headers=None, cookies=None):
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        self.opener.cookies.clear()  # send only the cookies the test passes
        return self.opener.post(self.base_url() + path, data=json.dumps(body or {}), headers=hdrs,
                                cookies=cookies, timeout=30)

    def register(self, **over):
        return self.post("/auth/register", {**OK_USER, **over})

    def assertCode(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["code"], code)
        self.assertIn("message", response.json())

    @staticmethod
    def set_cookies(response):
        return " | ".join(response.raw.headers.getlist("Set-Cookie"))

    @staticmethod
    def refresh_cookie(response):
        return response.cookies.get("refresh_token")

    def make_user(self, login, password=PASSWORD, name="Test User", active=True):
        user = self.env["res.users"].sudo().with_context(no_reset_password=True).create({
            "name": name, "login": login, "email": login.lower(), "password": password,
            "groups_id": [Command.set([self.env.ref("base.group_user").id])]})
        if not active:
            user.active = False
        self.env.flush_all()
        return user

    def emailed_code(self, email):
        mail = self.env["mail.mail"].sudo().search([("email_to", "=", email)], order="id desc", limit=1)
        match = re.search(r"<strong[^>]*>(\d{6})</strong>", mail.body_html or "")
        return match.group(1) if match else None

    def user(self):
        return self.env["res.users"].sudo().with_context(active_test=False).search([("login", "=", EMAIL)])


@tagged("post_install", "-at_install")
class TestAuthApi(AuthApiCommon):

    def test_register_returns_session_and_cookies(self):
        r = self.register()
        self.assertEqual(r.status_code, 201, r.text)
        data = r.json()
        self.assertEqual(set(data), {"accessToken", "tokenType", "expiresIn", "user"})
        self.assertEqual(data["tokenType"], "Bearer")
        self.assertGreaterEqual(data["expiresIn"], 120)
        self.assertEqual(data["user"]["email"], EMAIL)
        self.assertIsNone(data["user"]["avatar"])
        self.assertIsInstance(data["user"]["id"], str)
        cookies = self.set_cookies(r)
        self.assertIn("refresh_token=", cookies)
        self.assertIn("HttpOnly", cookies)
        self.assertIn("Path=/auth", cookies)
        self.assertIn("spark_session=1", cookies)
        user = self.user()
        self.assertTrue(user.has_group("base.group_portal"))
        self.assertTrue(user.share)
        self.assertFalse(user.has_group("base.group_user"))
        self.assertEqual(user.partner_id.phone, "+966501234567")

    def test_a_storefront_account_is_a_customer_from_sign_up(self):
        self.assertEqual(self.register().status_code, 201)
        partner = self.user().partner_id
        self.assertGreaterEqual(partner.customer_rank, 1)
        self.assertIn(partner, self.env["res.partner"].search([("customer_rank", ">", 0)]))  # Sales > Customers

    def test_register_user_type_can_be_switched_to_internal(self):
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.auth_register_user_type", "internal")
        self.assertEqual(self.register().status_code, 201)
        user = self.user()
        self.assertTrue(user.has_group("base.group_user"))
        self.assertFalse(user.has_group("base.group_portal"))

    def test_register_validation_and_conflicts(self):
        r = self.post("/auth/register", {"name": "S", "email": "nope", "phone": "123", "password": "weak"})
        self.assertCode(r, 422, "validation_error")
        self.assertEqual(set(r.json()["fields"]), {"name", "email", "phone", "password"})
        self.assertEqual(self.register().status_code, 201)
        self.assertCode(self.register(phone="+966500000001"), 409, "email_taken")
        self.assertCode(self.register(email="other@example.com"), 409, "phone_taken")

    def test_login_flow_and_errors(self):
        self.register()
        r = self.post("/auth/login", {"email": "SARA.ALI@example.com", "password": PASSWORD, "remember": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Max-Age=%s" % (30 * 86400), self.set_cookies(r))
        self.assertCode(self.post("/auth/login", {"email": EMAIL, "password": "Wrong-pass1"}),
                        401, "invalid_credentials")
        self.assertCode(self.post("/auth/login", {"email": "ghost@example.com", "password": PASSWORD}),
                        401, "invalid_credentials")
        self.assertCode(self.post("/auth/login", {"email": "bad", "password": ""}), 422, "validation_error")
        self.user().active = False
        self.env.flush_all()
        self.assertCode(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}),
                        403, "account_disabled")

    def test_refresh_rotates_and_reuse_revokes_the_family(self):
        first = self.register().cookies["refresh_token"]
        r2 = self.post("/auth/refresh", cookies={"refresh_token": first})
        self.assertEqual(r2.status_code, 200, r2.text)
        second = r2.cookies["refresh_token"]
        self.assertNotEqual(first, second)
        # the old token is dead, and presenting it kills the whole chain
        self.assertCode(self.post("/auth/refresh", cookies={"refresh_token": first}), 401, "session_expired")
        self.assertCode(self.post("/auth/refresh", cookies={"refresh_token": second}), 401, "session_expired")

    def test_refresh_without_cookie_is_401_and_clears_cookies(self):
        r = self.post("/auth/refresh")
        self.assertCode(r, 401, "session_expired")
        cleared = self.set_cookies(r)
        self.assertIn("refresh_token=;", cleared)
        self.assertIn("spark_session=;", cleared)

    def test_logout(self):
        r = self.register()
        token = r.json()["accessToken"]
        refresh = r.cookies["refresh_token"]
        self.assertCode(self.post("/auth/logout"), 401, "session_expired")
        out = self.post("/auth/logout", headers={"Authorization": "Bearer " + token},
                        cookies={"refresh_token": refresh})
        self.assertEqual(out.status_code, 204)
        self.assertCode(self.post("/auth/refresh", cookies={"refresh_token": refresh}), 401, "session_expired")
        again = self.post("/auth/logout", headers={"Authorization": "Bearer " + token})
        self.assertEqual(again.status_code, 204)

    def test_password_reset_end_to_end(self):
        refresh = self.register().cookies["refresh_token"]
        Code = self.env["leaders.auth.reset.code"].sudo()
        self.assertEqual(self.post("/auth/password/forgot", {"email": "ghost@example.com"}).status_code, 202)
        self.assertEqual(self.post("/auth/password/forgot", {"email": EMAIL}).status_code, 202)
        user = self.user()
        record = Code.search([("user_id", "=", user.id), ("state", "=", "pending")])
        self.assertEqual(len(record), 1)
        self.assertEqual(Code.search_count([]), 1)  # nothing was issued for the unknown address
        self.assertCode(self.post("/auth/password/verify", {"email": EMAIL, "code": "000000"}),
                        400, "invalid_code")
        # the stored hash is salted, so plant a code we know
        record.write({"code_hash": Code._hash_code(record.salt, user.id, "123456")})
        self.env.flush_all()
        v = self.post("/auth/password/verify", {"email": EMAIL, "code": "123456"})
        self.assertEqual(v.status_code, 200, v.text)
        self.assertEqual(set(v.json()), {"resetToken", "expiresIn"})
        token = v.json()["resetToken"]
        self.assertCode(self.post("/auth/password/reset", {"resetToken": token, "password": "weak"}),
                        422, "validation_error")
        done = self.post("/auth/password/reset", {"resetToken": token, "password": "Brand-new9"})
        self.assertEqual(done.status_code, 204, done.text)
        self.assertCode(self.post("/auth/password/reset", {"resetToken": token, "password": "Brand-new9"}),
                        400, "invalid_reset_token")
        self.assertCode(self.post("/auth/refresh", cookies={"refresh_token": refresh}), 401, "session_expired")
        self.assertCode(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}),
                        401, "invalid_credentials")
        self.assertEqual(self.post("/auth/login", {"email": EMAIL, "password": "Brand-new9"}).status_code, 200)

    def test_staging_fixtures_give_the_demo_user_a_fixed_code_and_reset_token(self):
        self.assertCode(self.post("/auth/password/verify", {"email": "demo@spark-fu.example", "code": "123456"}),
                        400, "invalid_code")  # off by default
        self.param.set_param("leaders_marketplace.auth_fixtures", "True")
        for _round in range(4):  # the demo user is exempt from the 3-per-10-minutes forgot limit
            self.assertEqual(self.post("/auth/password/forgot", {"email": "demo@spark-fu.example"}).status_code, 202)
        verified = self.post("/auth/password/verify", {"email": "demo@spark-fu.example", "code": "123456"})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(verified.json()["resetToken"], "mock-reset-token")
        done = self.post("/auth/password/reset", {"resetToken": "mock-reset-token", "password": "Brand-new9"})
        self.assertEqual(done.status_code, 204, done.text)
        self.assertCode(self.post("/auth/password/reset", {"resetToken": "mock-reset-token", "password": "Brand-new9"}),
                        400, "invalid_reset_token")

    def test_verify_locks_after_five_wrong_attempts_then_expiry(self):
        self.register()
        self.post("/auth/password/forgot", {"email": EMAIL})
        body = {"email": EMAIL, "code": "000000"}
        statuses = [self.post("/auth/password/verify", body).status_code for _i in range(6)]
        self.assertEqual(statuses, [400] * 5 + [429])
        self.assertCode(self.post("/auth/password/verify", body), 429, "too_many_attempts")
        self.env["leaders.auth.reset.code"].sudo().search([("user_id", "=", self.user().id)]).write(
            {"attempts": 0, "expires_at": fields.Datetime.subtract(fields.Datetime.now(), minutes=1)})
        self.env.flush_all()
        self.assertCode(self.post("/auth/password/verify", body), 400, "code_expired")

    def test_unknown_reset_token_and_invalid_json(self):
        self.assertCode(self.post("/auth/password/reset", {"resetToken": "nope", "password": PASSWORD}),
                        400, "invalid_reset_token")
        r = self.opener.post(self.base_url() + "/auth/login", data="{not json",
                             headers={"Content-Type": "application/json"}, timeout=30)
        self.assertCode(r, 400, "invalid_request")

    def test_login_is_rate_limited(self):
        # fill the per-IP bucket directly: 32 slow password checks can straddle the one-minute window
        rate = self.env["leaders.auth.rate"].sudo()
        for _i in range(30):
            rate.hit("login_ip:127.0.0.1", 30, 60)
        self.env.flush_all()
        r = self.post("/auth/login", {"email": "x@example.com", "password": "p"})
        self.assertEqual(r.status_code, 429)

    def test_cors_headers_only_for_the_configured_origin(self):
        origin = "https://app.example.com"
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.auth_cors_origin", origin)
        body = {"email": "x@example.com", "password": "p"}
        r = self.post("/auth/login", body, headers={"Origin": origin})
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], origin)
        self.assertEqual(r.headers["Access-Control-Allow-Credentials"], "true")
        other = self.post("/auth/login", body, headers={"Origin": "https://evil.test"})
        self.assertNotIn("Access-Control-Allow-Origin", other.headers)
        pre = self.opener.options(self.base_url() + "/auth/login", headers={"Origin": origin}, timeout=30)
        self.assertEqual(pre.status_code, 204)
        self.assertIn("POST", pre.headers["Access-Control-Allow-Methods"])


@tagged("post_install", "-at_install")
class TestAuthApiContractShape(AuthApiCommon):
    """Response schema, headers, bearer edge cases, methods, idempotency."""

    ROUTES = ["/auth/register", "/auth/login", "/auth/refresh", "/auth/logout",
              "/auth/password/forgot", "/auth/password/verify", "/auth/password/reset"]

    def test_every_response_is_json_with_no_store(self):
        for path in self.ROUTES:
            r = self.post(path, {})
            self.assertTrue(r.headers["Content-Type"].startswith("application/json"), path)
            self.assertEqual(r.headers["Cache-Control"], "no-store", path)

    def test_session_response_schema_and_default_cookie_attributes(self):
        r = self.register()
        data = r.json()
        self.assertIsInstance(data["accessToken"], str)
        self.assertEqual(data["tokenType"], "Bearer")
        self.assertIs(type(data["expiresIn"]), int)
        self.assertEqual(set(data["user"]), {"id", "name", "email", "avatar", "type"})
        self.assertTrue(all(isinstance(data["user"][k], str) for k in ("id", "name", "email")))
        cookies = r.raw.headers.getlist("Set-Cookie")
        refresh = next(c for c in cookies if c.startswith("refresh_token="))
        marker = next(c for c in cookies if c.startswith("spark_session="))
        self.assertIn("Max-Age=%s" % (7 * 86400), refresh)
        self.assertIn("SameSite=None", refresh)  # the API and the site are different sites
        self.assertIn("HttpOnly", refresh)
        self.assertNotIn("HttpOnly", marker)
        self.assertIn("Path=/;", marker + ";")
        self.assertIn("SameSite=None", marker)

    def test_access_token_is_a_verifiable_jwt_and_expiry_floor(self):
        from ..tools.auth_tokens import decode_access_token
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.auth_access_token_seconds", "10")
        data = self.register().json()
        self.assertGreaterEqual(data["expiresIn"], 120)
        self.assertEqual(decode_access_token(self.env, data["accessToken"]), int(data["user"]["id"]))

    def test_bearer_edge_cases_on_logout(self):
        import time
        import jwt
        from ..tools.auth_tokens import ALGORITHM, _secret
        secret = _secret(self.env)
        now = int(time.time())
        expired = jwt.encode({"sub": "2", "typ": "access", "iat": now - 2000, "exp": now - 1000}, secret, ALGORITHM)
        wrong_type = jwt.encode({"sub": "2", "typ": "refresh", "iat": now, "exp": now + 900}, secret, ALGORITHM)
        forged = jwt.encode({"sub": "2", "typ": "access", "iat": now, "exp": now + 900}, "other-secret", ALGORITHM)
        for header in ("Bearer " + expired, "Bearer " + wrong_type, "Bearer " + forged, "Bearer garbage",
                       "Basic abc", ""):
            r = self.post("/auth/logout", headers={"Authorization": header} if header else None)
            self.assertCode(r, 401, "session_expired")

    def test_wrong_http_method_is_not_served(self):
        for path in self.ROUTES:
            r = self.opener.get(self.base_url() + path, timeout=30)
            self.assertIn(r.status_code, (404, 405), path)

    def test_body_must_be_a_json_object(self):
        for raw in ("[1, 2]", "\"text\"", "123"):
            r = self.opener.post(self.base_url() + "/auth/login", data=raw,
                                 headers={"Content-Type": "application/json"}, timeout=30)
            self.assertCode(r, 400, "invalid_request")

    def test_register_email_is_case_insensitive_unique_and_name_trimmed(self):
        self.assertEqual(self.register(name="  Sara Ali  ").status_code, 201)
        self.assertEqual(self.user().partner_id.name, "Sara Ali")
        self.assertCode(self.register(email="SARA.ALI@EXAMPLE.COM", phone="+966500000009"), 409, "email_taken")

    def test_forgot_is_idempotent_and_supersedes_the_previous_code(self):
        self.register()
        for _i in range(2):
            self.assertEqual(self.post("/auth/password/forgot", {"email": EMAIL}).status_code, 202)
        codes = self.env["leaders.auth.reset.code"].sudo().search([("user_id", "=", self.user().id)])
        self.assertEqual(sorted(codes.mapped("state")), ["pending", "superseded"])
        self.assertEqual(self.post("/auth/password/forgot", {"email": EMAIL}).status_code, 202)
        self.assertCode(self.post("/auth/password/forgot", {"email": EMAIL}), 429, "rate_limited")
        self.assertCode(self.post("/auth/password/forgot", {"email": "bad"}), 422, "validation_error")

    def test_verify_and_reset_field_validation(self):
        r = self.post("/auth/password/verify", {"email": "bad", "code": "12"})
        self.assertCode(r, 422, "validation_error")
        self.assertEqual(set(r.json()["fields"]), {"email", "code"})
        r = self.post("/auth/password/reset", {})
        self.assertCode(r, 422, "validation_error")
        self.assertEqual(set(r.json()["fields"]), {"resetToken", "password"})

    def test_reset_token_cannot_be_used_after_it_expires(self):
        self.register()
        self.post("/auth/password/forgot", {"email": EMAIL})
        Code = self.env["leaders.auth.reset.code"].sudo()
        record = Code.search([("user_id", "=", self.user().id), ("state", "=", "pending")])
        record.write({"code_hash": Code._hash_code(record.salt, record.user_id.id, "654321")})
        self.env.flush_all()
        token = self.post("/auth/password/verify", {"email": EMAIL, "code": "654321"}).json()["resetToken"]
        record.reset_expires_at = fields.Datetime.subtract(fields.Datetime.now(), seconds=1)
        self.env.flush_all()
        self.assertCode(self.post("/auth/password/reset", {"resetToken": token, "password": PASSWORD}),
                        400, "reset_token_expired")

    def test_login_remember_defaults_to_false_and_requires_a_real_boolean(self):
        self.register()
        r = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD, "remember": "true"})
        self.assertIn("Max-Age=%s" % (7 * 86400), self.set_cookies(r))


@tagged("post_install", "-at_install")
class TestAuthApiAvatar(AuthApiCommon):

    PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

    def test_generated_avatar_is_null_and_a_real_picture_is_a_url(self):
        self.assertIsNone(self.register().json()["user"]["avatar"])
        self.user().partner_id.image_1920 = self.PNG
        self.env.flush_all()
        data = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}).json()
        url = data["user"]["avatar"]
        self.assertRegex(url, r"^https?://.+/account/avatar/\d+/[0-9a-f]{32}$")
        # a visitor (the storefront's <img>) gets the real picture, not Odoo's placeholder
        self.opener.cookies.clear()
        path = url[url.index("/account/avatar/"):]
        r = self.opener.get(self.base_url() + path, timeout=30)
        self.assertEqual((r.status_code, r.headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(r.content[:8], b"\x89PNG\r\n\x1a\n")
        placeholder = self.opener.get(self.base_url() + "/web/static/img/placeholder.png", timeout=30).content
        self.assertNotEqual(r.content, placeholder)
        # a forged or stale signature, or another partner's id, shows nothing
        partner_id = path.split("/")[3]
        self.assertEqual(self.opener.get(self.base_url() + path[:-4] + "0000", timeout=30).status_code, 404)
        self.assertEqual(self.opener.get(self.base_url() + path.replace("/%s/" % partner_id, "/1/"), timeout=30).status_code, 404)
        self.user().partner_id.image_1920 = False
        self.env.flush_all()
        self.assertEqual(self.opener.get(self.base_url() + path, timeout=30).status_code, 404)


# ----------------------------------------------------------------------------------------------
# Exhaustive cases: every endpoint, boundaries, wrong types, isolation, storage, limits, config
# ----------------------------------------------------------------------------------------------
@tagged("post_install", "-at_install")
class TestAuthRegisterCases(AuthApiCommon):

    def test_name_length_boundaries(self):
        n = 0
        for name, status in (("A", 422), ("   ", 422), ("Al", 201), ("x" * 80, 201), ("x" * 81, 422)):
            n += 1
            r = self.register(name=name, email="n%s@example.com" % n, phone="+9665000000%02d" % n)
            self.assertEqual(r.status_code, status, (name, r.text))
            if status == 422:
                self.assertEqual(set(r.json()["fields"]), {"name"})

    def test_password_rules_boundaries(self):
        for i, (pw, status) in enumerate((("Abcdef1", 422), ("Abcdefg1", 201), ("abcdefg1", 422), ("Abcdefgh", 422), ("ABCDEFG1", 201))):
            r = self.register(password=pw, email="pw%s@example.com" % i, phone="+9665000001%02d" % i)
            self.assertEqual(r.status_code, status, (pw, r.text))
            if status == 422:
                self.assertEqual(set(r.json()["fields"]), {"password"})

    def test_phone_formats(self):
        spaced = self.register(phone="+966 55 501 9283", email="sp@example.com")
        self.assertEqual(spaced.status_code, 201, spaced.text)
        self.assertEqual(self.env["res.users"].search([("login", "=", "sp@example.com")]).partner_id.phone, "+966555019283")
        for i, bad in enumerate(("966501234567", "+0501234567", "+96650", "+9665012345678901", "+966abc1234567", "")):
            r = self.register(phone=bad, email="bp%s@example.com" % i)
            self.assertCode(r, 422, "validation_error")
            self.assertEqual(set(r.json()["fields"]), {"phone"}, bad)

    def test_email_formats(self):
        self.assertEqual(self.register(email="plus+tag@example.com", phone="+966500000010").status_code, 201)
        for i, bad in enumerate(("a@b", "a@@b.com", "no-at.example.com", "has space@example.com", "")):
            r = self.register(email=bad, phone="+96650000002%s" % i)
            self.assertCode(r, 422, "validation_error")
            self.assertEqual(set(r.json()["fields"]), {"email"}, bad)

    def test_wrong_json_types_are_validation_errors_not_crashes(self):
        r = self.post("/auth/register", {"name": 123, "email": None, "phone": ["+966501234567"], "password": {"a": 1}})
        self.assertCode(r, 422, "validation_error")
        self.assertEqual(set(r.json()["fields"]), {"name", "email", "phone", "password"})
        self.assertEqual(set(self.post("/auth/register", {}).json()["fields"]), {"name", "email", "phone", "password"})

    def test_arabic_name_and_unicode_are_stored(self):
        r = self.register(name="ليلى حسن", email="layla@example.com", phone="+966500000030")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["user"]["name"], "ليلى حسن")

    def test_email_is_unique_against_users_created_elsewhere_with_mixed_case(self):
        self.make_user("Mixed.Case@Example.com", name="Existing")
        self.assertCode(self.register(email="mixed.case@example.com"), 409, "email_taken")
        ok = self.post("/auth/login", {"email": "mixed.case@example.com", "password": PASSWORD})
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_phone_is_unique_across_accounts(self):
        self.assertEqual(self.register().status_code, 201)
        self.assertCode(self.register(email="other@example.com", phone="+966 50 123 4567"), 409, "phone_taken")

    def test_concurrent_signup_with_the_same_email_is_409_not_500(self):
        from ..services.auth_service import AuthService
        self.assertEqual(self.register().status_code, 201)
        with patch.object(AuthService, "_find_user", return_value=self.env["res.users"]), \
                patch.object(AuthService, "_phone_taken", return_value=False):
            r = self.register(phone="+966500000002")
        self.assertCode(r, 409, "email_taken")

    def test_a_failed_create_is_mapped_by_its_real_cause(self):
        from odoo.exceptions import ValidationError
        from ..services.auth_service import AuthService
        users = type(self.env["res.users"])
        # the race was on the phone: second look finds it taken
        with patch.object(users, "create", side_effect=ValidationError("dup")), \
                patch.object(AuthService, "_phone_taken", side_effect=[False, True]):
            self.assertCode(self.register(), 409, "phone_taken")
        # an unrelated validation failure is not disguised as a conflict
        with patch.object(users, "create", side_effect=ValidationError("something else")), \
                patch.object(AuthService, "_phone_taken", return_value=False):
            self.assertCode(self.register(email="other@example.com"), 400, "invalid_request")

    def test_the_returned_access_token_really_authenticates(self):
        r = self.register()
        out = self.post("/auth/logout", headers={"Authorization": "Bearer " + r.json()["accessToken"]})
        self.assertEqual(out.status_code, 204)

    def test_password_is_stored_hashed_and_the_user_is_portal_by_default(self):
        self.register()
        self.env.cr.execute("SELECT password FROM res_users WHERE login = %s", (EMAIL,))
        stored = self.env.cr.fetchone()[0]
        self.assertNotEqual(stored, PASSWORD)
        self.assertTrue(stored.startswith("$pbkdf2"))
        self.assertTrue(self.user().share)


@tagged("post_install", "-at_install")
class TestAuthLoginCases(AuthApiCommon):

    def test_login_tells_a_vendor_from_a_customer(self):
        self.register()
        login = {"email": EMAIL, "password": PASSWORD}
        self.assertEqual(self.post("/auth/login", login).json()["user"]["type"], "customer")
        company = self.user().partner_id.commercial_partner_id
        company.sudo().write({"is_marketplace_vendor": True, "vendor_state": "submitted"})
        self.env.flush_all()
        self.assertEqual(self.post("/auth/login", login).json()["user"]["type"], "customer")  # still under review
        for state in ("approved", "frozen"):
            company.sudo().vendor_state = state
            self.env.flush_all()
            data = self.post("/auth/login", login).json()
            self.assertEqual(data["user"]["type"], "vendor", state)
        profile = self.opener.get(self.base_url() + "/account/profile",
                                  headers={"Authorization": "Bearer %s" % data["accessToken"]}, timeout=30)
        self.assertEqual(profile.json()["type"], "vendor")

    def test_login_trims_and_ignores_case_of_the_email_but_not_of_the_password(self):
        self.register()
        self.assertEqual(self.post("/auth/login", {"email": "  SARA.ALI@EXAMPLE.COM ", "password": PASSWORD}).status_code, 200)
        self.assertCode(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD.lower()}), 401, "invalid_credentials")
        self.assertCode(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD + " "}), 401, "invalid_credentials")

    def test_remember_only_counts_when_it_is_a_real_true(self):
        self.register()
        for value in ("true", 1, "yes", None, False):
            r = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD, "remember": value})
            self.assertIn("Max-Age=%s" % (7 * 86400), self.set_cookies(r), value)
        r = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD, "remember": True})
        self.assertIn("Max-Age=%s" % (30 * 86400), self.set_cookies(r))

    def test_archived_user_is_disabled_only_with_the_right_password(self):
        self.make_user("old@example.com", active=False)
        self.assertCode(self.post("/auth/login", {"email": "old@example.com", "password": "Wrong-pass1"}), 401, "invalid_credentials")
        self.assertCode(self.post("/auth/login", {"email": "old@example.com", "password": PASSWORD}), 403, "account_disabled")

    def test_injection_style_input_is_just_a_wrong_login(self):
        for evil in ("x'--@example.com", "a');DROP/**/TABLE/**/res_users;--@example.com", "%@example.com", "_@example.com"):
            r = self.post("/auth/login", {"email": evil, "password": PASSWORD})
            self.assertCode(r, 401, "invalid_credentials")
        r = self.post("/auth/login", {"email": "a'; DROP TABLE res_users;--@example.com", "password": PASSWORD})
        self.assertCode(r, 422, "validation_error")  # spaces make it an invalid email before any lookup
        self.assertTrue(self.env["res.users"].search_count([]))

    def test_login_updates_the_last_login_date(self):
        self.register()
        before = self.user().login_date
        self.assertEqual(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}).status_code, 200)
        self.env.invalidate_all()
        self.assertTrue(self.user().login_date)
        self.assertNotEqual(self.user().login_date, before) if before else None

    def test_each_login_gets_its_own_token_and_cookie(self):
        self.register()
        a = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD})
        b = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD})
        self.assertNotEqual(a.json()["accessToken"], b.json()["accessToken"])
        self.assertNotEqual(self.refresh_cookie(a), self.refresh_cookie(b))

    def test_access_token_claims(self):
        import jwt
        from ..tools.auth_tokens import ALGORITHM, _secret
        self.register()
        r = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}).json()
        claims = jwt.decode(r["accessToken"], _secret(self.env), algorithms=[ALGORITHM])
        self.assertEqual(claims["sub"], r["user"]["id"])
        self.assertEqual(claims["typ"], "access")
        self.assertEqual(claims["exp"] - claims["iat"], r["expiresIn"])
        self.assertEqual(len(claims["jti"]), 32)

    def test_odoo_login_cooldown_still_protects_the_endpoint(self):
        self.register()
        self.param.set_param("base.login_cooldown_after", "3")
        for _i in range(3):
            self.assertCode(self.post("/auth/login", {"email": EMAIL, "password": "Wrong-pass1"}), 401, "invalid_credentials")
        # now even the right password is refused during the cooldown
        self.assertCode(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}), 401, "invalid_credentials")
        del self.registry._login_failures
        self.param.set_param("base.login_cooldown_after", "0")


@tagged("post_install", "-at_install")
class TestAuthRefreshLogoutCases(AuthApiCommon):

    def test_remember_survives_rotation(self):
        self.register()
        r = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD, "remember": True})
        again = self.post("/auth/refresh", cookies={"refresh_token": self.refresh_cookie(r)})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertIn("Max-Age=%s" % (30 * 86400), self.set_cookies(again))

    def test_rotation_keeps_the_family_and_stores_only_hashes(self):
        Token = self.env["leaders.auth.refresh.token"].sudo()
        raw = self.refresh_cookie(self.register())
        self.assertFalse(Token.search([("token_hash", "=", raw)]))
        new_raw = self.refresh_cookie(self.post("/auth/refresh", cookies={"refresh_token": raw}))
        records = Token.search([("user_id", "=", self.user().id)], order="id")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].family, records[1].family)
        self.assertEqual([records[0].revoked, records[1].revoked], [True, False])
        self.assertNotIn(new_raw, records.mapped("token_hash"))

    def test_sessions_on_two_devices_are_independent(self):
        self.register()
        a = self.refresh_cookie(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}))
        b = self.refresh_cookie(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}))
        self.assertEqual(self.post("/auth/refresh", cookies={"refresh_token": a}).status_code, 200)
        self.assertCode(self.post("/auth/refresh", cookies={"refresh_token": a}), 401, "session_expired")  # reuse
        self.assertEqual(self.post("/auth/refresh", cookies={"refresh_token": b}).status_code, 200)  # untouched

    def test_expired_archived_and_garbage_refresh_tokens(self):
        Token = self.env["leaders.auth.refresh.token"].sudo()
        raw = self.refresh_cookie(self.register())
        Token.search([("user_id", "=", self.user().id)]).expires_at = fields.Datetime.subtract(fields.Datetime.now(), seconds=1)
        self.env.flush_all()
        self.assertCode(self.post("/auth/refresh", cookies={"refresh_token": raw}), 401, "session_expired")
        raw2 = self.refresh_cookie(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}))
        self.user().active = False
        self.env.flush_all()
        self.assertCode(self.post("/auth/refresh", cookies={"refresh_token": raw2}), 401, "session_expired")
        for junk in ("", "x", "a" * 500, "%00", "' OR 1=1 --"):
            self.assertCode(self.post("/auth/refresh", cookies={"refresh_token": junk}), 401, "session_expired")

    def test_refresh_response_is_a_full_session(self):
        r = self.register()
        again = self.post("/auth/refresh", cookies={"refresh_token": self.refresh_cookie(r)})
        data = again.json()
        self.assertEqual(set(data), {"accessToken", "tokenType", "expiresIn", "user"})
        self.assertEqual(data["user"], r.json()["user"])
        self.assertNotEqual(data["accessToken"], r.json()["accessToken"])
        self.assertIn("spark_session=1", self.set_cookies(again))

    def test_logout_only_revokes_the_bearers_own_refresh_token(self):
        a = self.register()
        b = self.register(email="bob@example.com", phone="+966500000040", name="Bob Stone")
        out = self.post("/auth/logout", headers={"Authorization": "Bearer " + a.json()["accessToken"]},
                        cookies={"refresh_token": self.refresh_cookie(b)})
        self.assertEqual(out.status_code, 204)
        self.assertEqual(self.post("/auth/refresh", cookies={"refresh_token": self.refresh_cookie(b)}).status_code, 200)

    def test_logout_clears_both_cookies_even_without_a_refresh_cookie(self):
        token = self.register().json()["accessToken"]
        out = self.post("/auth/logout", headers={"Authorization": "Bearer " + token})
        self.assertEqual(out.status_code, 204)
        cleared = self.set_cookies(out)
        self.assertIn("refresh_token=;", cleared)
        self.assertIn("spark_session=;", cleared)
        self.assertEqual(out.text, "")

    def test_logout_accepts_extra_spaces_and_rejects_other_schemes(self):
        token = self.register().json()["accessToken"]
        self.assertEqual(self.post("/auth/logout", headers={"Authorization": "Bearer   " + token}).status_code, 204)
        for header in ("Token " + token, "Bearer", "Bearer ", token):
            self.assertCode(self.post("/auth/logout", headers={"Authorization": header}), 401, "session_expired")

    def test_logout_with_an_unknown_refresh_cookie_is_still_204(self):
        token = self.register().json()["accessToken"]
        out = self.post("/auth/logout", headers={"Authorization": "Bearer " + token}, cookies={"refresh_token": "nope"})
        self.assertEqual(out.status_code, 204)


@tagged("post_install", "-at_install")
class TestAuthPasswordResetCases(AuthApiCommon):

    def setUp(self):
        super().setUp()
        self.register()

    def forgot(self, email=EMAIL):
        return self.post("/auth/password/forgot", {"email": email})

    def test_the_emailed_code_really_works_end_to_end(self):
        self.assertEqual(self.forgot().status_code, 202)
        code = self.emailed_code(EMAIL)
        self.assertRegex(code or "", r"^\d{6}$")
        v = self.post("/auth/password/verify", {"email": EMAIL, "code": code})
        self.assertEqual(v.status_code, 200, v.text)
        self.assertEqual(self.post("/auth/password/reset", {"resetToken": v.json()["resetToken"], "password": "Fresh-pass9"}).status_code, 204)
        self.assertEqual(self.post("/auth/login", {"email": EMAIL, "password": "Fresh-pass9"}).status_code, 200)

    def test_forgot_is_case_insensitive_and_skips_unknown_and_archived_accounts(self):
        Code = self.env["leaders.auth.reset.code"].sudo()
        self.assertEqual(self.forgot("SARA.ALI@Example.com").status_code, 202)
        self.assertEqual(Code.search_count([]), 1)
        self.make_user("gone@example.com", active=False)
        for email in ("gone@example.com", "nobody@example.com"):
            self.assertEqual(self.forgot(email).status_code, 202)
        self.assertEqual(Code.search_count([]), 1)

    def test_the_code_email_wakes_the_mail_queue_at_once(self):
        cron = self.env.ref("mail.ir_cron_mail_scheduler_action")
        Trigger = self.env["ir.cron.trigger"].sudo()
        before = Trigger.search_count([("cron_id", "=", cron.id)])
        self.assertEqual(self.forgot().status_code, 202)
        self.assertGreater(Trigger.search_count([("cron_id", "=", cron.id)]), before)

    def test_a_mail_failure_never_leaks_to_the_caller(self):
        with patch("odoo.addons.mail.models.mail_template.MailTemplate.send_mail", side_effect=RuntimeError("smtp down")):
            r = self.forgot()
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.text, "")

    def test_the_code_is_stored_hashed_and_a_new_code_replaces_the_old_one(self):
        self.forgot()
        first = self.emailed_code(EMAIL)
        record = self.env["leaders.auth.reset.code"].sudo().search([("state", "=", "pending")])
        self.assertNotEqual(record.code_hash, first)
        self.assertNotIn(first, record.code_hash)
        self.env["mail.mail"].sudo().search([("email_to", "=", EMAIL)]).unlink()
        self.forgot()
        second = self.emailed_code(EMAIL)
        wrong = first if first != second else str((int(first) + 1) % 10 ** 6).zfill(6)
        self.assertCode(self.post("/auth/password/verify", {"email": EMAIL, "code": wrong}), 400, "invalid_code")
        self.assertEqual(self.post("/auth/password/verify", {"email": EMAIL, "code": second}).status_code, 200)

    def test_verify_edge_cases(self):
        self.assertCode(self.post("/auth/password/verify", {"email": EMAIL, "code": "123456"}), 400, "invalid_code")  # never asked
        self.assertCode(self.post("/auth/password/verify", {"email": "ghost@example.com", "code": "123456"}), 400, "invalid_code")
        for bad in ("12345", "1234567", "12345a", "", " ", "12 456"):
            r = self.post("/auth/password/verify", {"email": EMAIL, "code": bad})
            self.assertCode(r, 422, "validation_error")
            self.assertEqual(set(r.json()["fields"]), {"code"}, bad)
        self.forgot()
        code = self.emailed_code(EMAIL)
        padded = self.post("/auth/password/verify", {"email": EMAIL, "code": " %s " % code})
        self.assertEqual(padded.status_code, 200, padded.text)
        self.assertCode(self.post("/auth/password/verify", {"email": EMAIL, "code": code}), 400, "invalid_code")  # used

    def test_wrong_attempts_are_counted_in_the_database(self):
        self.forgot()
        for n in (1, 2, 3):
            self.post("/auth/password/verify", {"email": EMAIL, "code": "000000"})
            self.assertEqual(self.env["leaders.auth.reset.code"].sudo().search([]).attempts, n)

    def test_the_right_code_is_refused_after_five_wrong_ones(self):
        self.forgot()
        code = self.emailed_code(EMAIL)
        wrong = "000000" if code != "000000" else "111111"
        for _i in range(5):
            self.post("/auth/password/verify", {"email": EMAIL, "code": wrong})
        self.assertCode(self.post("/auth/password/verify", {"email": EMAIL, "code": code}), 429, "too_many_attempts")

    def _token(self):
        self.forgot()
        return self.post("/auth/password/verify", {"email": EMAIL, "code": self.emailed_code(EMAIL)}).json()["resetToken"]

    def test_a_weak_password_does_not_burn_the_reset_token(self):
        token = self._token()
        self.assertCode(self.post("/auth/password/reset", {"resetToken": token, "password": "weak"}), 422, "validation_error")
        self.assertEqual(self.post("/auth/password/reset", {"resetToken": token, "password": "Fresh-pass9"}).status_code, 204)

    def test_a_newer_code_invalidates_an_already_issued_reset_token(self):
        token = self._token()
        self.env["mail.mail"].sudo().search([("email_to", "=", EMAIL)]).unlink()
        self.forgot()
        self.assertCode(self.post("/auth/password/reset", {"resetToken": token, "password": "Fresh-pass9"}), 400, "invalid_reset_token")

    def test_reset_changes_only_that_users_password_and_signs_out_every_device(self):
        other = self.register(email="bob@example.com", phone="+966500000040", name="Bob Stone")
        devices = [self.refresh_cookie(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD})) for _i in range(2)]
        token = self._token()
        self.assertEqual(self.post("/auth/password/reset", {"resetToken": token, "password": "Fresh-pass9"}).status_code, 204)
        for raw in devices:
            self.assertCode(self.post("/auth/refresh", cookies={"refresh_token": raw}), 401, "session_expired")
        self.assertEqual(self.post("/auth/refresh", cookies={"refresh_token": self.refresh_cookie(other)}).status_code, 200)
        self.assertEqual(self.post("/auth/login", {"email": "bob@example.com", "password": PASSWORD}).status_code, 200)

    def test_reset_token_is_stored_hashed_and_validated_strictly(self):
        token = self._token()
        record = self.env["leaders.auth.reset.code"].sudo().search([("state", "=", "verified")])
        self.assertNotEqual(record.reset_token_hash, token)
        for junk in ("", "x", token[:-1], token + "x", token.upper()):
            r = self.post("/auth/password/reset", {"resetToken": junk, "password": "Fresh-pass9"})
            self.assertEqual(r.status_code, 422 if junk == "" else 400, junk)
        self.assertEqual(self.post("/auth/password/reset", {"resetToken": token, "password": "Fresh-pass9"}).status_code, 204)

    def test_purge_crons_remove_only_old_rows(self):
        Token = self.env["leaders.auth.refresh.token"].sudo()
        Code = self.env["leaders.auth.reset.code"].sudo()
        self.forgot()
        old_code = Code.search([])
        self.env.cr.execute("UPDATE leaders_auth_reset_code SET create_date = NOW() - INTERVAL '3 days' WHERE id = %s", (old_code.id,))
        self.forgot("sara.ali@example.com")
        old_tok = Token.search([("user_id", "=", self.user().id)])
        self.env.cr.execute("UPDATE leaders_auth_refresh_token SET expires_at = NOW() - INTERVAL '3 days' WHERE id = %s", (old_tok.id,))
        self.env.invalidate_all()
        keep = self.refresh_cookie(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}))
        Token.cron_purge()
        Code.cron_purge()
        self.env["leaders.auth.rate"].sudo().cron_purge()
        self.assertFalse(old_tok.exists())
        self.assertFalse(old_code.exists())
        self.assertEqual(self.post("/auth/refresh", cookies={"refresh_token": keep}).status_code, 200)


@tagged("post_install", "-at_install")
class TestAuthRateLimitCases(AuthApiCommon):

    def hammer(self, path, bodies, cookies=None):
        return [self.post(path, b, cookies=cookies).status_code for b in bodies]

    def test_register_limit_is_10_per_minute_per_ip(self):
        codes = self.hammer("/auth/register", [{} for _i in range(12)])
        self.assertEqual(codes[:10], [422] * 10)
        self.assertEqual(codes[10:], [429, 429])

    def test_login_limit_is_10_per_minute_per_email_and_other_emails_are_unaffected(self):
        bad = {"email": EMAIL, "password": "Wrong-pass1"}
        codes = self.hammer("/auth/login", [bad] * 12)
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10:], [429, 429])
        self.assertEqual(self.post("/auth/login", {"email": "other@example.com", "password": "x"}).status_code, 401)

    def test_refresh_limit_is_60_per_minute_per_ip(self):
        codes = self.hammer("/auth/refresh", [None] * 62)
        self.assertEqual(codes[:60], [401] * 60)
        self.assertEqual(codes[60:], [429, 429])

    def test_forgot_limits(self):
        codes = self.hammer("/auth/password/forgot", [{"email": EMAIL}] * 4)  # 3 per 10 min per email
        self.assertEqual(codes, [202, 202, 202, 429])
        ip = self.hammer("/auth/password/forgot", [{"email": "u%s@example.com" % i} for i in range(9)])
        self.assertEqual(ip[-1], 429)  # the 10 per minute IP cap (1 already used above)

    def test_verify_and_reset_limits(self):
        v = self.hammer("/auth/password/verify", [{"email": EMAIL, "code": "000000"}] * 32)
        self.assertEqual(v[:30], [400] * 30)
        self.assertEqual(v[30:], [429, 429])
        r = self.hammer("/auth/password/reset", [{"resetToken": "x", "password": PASSWORD}] * 12)
        self.assertEqual(r[:10], [400] * 10)
        self.assertEqual(r[10:], [429, 429])

    def test_a_429_has_the_standard_error_body(self):
        self.hammer("/auth/register", [{} for _i in range(10)])
        r = self.post("/auth/register", {})
        self.assertCode(r, 429, "rate_limited")
        self.assertEqual(set(r.json()), {"code", "message"})

    def test_counters_live_in_the_database_so_they_are_shared_between_workers(self):
        self.hammer("/auth/register", [{}] * 3)
        self.env.cr.execute("SELECT count FROM leaders_auth_rate WHERE bucket LIKE 'register_ip:%'")
        self.assertEqual(self.env.cr.fetchone()[0], 3)


@tagged("post_install", "-at_install")
class TestAuthConfigCorsAndErrorCases(AuthApiCommon):

    def test_secure_flag_is_on_by_default_and_domain_is_configurable(self):
        self.param.set_param("leaders_marketplace.auth_cookie_secure", False)
        r = self.register()
        lines = r.raw.headers.getlist("Set-Cookie")
        self.assertEqual(sorted(l.split("=")[0] for l in lines), ["refresh_token", "spark_session"])  # no Odoo session cookie
        for line in lines:
            self.assertIn("Secure", line)
            self.assertIn("SameSite=None", line)
        self.param.set_param("leaders_marketplace.auth_cookie_domain", ".example.com")
        out = self.post("/auth/logout", headers={"Authorization": "Bearer " + r.json()["accessToken"]})
        for line in out.raw.headers.getlist("Set-Cookie"):
            self.assertRegex(line, r"Domain=\.?example\.com")  # browsers treat the leading dot as optional
        login = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD})
        for line in login.raw.headers.getlist("Set-Cookie"):
            self.assertRegex(line, r"Domain=\.?example\.com")  # browsers treat the leading dot as optional

    def test_access_token_lifetime_setting(self):
        key = "leaders_marketplace.auth_access_token_seconds"
        self.register()
        for value, expected in (("300", 300), ("10", 120), ("abc", 900), ("", 900)):
            self.param.set_param(key, value)
            r = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD})
            self.assertEqual(r.json()["expiresIn"], expected, value)

    def test_the_jwt_secret_is_generated_once_and_kept(self):
        key = "leaders_marketplace.auth_jwt_secret"
        self.param.set_param(key, False)
        self.register()
        first = self.param.get_param(key)
        self.assertEqual(len(first), 64)
        self.post("/auth/login", {"email": EMAIL, "password": PASSWORD})
        self.assertEqual(self.param.get_param(key), first)

    def test_cors_matrix(self):
        origin = "https://app.example.com"
        body = {"email": "x@example.com", "password": "p"}
        r = self.post("/auth/login", body)  # not configured yet
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)
        pre = self.opener.options(self.base_url() + "/auth/login", headers={"Origin": origin}, timeout=30)
        self.assertEqual(pre.status_code, 204)
        self.assertNotIn("Access-Control-Allow-Origin", pre.headers)
        self.param.set_param("leaders_marketplace.auth_cors_origin", origin)
        self.assertNotIn("Access-Control-Allow-Origin", self.post("/auth/login", body).headers)  # no Origin header sent
        ok = self.post("/auth/login", body, headers={"Origin": origin})
        self.assertEqual(ok.headers["Access-Control-Allow-Origin"], origin)
        self.assertEqual(ok.headers["Access-Control-Allow-Credentials"], "true")
        self.assertIn("Origin", ok.headers["Vary"])
        for wrong in ("https://app.example.com.evil.test", "http://app.example.com", "null", "*"):
            self.assertNotIn("Access-Control-Allow-Origin", self.post("/auth/login", body, headers={"Origin": wrong}).headers, wrong)
        pre = self.opener.options(self.base_url() + "/auth/login", headers={"Origin": origin}, timeout=30)
        self.assertEqual(pre.headers["Access-Control-Allow-Origin"], origin)
        self.assertEqual(pre.headers["Access-Control-Allow-Headers"], "Content-Type, Authorization")
        self.assertEqual(pre.headers["Access-Control-Max-Age"], "600")
        errors = self.post("/auth/login", {}, headers={"Origin": origin})
        self.assertEqual(errors.headers["Access-Control-Allow-Origin"], origin)  # error responses carry CORS too

    def test_every_error_has_exactly_the_documented_shape(self):
        cases = [
            self.post("/auth/register", {}), self.post("/auth/login", {}), self.post("/auth/refresh"),
            self.post("/auth/logout"), self.post("/auth/password/forgot", {}),
            self.post("/auth/password/verify", {}), self.post("/auth/password/reset", {}),
            self.post("/auth/login", {"email": "a@example.com", "password": "x"}),
        ]
        for r in cases:
            self.assertGreaterEqual(r.status_code, 400)
            body = r.json()
            self.assertIn(set(body), ({"code", "message"}, {"code", "message", "fields"}))
            self.assertRegex(body["code"], r"^[a-z]+(_[a-z]+)*$")
            self.assertNotIn("success", body)
            self.assertNotIn("data", body)

    def test_unexpected_failures_become_a_clean_500_and_user_errors_a_400(self):
        from odoo.exceptions import UserError
        from ..services.auth_service import AuthService
        with patch.object(AuthService, "login", side_effect=RuntimeError("boom")):
            r = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD})
        self.assertCode(r, 500, "server_error")
        self.assertNotIn("boom", r.text)
        with patch.object(AuthService, "login", side_effect=UserError("nope")):
            r = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD})
        self.assertCode(r, 400, "invalid_request")

    def test_responses_are_never_cacheable(self):
        for path, body in (("/auth/register", OK_USER), ("/auth/login", {}), ("/auth/refresh", None),
                           ("/auth/password/forgot", {"email": EMAIL})):
            self.assertEqual(self.post(path, body).headers["Cache-Control"], "no-store", path)


@tagged("post_install", "-at_install")
class TestAuthSignupOtp(AuthApiCommon):
    """Sign-up confirms the email with a code; Google needs the phone; sign out of every device."""

    def setUp(self):
        super().setUp()
        self.param.set_param("leaders_marketplace.auth_register_otp", "1")

    def test_signup_opens_the_session_only_after_the_email_code(self):
        r = self.register()
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["verification"], "email")
        self.assertNotIn("refresh_token=", self.set_cookies(r))
        self.assertFalse(self.user().leaders_email_verified)
        self.assertCode(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}), 403, "email_not_verified")
        code = self.emailed_code(EMAIL)
        self.assertTrue(code)
        self.assertCode(self.post("/auth/register/verify", {"email": EMAIL, "code": "000000" if code != "000000" else "111111"}),
                        400, "invalid_code")
        r = self.post("/auth/register/verify", {"email": EMAIL, "code": code})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(set(r.json()), {"accessToken", "tokenType", "expiresIn", "user"})
        self.assertTrue(self.user().leaders_email_verified)
        self.assertEqual(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}).status_code, 200)
        self.assertCode(self.register(), 409, "email_taken")  # a confirmed account is taken

    def test_the_signup_code_email_wakes_the_mail_queue_at_once(self):
        cron = self.env.ref("mail.ir_cron_mail_scheduler_action")
        Trigger = self.env["ir.cron.trigger"].sudo()
        before = Trigger.search_count([("cron_id", "=", cron.id)])
        self.assertEqual(self.register().status_code, 202)
        self.assertGreater(Trigger.search_count([("cron_id", "=", cron.id)]), before)

    def test_registering_again_before_confirming_sends_a_new_code(self):
        self.assertEqual(self.register().status_code, 202)
        first = self.emailed_code(EMAIL)
        self.assertEqual(self.register().status_code, 202)  # not "taken": the account was never confirmed
        self.assertEqual(self.post("/auth/register/resend", {"email": EMAIL}).status_code, 202)
        self.assertEqual(self.post("/auth/register/resend", {"email": "nobody@example.com"}).status_code, 202)
        latest = self.emailed_code(EMAIL)
        if latest != first:
            self.assertCode(self.post("/auth/register/verify", {"email": EMAIL, "code": first}), 400, "invalid_code")
        self.assertEqual(self.post("/auth/register/verify", {"email": EMAIL, "code": latest}).status_code, 201)

    CLIENT = "client-1.apps.googleusercontent.com"

    def _google(self, email="g.user@example.com", sub="sub-1", **extra):
        """Patch Google's token check to answer with these claims."""
        claims = {"aud": self.CLIENT, "iss": "https://accounts.google.com", "email": email, "email_verified": "true",
                  "name": "G User", "sub": sub}
        claims.update(extra)

        class Answer:
            status_code = 200

            def json(self):
                return claims
        return patch("odoo.addons.leaders_marketplace.services.auth_service.requests.get", return_value=Answer())

    def test_google_sign_up_needs_the_phone(self):
        self.param.set_param("leaders_marketplace.google_client_id", self.CLIENT)
        with self._google():
            self.assertCode(self.post("/auth/google", {"idToken": "t"}), 422, "phone_required")
            r = self.post("/auth/google", {"idToken": "t", "phone": "+201009998877"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("refresh_token=", self.set_cookies(r))  # the same cookies as login
            self.assertEqual(self.post("/auth/google", {"idToken": "t"}).status_code, 200)  # existing account: no phone asked
        user = self.env["res.users"].sudo().search([("login", "=", "g.user@example.com")])
        self.assertEqual((user.phone, user.leaders_email_verified, user.leaders_google_sub),
                         ("+201009998877", True, "sub-1"))

    def test_google_checks_the_token_and_answers_invalid_credentials(self):
        self.param.set_param("leaders_marketplace.google_client_id", self.CLIENT)
        for name, change in (("another client", {"aud": "other.apps.googleusercontent.com"}),
                             ("another issuer", {"iss": "https://evil.example.com"}),
                             ("email not verified", {"email_verified": "false"}),
                             ("no sub", {"sub": ""}),
                             ("expired", {"exp": "1000"})):
            with self._google(**change):
                self.assertCode(self.post("/auth/google", {"idToken": "t", "phone": "+201009998877"}),
                                401, "invalid_credentials")
        self.assertFalse(self.env["res.users"].sudo().search([("login", "=", "g.user@example.com")]), "nothing was created")

    def test_google_accepts_several_client_ids(self):
        self.param.set_param("leaders_marketplace.google_client_id", "web.apps.googleusercontent.com, %s" % self.CLIENT)
        with self._google():
            self.assertCode(self.post("/auth/google", {"idToken": "t"}), 422, "phone_required")  # the 2nd id passes

    def test_google_bad_phone_is_a_validation_error_and_a_taken_phone_is_409(self):
        self.param.set_param("leaders_marketplace.google_client_id", self.CLIENT)
        with self._google():
            r = self.post("/auth/google", {"idToken": "t", "phone": "123"})
            self.assertCode(r, 422, "validation_error")
            self.assertIn("phone", r.json()["fields"])
            self.register()  # an account with a phone
            taken = OK_USER["phone"]
            self.assertCode(self.post("/auth/google", {"idToken": "t", "phone": taken}), 409, "phone_taken")

    def test_google_links_a_password_account_by_email_then_finds_it_by_sub(self):
        self.param.set_param("leaders_marketplace.google_client_id", self.CLIENT)
        self.param.set_param("leaders_marketplace.auth_register_otp", "0")
        self.register()
        with self._google(email=EMAIL, sub="sub-9"):
            self.assertEqual(self.post("/auth/google", {"idToken": "t"}).status_code, 200)  # no phone: it exists
        self.assertEqual(self.user().leaders_google_sub, "sub-9")
        self.assertEqual(self.post("/auth/login", {"email": EMAIL, "password": PASSWORD}).status_code, 200)  # still works
        # the Google account changed its email: the sub still finds the account
        with self._google(email="renamed@example.com", sub="sub-9"):
            self.assertEqual(self.post("/auth/google", {"idToken": "t"}).status_code, 200)
        self.assertFalse(self.env["res.users"].sudo().search([("login", "=", "renamed@example.com")]))
        # another Google account claiming the same email is refused
        with self._google(email=EMAIL, sub="sub-other"):
            self.assertCode(self.post("/auth/google", {"idToken": "t"}), 401, "invalid_credentials")

    def test_a_google_account_can_set_a_password_with_forgot_password(self):
        self.param.set_param("leaders_marketplace.google_client_id", self.CLIENT)
        with self._google():
            self.assertEqual(self.post("/auth/google", {"idToken": "t", "phone": "+201009998877"}).status_code, 200)
        Code = self.env["leaders.auth.reset.code"].sudo()
        user = self.env["res.users"].sudo().search([("login", "=", "g.user@example.com")])
        self.assertEqual(self.post("/auth/password/forgot", {"email": "g.user@example.com"}).status_code, 202)
        record = Code.search([("user_id", "=", user.id), ("state", "=", "pending")])
        self.assertEqual(len(record), 1)
        record.write({"code_hash": Code._hash_code(record.salt, user.id, "123456")})
        self.env.flush_all()
        token = self.post("/auth/password/verify", {"email": "g.user@example.com", "code": "123456"}).json()["resetToken"]
        self.assertEqual(self.post("/auth/password/reset", {"resetToken": token, "password": "Brand-new9"}).status_code, 204)
        self.assertEqual(self.post("/auth/login", {"email": "g.user@example.com", "password": "Brand-new9"}).status_code, 200)

    def test_sign_out_of_every_device(self):
        self.param.set_param("leaders_marketplace.auth_register_otp", "0")
        first = self.register()
        token = first.json()["accessToken"]
        second = self.post("/auth/login", {"email": EMAIL, "password": PASSWORD})
        r = self.post("/auth/logout-all", headers={"Authorization": "Bearer %s" % token})
        self.assertEqual(r.status_code, 204, r.text)
        for response in (first, second):
            self.assertCode(self.post("/auth/refresh", cookies={"refresh_token": self.refresh_cookie(response)}),
                            401, "session_expired")
        self.assertEqual(self.post("/auth/logout-all").status_code, 401)



@tagged("post_install", "-at_install")
class TestAuthMasterCode(AuthApiCommon):
    """The shared testing code works for every account and every code step, next to the code that was sent."""

    MASTER = "246810"

    def setUp(self):
        super().setUp()
        self.param.set_param("leaders_marketplace.auth_register_otp", "1")
        self.param.set_param("leaders_marketplace.auth_master_code", self.MASTER)

    def test_sign_up_with_the_shared_code(self):
        self.assertEqual(self.register().status_code, 202)
        r = self.post("/auth/register/verify", {"email": EMAIL, "code": self.MASTER})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue(self.user().leaders_email_verified)

    def test_the_sent_code_still_works(self):
        self.assertEqual(self.register().status_code, 202)
        r = self.post("/auth/register/verify", {"email": EMAIL, "code": self.emailed_code(EMAIL)})
        self.assertEqual(r.status_code, 201, r.text)

    def test_password_reset_with_the_shared_code(self):
        self.assertEqual(self.register().status_code, 202)
        self.assertEqual(self.post("/auth/password/forgot", {"email": EMAIL}).status_code, 202)
        v = self.post("/auth/password/verify", {"email": EMAIL, "code": self.MASTER})
        self.assertEqual(v.status_code, 200, v.text)
        self.assertIn("resetToken", v.json())

    def test_needs_a_code_to_have_been_sent_and_can_be_switched_off(self):
        # nothing was sent to this account yet: the shared code does not skip the steps
        self.assertCode(self.post("/auth/password/verify", {"email": EMAIL, "code": self.MASTER}), 400, "invalid_code")
        self.param.set_param("leaders_marketplace.auth_master_code", "")
        self.assertEqual(self.register().status_code, 202)
        self.assertCode(self.post("/auth/register/verify", {"email": EMAIL, "code": self.MASTER}), 400, "invalid_code")
