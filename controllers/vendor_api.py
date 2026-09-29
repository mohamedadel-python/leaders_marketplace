"""B4: vendor write API (bearer keys ``lmk_...``), OpenAPI document.

Every endpoint resolves the key to ONE vendor and scopes every query to it, then
calls the same model methods as the portal routes. Errors are JSON:
``{"error": code, "message": text, "details": [...]}``.
"""
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

from odoo import _, fields, http
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)

MAX_LIMIT = 100
DEFAULT_LIMIT = 20
SHIPPABLE_STATES = ("allocated", "picking", "packing", "ready", "partially_shipped")


class ApiError(Exception):
    def __init__(self, status, code, message, details=None, headers=None, extra=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.details = details or []
        self.headers = headers or []
        self.extra = extra or {}


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _parse_datetime(value, field):
    """ISO 8601 -> naive UTC datetime (a naive input is taken as UTC)."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(field)
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(field)
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class Validator:
    """Collects field errors so the client gets all of them at once."""

    def __init__(self):
        self.details = []

    def add(self, field, message):
        self.details.append({"field": field, "message": message})

    def number(self, body, field, minimum=None, maximum=None, required=False, exclusive_min=False):
        if field not in body or body[field] is None:
            if required:
                self.add(field, _("This field is required."))
            return None
        value = body[field]
        if not _is_number(value):
            self.add(field, _("Must be a number."))
            return None
        if minimum is not None and (value <= minimum if exclusive_min else value < minimum):
            self.add(field, _("Must be greater than %s.", minimum) if exclusive_min
                     else _("Must be at least %s.", minimum))
            return None
        if maximum is not None and value > maximum:
            self.add(field, _("Must be at most %s.", maximum))
            return None
        return float(value)

    def integer(self, body, field, required=False, minimum=1):
        if field not in body or body[field] is None:
            if required:
                self.add(field, _("This field is required."))
            return None
        value = body[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            self.add(field, _("Must be a whole number of at least %s.", minimum))
            return None
        return value

    def string(self, body, field, required=False, max_len=500):
        if field not in body or body[field] is None:
            if required:
                self.add(field, _("This field is required."))
            return None
        value = body[field]
        if not isinstance(value, str):
            self.add(field, _("Must be text."))
            return None
        value = value.strip()
        if required and not value:
            self.add(field, _("This field is required."))
            return None
        if len(value) > max_len:
            self.add(field, _("Must be at most %s characters.", max_len))
            return None
        return value

    def unknown(self, body, allowed):
        for key in body:
            if key not in allowed:
                self.add(key, _("Unknown field."))

    def datetime(self, body, field):
        try:
            return _parse_datetime(body.get(field), field)
        except ValueError:
            self.add(field, _("Must be an ISO 8601 date and time (e.g. 2026-10-01T09:00:00Z)."))
            return None

    def check(self):
        if self.details:
            raise ApiError(422, "validation_error", _("The request is not valid."), self.details)


class VendorAPI(http.Controller):

    # ------------------------------------------------------------------ plumbing
    @staticmethod
    def _bearer_token():
        header = request.httprequest.headers.get("Authorization") or ""
        match = re.match(r"^bearer\s+(\S+)$", header, re.IGNORECASE)
        return match.group(1) if match else None

    @staticmethod
    def _error_response(exc, extra_headers=()):
        payload = {"error": exc.code, "message": str(exc.message), "details": exc.details}
        payload.update(exc.extra)
        return request.make_json_response(
            payload, status=exc.status, headers=list(exc.headers) + list(extra_headers))

    def _authenticate(self, scope):
        Key = request.env["leaders.vendor.api.key"]
        if not Key._leaders_api_enabled():
            raise ApiError(404, "not_found", _("Not found."))
        token = self._bearer_token()
        key = Key._leaders_authenticate(token) if token else Key.browse()
        if not key:
            raise ApiError(401, "unauthorized", _("Missing, invalid or revoked API key."),
                           headers=[("WWW-Authenticate", "Bearer")])
        if scope == "write" and key.scope != "write":
            raise ApiError(403, "insufficient_scope", _("This key is read only."))
        vendor = key.vendor_id.commercial_partner_id
        if not vendor.is_marketplace_vendor or vendor.vendor_state not in ("approved", "frozen"):
            raise ApiError(403, "not_vendor", _("Your account is not an approved marketplace vendor."))
        # From here on the request runs as the key's user, like the portal.
        request.update_env(user=key.user_id.id)
        key._leaders_touch()
        return SimpleNamespace(key=key, vendor=vendor, user=key.user_id, headers=[])

    def _json_body(self):
        raw = request.httprequest.get_data(as_text=True) or ""
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            raise ApiError(400, "invalid_json", _("The request body is not valid JSON."))
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", _("The request body must be a JSON object."))
        return body

    def _run(self, scope, handler, *args, **kw):
        """Authenticate, rate limit, replay/record idempotency and run ``handler``."""
        headers = []
        try:
            ctx = self._authenticate(scope)
            allowed, limit, remaining, retry_after = ctx.key._leaders_rate_hit()
            if limit:
                headers += [("X-RateLimit-Limit", str(limit)), ("X-RateLimit-Remaining", str(remaining))]
            if not allowed:
                raise ApiError(429, "rate_limited", _("Too many requests. Retry later."),
                               headers=[("Retry-After", str(retry_after))] + headers)
            from odoo.addons.leaders_marketplace.controllers.api import _terms_pending
            blocked = _terms_pending(ctx.vendor)
            if blocked:
                return blocked
            method = request.httprequest.method
            body = self._json_body() if method in ("POST", "PATCH") else None
            idem = None
            if method in ("POST", "PATCH"):
                idem = request.httprequest.headers.get("Idempotency-Key")
                if idem is not None:
                    idem = idem.strip()
                    if not idem or len(idem) > 255:
                        raise ApiError(400, "invalid_idempotency_key",
                                       _("The Idempotency-Key header must be 1 to 255 characters."))
                    ctx.key._leaders_lock()
                    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
                    fingerprint = hashlib.sha256(("%s %s %s" % (
                        method, request.httprequest.path, canonical)).encode()).hexdigest()
                    stored = request.env["leaders.vendor.api.idempotency"].sudo().search([
                        ("key_id", "=", ctx.key.id), ("idempotency_key", "=", idem)], limit=1)
                    if stored:
                        if stored.request_hash != fingerprint:
                            raise ApiError(422, "idempotency_key_conflict", _(
                                "This Idempotency-Key was already used with a different request."),
                                headers=headers)
                        return request.make_response(stored.body or "", headers=[
                            ("Content-Type", "application/json; charset=utf-8"),
                            ("Idempotent-Replay", "true")] + headers, status=stored.status)
            try:
                with request.env.cr.savepoint():
                    status, data = handler(ctx, body, *args, **kw)
                    text = json.dumps(data, ensure_ascii=False)
                    if idem is not None:
                        request.env["leaders.vendor.api.idempotency"].sudo().create({
                            "key_id": ctx.key.id, "idempotency_key": idem,
                            "request_hash": fingerprint, "status": status, "body": text})
            except (UserError, ValidationError) as exc:
                raise ApiError(422, "unprocessable", exc.args[0] if exc.args else str(exc))
            except MissingError:
                raise ApiError(404, "not_found", _("Not found."))
            except AccessError as exc:
                raise ApiError(403, "forbidden", exc.args[0] if exc.args else str(exc))
            return request.make_response(text, headers=[
                ("Content-Type", "application/json; charset=utf-8")] + headers, status=status)
        except ApiError as exc:
            return self._error_response(exc, headers if exc.status != 429 else ())
        except Exception:  # never a bare traceback for a client
            _logger.exception("Vendor API unexpected error")
            return request.make_json_response(
                {"error": "server_error", "message": "Unexpected error.", "details": []}, status=500)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _paginate(model, domain, order):
        args = request.httprequest.args
        v = Validator()
        page = limit = None
        for field, default in (("page", 1), ("limit", DEFAULT_LIMIT)):
            raw = args.get(field)
            if raw in (None, ""):
                value = default
            else:
                try:
                    value = int(raw)
                except ValueError:
                    v.add(field, _("Must be a whole number."))
                    continue
                if value < 1 or (field == "limit" and value > MAX_LIMIT):
                    v.add(field, _("Must be between 1 and %s.", MAX_LIMIT) if field == "limit"
                          else _("Must be at least 1."))
                    continue
            if field == "page":
                page = value
            else:
                limit = value
        v.check()
        total = model.search_count(domain)
        records = model.search(domain, order=order, limit=limit, offset=(page - 1) * limit)
        nxt = None
        if page * limit < total:
            params = {k: val for k, val in args.items() if k != "page"}
            params["page"] = page + 1
            nxt = "%s?%s" % (request.httprequest.path, urlencode(params))
        return records, {"page": page, "limit": limit, "total": total, "next": nxt}

    @staticmethod
    def _owned(model, record_id, vendor, domain=None):
        record = request.env[model].sudo().search(
            [("id", "=", record_id), ("vendor_id", "=", vendor.id)] + (domain or []), limit=1)
        if not record:
            raise ApiError(404, "not_found", _("Not found."))
        return record

    @staticmethod
    def _require_approved(vendor):
        if vendor.vendor_state != "approved":
            raise ApiError(403, "vendor_frozen", _(
                "Your vendor account is frozen, so you cannot list or change products."))

    @staticmethod
    def _choice_filter(field, allowed):
        value = request.httprequest.args.get(field)
        if value in (None, ""):
            return None
        if value not in allowed:
            raise ApiError(422, "validation_error", _("The request is not valid."),
                           [{"field": field, "message": _("Must be one of: %s.", ", ".join(allowed))}])
        return value

    # ------------------------------------------------------------------ serializers
    @staticmethod
    def _offer_json(o):
        return {
            "id": o.id, "seller_sku": o.seller_sku, "name": o.product_id.display_name,
            "product_id": o.product_id.id, "price": o.price, "currency": o.currency_id.name,
            "effective_price": o.effective_price, "fulfillment_type": o.fulfillment_type,
            "state": o.state, "active": o.active, "reject_reason": o.reject_reason or None,
            "stock_available": o.available_qty(),
            "discount": {
                "state": o.discount_state, "type": o.discount_type or None,
                "percent": o.discount_percent, "price": o.discount_price,
                "start": _iso(o.discount_start), "end": _iso(o.discount_end),
            },
        }

    @staticmethod
    def _order_json(o, detail=False):
        data = {
            "id": o.id, "name": o.name, "state": o.state, "date_order": _iso(o.date_order),
            "currency": o.currency_id.name, "amount_untaxed": o.amount_untaxed,
            "amount_tax": o.amount_tax, "amount_total": o.amount_total,
            "commission_amount": o.commission_amount, "vendor_net": o.vendor_net,
            "customer": o.partner_id.display_name, "date_shipped": _iso(o.date_shipped),
            "ship_by_date": _iso(o.ship_by_date), "is_late": bool(o.is_late),
            "updated": _iso(o.write_date),
        }
        if detail:
            data["lines"] = [{
                "id": l.id, "name": l.name, "product_id": l.product_id.id,
                "quantity": l.product_uom_qty, "price_unit": l.price_unit, "discount": l.discount,
                "subtotal": l.price_subtotal, "qty_delivered": l.qty_delivered,
                "group_id": l.fulfillment_group_id.id or None,
            } for l in o.line_ids.filtered(lambda l: not l.is_delivery)]
            data["groups"] = [{
                "id": g.id, "name": g.name, "fulfillment_type": g.fulfillment_type,
                "state": g.state, "tracking_ref": g.tracking_ref or None,
                "carrier": g.carrier_id.name or None,
                "delivered_date": _iso(g.delivered_date),
            } for g in o.fulfillment_group_ids]
        return data

    @staticmethod
    def _return_json(r):
        return {
            "id": r.id, "name": r.name, "state": r.state, "order_id": r.vendor_order_id.id,
            "order": r.vendor_order_id.name, "customer": r.partner_id.display_name,
            "reason": r.reason, "vendor_note": r.vendor_note or None,
            "request_date": _iso(r.request_date), "vendor_deadline": _iso(r.vendor_deadline),
            "currency": r.currency_id.name, "refund_amount": r.refund_amount,
            "lines": [{"line_id": l.vendor_line_id.id, "product": l.product_id.display_name,
                       "quantity": l.quantity} for l in r.line_ids],
        }

    @staticmethod
    def _tx_json(t):
        return {
            "id": t.id, "type": t.type, "state": t.state, "amount": t.amount,
            "currency": t.currency_id.name, "available_date": str(t.available_date or "") or None,
            "reference": t.reference or None, "order_id": t.vendor_order_id.id or None,
            "settlement_id": t.settlement_id.id or None, "created": _iso(t.create_date),
        }

    @staticmethod
    def _settlement_json(s):
        return {
            "id": s.id, "name": s.name, "state": s.state, "period_from": str(s.period_from),
            "period_to": str(s.period_to), "currency": s.currency_id.name,
            "gross_amount": s.gross_amount, "deductions": s.deductions, "net_amount": s.net_amount,
        }

    # ------------------------------------------------------------------ offers
    @http.route("/api/v1/vendor/offers", type="http", auth="public", methods=["GET"],
                csrf=False, save_session=False)
    def offers(self, **kw):
        return self._run("read", self._offers_list)

    def _offers_list(self, ctx, body):
        domain = [("vendor_id", "=", ctx.vendor.id)]
        state = self._choice_filter("state", ("draft", "pending", "active", "rejected", "suspended"))
        if state:
            domain.append(("state", "=", state))
        ftype = self._choice_filter("fulfillment_type", ("fbm", "fbl"))
        if ftype:
            domain.append(("fulfillment_type", "=", ftype))
        sku = request.httprequest.args.get("seller_sku")
        if sku:
            domain.append(("seller_sku", "=", sku))
        records, meta = self._paginate(request.env["leaders.marketplace.offer"].sudo(), domain, "id desc")
        return 200, dict(meta, items=[self._offer_json(o) for o in records])

    @http.route("/api/v1/vendor/offers/<int:offer_id>", type="http", auth="public",
                methods=["GET"], csrf=False, save_session=False)
    def offer_get(self, offer_id, **kw):
        return self._run("read", self._offer_get, offer_id)

    def _offer_get(self, ctx, body, offer_id):
        return 200, self._offer_json(self._owned("leaders.marketplace.offer", offer_id, ctx.vendor))

    @http.route("/api/v1/vendor/offers/<int:offer_id>", type="http", auth="public",
                methods=["PATCH"], csrf=False, save_session=False)
    def offer_patch(self, offer_id, **kw):
        return self._run("write", self._offer_patch, offer_id)

    def _offer_patch(self, ctx, body, offer_id):
        self._require_approved(ctx.vendor)
        offer = self._owned("leaders.marketplace.offer", offer_id, ctx.vendor)
        v = Validator()
        v.unknown(body, ("price", "discount", "stock", "submit"))
        if not any(k in body for k in ("price", "discount", "stock", "submit")):
            v.add("body", _("Send at least one of: price, discount, stock, submit."))
        price = v.number(body, "price", minimum=0)
        stock = v.number(body, "stock", minimum=0)
        submit = body.get("submit")
        if "submit" in body and not isinstance(submit, bool):
            v.add("submit", _("Must be true or false."))
        discount = clear = None
        if "discount" in body:
            raw = body["discount"]
            if raw is None:
                clear = True
            elif not isinstance(raw, dict):
                v.add("discount", _("Must be an object, or null to remove the discount."))
            else:
                sub = Validator()
                sub.unknown(raw, ("type", "percent", "price", "start", "end"))
                kind = raw.get("type")
                if kind not in ("percent", "price"):
                    sub.add("type", _("Must be percent or price."))
                percent = sub.number(raw, "percent", minimum=0, maximum=100) if kind == "percent" else None
                sale = sub.number(raw, "price", minimum=0) if kind == "price" else None
                if kind == "percent" and percent is None and not sub.details:
                    sub.add("percent", _("This field is required."))
                if kind == "price" and sale is None and not sub.details:
                    sub.add("price", _("This field is required."))
                start, end = sub.datetime(raw, "start"), sub.datetime(raw, "end")
                for d in sub.details:
                    v.add("discount.%s" % d["field"], d["message"])
                discount = {"type": kind, "percent": percent, "price": sale, "start": start, "end": end}
        v.check()
        if price is not None:
            offer._leaders_vendor_set_price(price)
        if stock is not None:
            offer._leaders_set_fbm_stock(stock)
        if clear:
            offer._leaders_clear_discount()
        elif discount:
            offer._leaders_set_discount(discount)
        if submit:
            offer._leaders_vendor_submit(ctx.vendor)
        offer.invalidate_recordset()
        return 200, self._offer_json(offer)

    # ------------------------------------------------------------------ orders
    @staticmethod
    def orders_list_response():
        """GET /api/v1/vendor/orders with a marketplace key (called by the legacy route)."""
        api = VendorAPI()
        return api._run("read", api._orders_list)

    def _orders_list(self, ctx, body):
        domain = [("vendor_id", "=", ctx.vendor.id)]
        Order = request.env["leaders.vendor.order"].sudo()
        states = [s[0] for s in Order._fields["state"].selection]
        state = self._choice_filter("state", states)
        if state:
            domain.append(("state", "=", state))
        since = request.httprequest.args.get("updated_since")
        if since:
            try:
                domain.append(("write_date", ">=", _parse_datetime(since, "updated_since")))
            except ValueError:
                raise ApiError(422, "validation_error", _("The request is not valid."), [{
                    "field": "updated_since", "message": _("Must be an ISO 8601 date and time.")}])
        records, meta = self._paginate(Order, domain, "id desc")
        return 200, dict(meta, items=[self._order_json(o) for o in records])

    @http.route("/api/v1/vendor/orders/<int:order_id>", type="http", auth="public",
                methods=["GET"], csrf=False, save_session=False)
    def order_get(self, order_id, **kw):
        return self._run("read", self._order_get, order_id)

    def _order_get(self, ctx, body, order_id):
        order = self._owned("leaders.vendor.order", order_id, ctx.vendor)
        return 200, self._order_json(order, detail=True)

    @http.route("/api/v1/vendor/orders/<int:order_id>/ship", type="http", auth="public",
                methods=["POST"], csrf=False, save_session=False)
    def order_ship(self, order_id, **kw):
        return self._run("write", self._order_ship, order_id)

    def _ship_spec(self, spec, prefix, order):
        """Validate one shipment description; returns (group, tracking, carrier, quantities)."""
        v = Validator()
        v.unknown(spec, ("group_id", "tracking_ref", "carrier_id", "lines"))
        group_id = v.integer(spec, "group_id")
        tracking = v.string(spec, "tracking_ref", required=True, max_len=200)
        carrier_id = v.integer(spec, "carrier_id")
        lines = spec.get("lines")
        quantities = None
        if lines is not None:
            if not isinstance(lines, list) or not lines:
                v.add("lines", _("Must be a non-empty list of {line_id, quantity}."))
            else:
                quantities = {}
                for i, item in enumerate(lines):
                    sub = Validator()
                    if not isinstance(item, dict):
                        v.add("lines[%s]" % i, _("Must be an object."))
                        continue
                    sub.unknown(item, ("line_id", "quantity"))
                    line_id = sub.integer(item, "line_id", required=True)
                    qty = sub.number(item, "quantity", minimum=0, required=True)
                    for d in sub.details:
                        v.add("lines[%s].%s" % (i, d["field"]), d["message"])
                    if line_id is not None and qty is not None:
                        if line_id in quantities:
                            v.add("lines[%s].line_id" % i, _("Duplicated line."))
                        quantities[line_id] = qty
        for d in v.details:
            d["field"] = prefix + d["field"]
        if v.details:
            raise ApiError(422, "validation_error", _("The request is not valid."), v.details)
        fbm_groups = order.fulfillment_group_ids.filtered(
            lambda g: g.fulfillment_type == "fbm" and g.state in SHIPPABLE_STATES)
        if group_id:
            group = order.fulfillment_group_ids.filtered(lambda g: g.id == group_id)
            if not group:
                raise ApiError(404, "not_found", _("Not found."), [{"field": prefix + "group_id",
                               "message": _("This shipment does not belong to the order.")}])
        elif not fbm_groups:
            raise UserError(_("There is nothing left to ship on this order."))
        elif len(fbm_groups) == 1:
            group = fbm_groups
        else:
            raise ApiError(422, "validation_error", _("The request is not valid."), [{
                "field": prefix + "group_id",
                "message": _("Required when the order has several open shipments.")}])
        if group.fulfillment_type != "fbm":
            raise UserError(_("FBL shipments are handled by the Leaders warehouse."))
        if group.state not in SHIPPABLE_STATES:
            raise UserError(_("This shipment cannot be shipped in state %s.", group.state))
        carrier = False
        if carrier_id:
            carrier = request.env["delivery.carrier"].sudo().browse(carrier_id).exists()
            if not carrier:
                raise ApiError(422, "validation_error", _("The request is not valid."), [{
                    "field": prefix + "carrier_id", "message": _("Unknown carrier.")}])
        return group, tracking, carrier, quantities

    def _order_ship(self, ctx, body, order_id):
        order = self._owned("leaders.vendor.order", order_id, ctx.vendor)
        v = Validator()
        if "groups" in body:
            v.unknown(body, ("groups",))
            v.check()
            specs = body["groups"]
            if not isinstance(specs, list) or not specs or not all(isinstance(s, dict) for s in specs):
                raise ApiError(422, "validation_error", _("The request is not valid."), [{
                    "field": "groups", "message": _("Must be a non-empty list of shipments.")}])
            prefixed = [(s, "groups[%s]." % i) for i, s in enumerate(specs)]
        else:
            prefixed = [(body, "")]
        parsed = [self._ship_spec(spec, prefix, order) for spec, prefix in prefixed]
        seen = [p[0].id for p in parsed]
        if len(seen) != len(set(seen)):
            raise ApiError(422, "validation_error", _("The request is not valid."), [{
                "field": "groups", "message": _("A shipment is listed twice.")}])
        for group, tracking, carrier, quantities in parsed:
            # Same steps as the portal route.
            vals = {"carrier_id": carrier.id if carrier else False}
            if not group.picking_ids.filtered(lambda p: p.state == "done"):
                vals["tracking_ref"] = tracking
            group.sudo().write(vals)
            group.sudo().action_ship(quantities=quantities, tracking_ref=tracking)
        order.invalidate_recordset()
        return 200, self._order_json(order, detail=True)

    @http.route("/api/v1/vendor/orders/<int:order_id>/delivered", type="http", auth="public",
                methods=["POST"], csrf=False, save_session=False)
    def order_delivered(self, order_id, **kw):
        return self._run("write", self._order_delivered, order_id)

    def _order_delivered(self, ctx, body, order_id):
        order = self._owned("leaders.vendor.order", order_id, ctx.vendor)
        v = Validator()
        v.unknown(body, ("group_id",))
        group_id = v.integer(body, "group_id")
        v.check()
        groups = order.fulfillment_group_ids.filtered(lambda g: g.fulfillment_type == "fbm")
        if group_id:
            groups = groups.filtered(lambda g: g.id == group_id)
            if not groups:
                raise ApiError(404, "not_found", _("Not found."))
        else:
            groups = groups.filtered(lambda g: g.state == "shipped")
            if not groups:
                raise UserError(_("Only shipped groups can be reported as delivered."))
        # Completes the delivery in "vendor" mode, or waits for the customer in
        # "customer_confirm" mode -- decided by the model, as in the portal.
        groups.sudo().action_report_delivery(reported_by=ctx.user)
        order.invalidate_recordset()
        return 200, self._order_json(order, detail=True)

    # ------------------------------------------------------------------ returns
    @http.route("/api/v1/vendor/returns", type="http", auth="public", methods=["GET"],
                csrf=False, save_session=False)
    def returns(self, **kw):
        return self._run("read", self._returns_list)

    def _returns_list(self, ctx, body):
        Return = request.env["leaders.vendor.return"].sudo()
        domain = [("vendor_id", "=", ctx.vendor.id)]
        state = self._choice_filter("state", [s[0] for s in Return._fields["state"].selection])
        if state:
            domain.append(("state", "=", state))
        records, meta = self._paginate(Return, domain, "id desc")
        return 200, dict(meta, items=[self._return_json(r) for r in records])

    @http.route("/api/v1/vendor/returns/<int:return_id>", type="http", auth="public",
                methods=["GET"], csrf=False, save_session=False)
    def return_get(self, return_id, **kw):
        return self._run("read", self._return_get, return_id)

    def _return_get(self, ctx, body, return_id):
        return 200, self._return_json(self._owned("leaders.vendor.return", return_id, ctx.vendor))

    @http.route("/api/v1/vendor/returns/<int:return_id>/decision", type="http", auth="public",
                methods=["POST"], csrf=False, save_session=False)
    def return_decision(self, return_id, **kw):
        return self._run("write", self._return_decision, return_id)

    def _return_decision(self, ctx, body, return_id):
        record = self._owned("leaders.vendor.return", return_id, ctx.vendor)
        v = Validator()
        v.unknown(body, ("decision", "note"))
        decision = body.get("decision")
        if decision not in ("approve", "reject"):
            v.add("decision", _("Must be approve or reject."))
        note = v.string(body, "note", max_len=2000)
        v.check()
        if decision == "approve":
            record.action_vendor_approve(note=note or None)
        else:
            record.action_vendor_reject(note=note or None)
        record.invalidate_recordset()
        return 200, self._return_json(record)

    # ------------------------------------------------------------------ finance
    @http.route("/api/v1/vendor/wallet", type="http", auth="public", methods=["GET"],
                csrf=False, save_session=False)
    def wallet(self, **kw):
        return self._run("read", self._wallet)

    def _wallet(self, ctx, body):
        vendor = ctx.vendor.sudo()
        Tx = request.env["leaders.wallet.transaction"].sudo()
        domain = [("vendor_id", "=", vendor.id)]
        state = self._choice_filter("state", [s[0] for s in Tx._fields["state"].selection])
        if state:
            domain.append(("state", "=", state))
        ttype = self._choice_filter("type", [s[0] for s in Tx._fields["type"].selection])
        if ttype:
            domain.append(("type", "=", ttype))
        records, meta = self._paginate(Tx, domain, "id desc")
        currency = vendor.company_currency_id or request.env.company.currency_id
        return 200, dict(meta, balances={
            "available": vendor.vendor_wallet_balance, "hold": vendor.vendor_wallet_hold,
            "currency": currency.name,
        }, items=[self._tx_json(t) for t in records])

    @http.route("/api/v1/vendor/settlements", type="http", auth="public", methods=["GET"],
                csrf=False, save_session=False)
    def settlements(self, **kw):
        return self._run("read", self._settlements)

    def _settlements(self, ctx, body):
        Settlement = request.env["leaders.settlement"].sudo()
        domain = [("vendor_id", "=", ctx.vendor.id)]
        state = self._choice_filter("state", [s[0] for s in Settlement._fields["state"].selection])
        if state:
            domain.append(("state", "=", state))
        records, meta = self._paginate(Settlement, domain, "id desc")
        return 200, dict(meta, items=[self._settlement_json(s) for s in records])

    # ------------------------------------------------------------------ OpenAPI
    @http.route("/api/v1/openapi.json", type="http", auth="public", methods=["GET"],
                csrf=False, save_session=False)
    def openapi(self, **kw):
        if not request.env["leaders.vendor.api.key"]._leaders_api_enabled():
            return request.make_json_response(
                {"error": "not_found", "message": "Not found.", "details": []}, status=404)
        return request.make_json_response(
            openapi_document(request.httprequest.host_url.rstrip("/")))


# ---------------------------------------------------------------------------
# OpenAPI 3 document
# ---------------------------------------------------------------------------
def _ref(name):
    return {"$ref": "#/components/schemas/%s" % name}


def _param(name, schema, description, where="query", required=False):
    return {"name": name, "in": where, "required": required, "description": description, "schema": schema}


PAGE_PARAMS = [
    _param("page", {"type": "integer", "minimum": 1, "default": 1}, "Page number."),
    _param("limit", {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
           "Items per page (max %s)." % MAX_LIMIT),
]
ID = lambda name: _param(name, {"type": "integer"}, "Record id.", "path", True)  # noqa: E731
IDEMPOTENCY = _param("Idempotency-Key", {"type": "string", "maxLength": 255},
                     "Same key and same request replays the stored response (Idempotent-Replay: true); "
                     "the same key with another request is refused (422). Kept 24 hours.", "header")
ERRORS = {
    "401": {"description": "Missing, invalid or revoked key.", "content": {"application/json": {"schema": _ref("Error")}}},
    "403": {"description": "Key scope, vendor state or terms not accepted.", "content": {"application/json": {"schema": _ref("Error")}}},
    "429": {"description": "Rate limit exceeded (see Retry-After).", "content": {"application/json": {"schema": _ref("Error")}}},
}


def _paged(item):
    return {"allOf": [_ref("Page"), {"type": "object", "properties": {
        "items": {"type": "array", "items": _ref(item)}}}]}


def _op(summary, tag, scope, params=(), body=None, ok=None, write=False, extra_errors=()):
    responses = {"200": {"description": "OK", "content": {"application/json": {"schema": ok or {"type": "object"}}}}}
    responses.update(ERRORS)
    responses["404"] = {"description": "Unknown id, or the record belongs to another vendor.",
                        "content": {"application/json": {"schema": _ref("Error")}}}
    responses["422"] = {"description": "Validation error or refused by a business rule.",
                        "content": {"application/json": {"schema": _ref("Error")}}}
    op = {"summary": summary, "tags": [tag], "security": [{"bearerAuth": []}],
          "x-scope": scope, "parameters": list(params) + ([IDEMPOTENCY] if write else []),
          "responses": responses}
    if body:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return op


def openapi_document(base_url=""):
    paths = {
        "/api/v1/vendor/offers": {"get": _op(
            "List your offers", "Offers", "read",
            PAGE_PARAMS + [
                _param("state", {"type": "string", "enum": ["draft", "pending", "active", "rejected", "suspended"]}, "Filter."),
                _param("fulfillment_type", {"type": "string", "enum": ["fbm", "fbl"]}, "Filter."),
                _param("seller_sku", {"type": "string"}, "Exact seller SKU.")], ok=_paged("Offer"))},
        "/api/v1/vendor/offers/{offer_id}": {
            "get": _op("Get one offer", "Offers", "read", [ID("offer_id")], ok=_ref("Offer")),
            "patch": _op("Change price, discount and/or FBM stock", "Offers", "write", [ID("offer_id")],
                         body=_ref("OfferPatch"), ok=_ref("Offer"), write=True)},
        "/api/v1/vendor/orders": {"get": _op(
            "List your orders", "Orders", "read",
            PAGE_PARAMS + [
                _param("state", {"type": "string"}, "Order state."),
                _param("updated_since", {"type": "string", "format": "date-time"}, "Only orders changed since.")],
            ok=_paged("Order"))},
        "/api/v1/vendor/orders/{order_id}": {"get": _op(
            "Get one order with lines and shipments", "Orders", "read", [ID("order_id")], ok=_ref("OrderDetail"))},
        "/api/v1/vendor/orders/{order_id}/ship": {"post": _op(
            "Ship (all or part of) an FBM shipment", "Orders", "write", [ID("order_id")],
            body=_ref("ShipRequest"), ok=_ref("OrderDetail"), write=True)},
        "/api/v1/vendor/orders/{order_id}/delivered": {"post": _op(
            "Report FBM shipments as delivered (customer confirmation mode is respected)", "Orders", "write",
            [ID("order_id")], body=_ref("DeliveredRequest"), ok=_ref("OrderDetail"), write=True)},
        "/api/v1/vendor/returns": {"get": _op(
            "List your return requests", "Returns", "read",
            PAGE_PARAMS + [_param("state", {"type": "string"}, "Return state.")], ok=_paged("Return"))},
        "/api/v1/vendor/returns/{return_id}": {"get": _op(
            "Get one return request", "Returns", "read", [ID("return_id")], ok=_ref("Return"))},
        "/api/v1/vendor/returns/{return_id}/decision": {"post": _op(
            "Approve or reject a return request", "Returns", "write", [ID("return_id")],
            body=_ref("ReturnDecision"), ok=_ref("Return"), write=True)},
        "/api/v1/vendor/wallet": {"get": _op(
            "Wallet balances and transactions", "Finance", "read",
            PAGE_PARAMS + [_param("state", {"type": "string", "enum": ["hold", "available", "settled"]}, "Filter."),
                           _param("type", {"type": "string"}, "Transaction type.")], ok=_ref("Wallet"))},
        "/api/v1/vendor/settlements": {"get": _op(
            "List your settlements", "Finance", "read",
            PAGE_PARAMS + [_param("state", {"type": "string", "enum": ["draft", "approved", "paid", "cancelled"]}, "Filter.")],
            ok=_paged("Settlement"))},
        "/api/v1/openapi.json": {"get": {
            "summary": "This document", "tags": ["Meta"], "security": [],
            "responses": {"200": {"description": "OpenAPI 3 document"}}}},
    }
    num = {"type": "number"}
    schemas = {
        "Error": {"type": "object", "required": ["error", "message", "details"], "properties": {
            "error": {"type": "string", "example": "validation_error"},
            "message": {"type": "string"},
            "details": {"type": "array", "items": {"type": "object", "properties": {
                "field": {"type": "string"}, "message": {"type": "string"}}}}}},
        "Page": {"type": "object", "properties": {
            "page": {"type": "integer"}, "limit": {"type": "integer"}, "total": {"type": "integer"},
            "next": {"type": "string", "nullable": True, "description": "URL of the next page, null on the last."}}},
        "Discount": {"type": "object", "properties": {
            "state": {"type": "string"}, "type": {"type": "string", "nullable": True},
            "percent": num, "price": num,
            "start": {"type": "string", "format": "date-time", "nullable": True},
            "end": {"type": "string", "format": "date-time", "nullable": True}}},
        "Offer": {"type": "object", "properties": {
            "id": {"type": "integer"}, "seller_sku": {"type": "string"}, "name": {"type": "string"},
            "product_id": {"type": "integer"}, "price": num, "effective_price": num,
            "currency": {"type": "string"}, "fulfillment_type": {"type": "string", "enum": ["fbm", "fbl"]},
            "state": {"type": "string"}, "active": {"type": "boolean"},
            "reject_reason": {"type": "string", "nullable": True}, "stock_available": num,
            "discount": _ref("Discount")}},
        "OfferPatch": {"type": "object", "additionalProperties": False, "minProperties": 1, "properties": {
            "price": {"type": "number", "minimum": 0,
                      "description": "New regular price. Like the portal edit, a changed price sends the offer back to draft for review."},
            "stock": {"type": "number", "minimum": 0, "description": "Set the FBM quantity (FBL stock is refused)."},
            "discount": {"type": "object", "nullable": True, "description": "null removes the discount.",
                         "required": ["type"], "properties": {
                             "type": {"type": "string", "enum": ["percent", "price"]},
                             "percent": {"type": "number", "minimum": 0, "maximum": 100},
                             "price": {"type": "number", "minimum": 0},
                             "start": {"type": "string", "format": "date-time"},
                             "end": {"type": "string", "format": "date-time"}}},
            "submit": {"type": "boolean", "description": "Send the offer for review after the change."}}},
        "Order": {"type": "object", "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"}, "state": {"type": "string"},
            "date_order": {"type": "string", "format": "date-time"}, "currency": {"type": "string"},
            "amount_untaxed": num, "amount_tax": num, "amount_total": num, "commission_amount": num,
            "vendor_net": num, "customer": {"type": "string"},
            "date_shipped": {"type": "string", "format": "date-time", "nullable": True},
            "ship_by_date": {"type": "string", "format": "date-time", "nullable": True},
            "is_late": {"type": "boolean"}, "updated": {"type": "string", "format": "date-time"}}},
        "OrderDetail": {"allOf": [_ref("Order"), {"type": "object", "properties": {
            "lines": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "integer"}, "name": {"type": "string"}, "product_id": {"type": "integer"},
                "quantity": num, "price_unit": num, "discount": num, "subtotal": num,
                "qty_delivered": num, "group_id": {"type": "integer", "nullable": True}}}},
            "groups": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "integer"}, "name": {"type": "string"},
                "fulfillment_type": {"type": "string"}, "state": {"type": "string"},
                "tracking_ref": {"type": "string", "nullable": True},
                "carrier": {"type": "string", "nullable": True},
                "delivered_date": {"type": "string", "format": "date-time", "nullable": True}}}}}}]},
        "Shipment": {"type": "object", "required": ["tracking_ref"], "additionalProperties": False, "properties": {
            "group_id": {"type": "integer", "description": "Required when several shipments are open."},
            "tracking_ref": {"type": "string", "maxLength": 200},
            "carrier_id": {"type": "integer"},
            "lines": {"type": "array", "description": "Omit to ship everything that is left.",
                      "items": {"type": "object", "required": ["line_id", "quantity"], "properties": {
                          "line_id": {"type": "integer"}, "quantity": {"type": "number", "minimum": 0}}}}}},
        "ShipRequest": {"oneOf": [_ref("Shipment"), {"type": "object", "required": ["groups"],
                                                       "additionalProperties": False, "properties": {
                                                           "groups": {"type": "array", "minItems": 1, "items": _ref("Shipment")}}}]},
        "DeliveredRequest": {"type": "object", "additionalProperties": False, "properties": {
            "group_id": {"type": "integer", "description": "Default: every shipped FBM shipment of the order."}}},
        "Return": {"type": "object", "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"}, "state": {"type": "string"},
            "order_id": {"type": "integer"}, "order": {"type": "string"}, "customer": {"type": "string"},
            "reason": {"type": "string"}, "vendor_note": {"type": "string", "nullable": True},
            "request_date": {"type": "string", "format": "date-time"},
            "vendor_deadline": {"type": "string", "format": "date-time", "nullable": True},
            "currency": {"type": "string"}, "refund_amount": num,
            "lines": {"type": "array", "items": {"type": "object", "properties": {
                "line_id": {"type": "integer"}, "product": {"type": "string"}, "quantity": num}}}}},
        "ReturnDecision": {"type": "object", "required": ["decision"], "additionalProperties": False, "properties": {
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "note": {"type": "string", "maxLength": 2000, "description": "Required when rejecting."}}},
        "Transaction": {"type": "object", "properties": {
            "id": {"type": "integer"}, "type": {"type": "string"}, "state": {"type": "string"},
            "amount": num, "currency": {"type": "string"},
            "available_date": {"type": "string", "format": "date", "nullable": True},
            "reference": {"type": "string", "nullable": True},
            "order_id": {"type": "integer", "nullable": True},
            "settlement_id": {"type": "integer", "nullable": True},
            "created": {"type": "string", "format": "date-time"}}},
        "Wallet": {"allOf": [_ref("Page"), {"type": "object", "properties": {
            "balances": {"type": "object", "properties": {
                "available": num, "hold": num, "currency": {"type": "string"}}},
            "items": {"type": "array", "items": _ref("Transaction")}}}]},
        "Settlement": {"type": "object", "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"}, "state": {"type": "string"},
            "period_from": {"type": "string", "format": "date"}, "period_to": {"type": "string", "format": "date"},
            "currency": {"type": "string"}, "gross_amount": num, "deductions": num, "net_amount": num}},
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Leaders Marketplace Vendor API", "version": "1",
            "description": "Manage offers, ship orders, answer returns and read finance with a vendor "
                           "API key created at /my/vendor/api. Every call is scoped to the vendor of "
                           "the key. Keys are limited per minute (429 + Retry-After). POST and PATCH "
                           "accept an Idempotency-Key header. Errors: {error, message, details}.",
        },
        "servers": [{"url": base_url or "/"}],
        "components": {
            "securitySchemes": {"bearerAuth": {
                "type": "http", "scheme": "bearer",
                "description": "Authorization: Bearer lmk_XXXXXXXX_... (read keys can only GET)."}},
            "schemas": schemas,
        },
        "paths": paths,
    }
