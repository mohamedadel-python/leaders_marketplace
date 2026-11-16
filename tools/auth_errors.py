"""Auth API contract: error type and JSON response builders.

Success bodies are the resource itself (no envelope); errors are
``{"code": <snake_case>, "message": <text>, "fields": {...}?}``.
"""
import json

from werkzeug.wrappers import Response

from .api_messages import localize


class AuthApiError(Exception):
    def __init__(self, status, code, message, fields=None, extra=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.fields = fields or {}
        self.extra = extra or {}  # more keys of the error body (e.g. what is still available)


def validation_error(fields, message="Validation failed."):
    return AuthApiError(422, "validation_error", message, fields)


def json_response(body, status=200, headers=None):
    hdrs = [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")]
    hdrs.extend(headers or [])
    return Response(json.dumps(body) if body is not None else "", status=status, headers=hdrs)


def error_response(error, headers=None):
    message, fields = localize(error.message, error.fields)
    body = {"code": error.code, "message": message}
    if fields:
        body["fields"] = fields
    body.update(getattr(error, "extra", None) or {})
    return json_response(body, error.status, headers)
