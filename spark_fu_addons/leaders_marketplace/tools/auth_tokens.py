"""Auth API contract: access-token (JWT) and opaque-token helpers."""
import hashlib
import secrets
import time
import uuid

import jwt

SECRET_PARAM = "leaders_marketplace.auth_jwt_secret"
EXPIRY_PARAM = "leaders_marketplace.auth_access_token_seconds"
DEFAULT_EXPIRY = 900
MIN_EXPIRY = 120  # the client refreshes 60 s early, so the contract needs >= 120
ALGORITHM = "HS256"


class InvalidAccessToken(Exception):
    pass


def _secret(env):
    param = env["ir.config_parameter"].sudo()
    secret = param.get_param(SECRET_PARAM)
    if not secret:
        secret = secrets.token_hex(32)
        param.set_param(SECRET_PARAM, secret)
    return secret


def access_expiry_seconds(env):
    raw = env["ir.config_parameter"].sudo().get_param(EXPIRY_PARAM) or DEFAULT_EXPIRY
    try:
        return max(int(raw), MIN_EXPIRY)
    except (TypeError, ValueError):
        return DEFAULT_EXPIRY


def encode_access_token(env, user):
    now = int(time.time())
    payload = {"sub": str(user.id), "typ": "access", "iat": now,
               "exp": now + access_expiry_seconds(env), "jti": uuid.uuid4().hex}
    return jwt.encode(payload, _secret(env), algorithm=ALGORITHM)


def decode_access_token(env, token):
    try:
        payload = jwt.decode(token, _secret(env), algorithms=[ALGORITHM], options={"require": ["exp", "sub"]})
        if payload.get("typ") != "access":
            raise InvalidAccessToken()
        return int(payload["sub"])
    except (jwt.PyJWTError, ValueError):
        raise InvalidAccessToken()


def hash_token(raw):
    return hashlib.sha256((raw or "").encode()).hexdigest()
