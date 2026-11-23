"""The notification list of the app's bell: what the pushes carried, kept for the person to read later.

``GET /account/inbox`` (paged, newest first), ``POST /account/inbox/<id>/read`` and ``POST /account/inbox/read-all``.
New routes; nothing that exists changes (``/account/notifications`` stays the notification settings).
"""
from odoo import http
from odoo.http import request

from ..services.storefront import iso, number
from ..tools.auth_errors import AuthApiError, json_response, validation_error
from .storefront_common import bearer_user, rate_limit, storefront_api

DEFAULT_SIZE, MAX_SIZE = 10, 50


def _arabic():
    return (request.httprequest.headers.get("Accept-Language") or "").lower().startswith("ar")


class StorefrontInbox(http.Controller):

    @http.route("/account/inbox", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def inbox(self, cors, **kw):
        rate_limit("sf_account")
        user = bearer_user(required=True)
        params = request.httprequest.args
        errors = {}
        page = number(params.get("page")) if params.get("page") else 1
        size = number(params.get("pageSize")) if params.get("pageSize") else DEFAULT_SIZE
        if page is None or int(page) != page or page < 1:
            errors["page"] = "Must be 1 or more."
        if size is None or int(size) != size or not 1 <= size <= MAX_SIZE:
            errors["pageSize"] = "pageSize must be between 1 and %s." % MAX_SIZE
        if errors:
            raise validation_error(errors)
        page, size = int(page), int(size)
        Entry = request.env["leaders.inbox.entry"].sudo()
        domain = [("user_id", "=", user.id)]
        arabic = _arabic()
        rows = Entry.search(domain, order="id desc", limit=size, offset=(page - 1) * size)
        items = []
        for entry in rows:
            title, body = entry._leaders_texts_in(arabic)
            item = {"id": str(entry.id), "event": entry.event or "", "title": title, "body": body}
            if entry.reference:
                item["reference"] = entry.reference
            item.update(read=bool(entry.read), createdAt=iso(entry.create_date))
            items.append(item)
        return json_response({"items": items, "totalCount": Entry.search_count(domain), "page": page, "pageSize": size,
                              "unreadCount": Entry.search_count(domain + [("read", "=", False)])}, 200, cors)

    @http.route("/account/inbox/read-all", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def read_all(self, cors, **kw):
        rate_limit("sf_account")
        user = bearer_user(required=True)
        request.env["leaders.inbox.entry"].sudo().search([("user_id", "=", user.id), ("read", "=", False)]).write({"read": True})
        return json_response(None, 204, cors)

    @http.route("/account/inbox/<string:entry_id>/read", type="http", auth="public", methods=["POST", "OPTIONS"],
                csrf=False, save_session=False)
    @storefront_api(["POST"])
    def read(self, cors, entry_id, **kw):
        """Safe to repeat on an entry that is already read; an entry that is not this person's is 404."""
        rate_limit("sf_account")
        user = bearer_user(required=True)
        entry = request.env["leaders.inbox.entry"].sudo().search(
            [("id", "=", int(entry_id)), ("user_id", "=", user.id)], limit=1) if entry_id.isdigit() else None
        if not entry:
            raise AuthApiError(404, "not_found", "Not found.")
        if not entry.read:
            entry.read = True
        return json_response(None, 204, cors)
