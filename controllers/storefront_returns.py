"""Storefront returns: a spare part that does not match the order is returned before it is installed.

Heavy equipment and rentals are not returned on the storefront. A return request is cut per seller
(one ``leaders.vendor.return`` per vendor order) and follows the usual flow: seller, then platform.
"""
import base64
import binascii

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from ..models.returns import MAX_RETURN_PHOTOS
from ..tools.auth_errors import AuthApiError, json_response, validation_error
from .storefront_common import bearer_user, json_body, rate_limit, storefront_api


def _reason_card(reason):
    return {"id": str(reason.id), "label": reason.name, "requiresPhoto": bool(reason.requires_photo)}


class StorefrontReturns(http.Controller):

    def _order(self, reference, user):
        order = request.env["sale.order"].sudo().search([
            ("leaders_public_ref", "=", reference), ("partner_id", "=", user.partner_id.commercial_partner_id.id)],
            limit=1)
        if not order:
            raise AuthApiError(404, "not_found", "Order not found.")
        return order

    @http.route("/returns/reasons", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False,
                save_session=False)
    @storefront_api(["GET"])
    def reasons(self, cors, **kw):
        rate_limit("sf_orders")
        domain = [("leaders_parts_allowed", "=", True)]  # the storefront returns spare parts only
        reasons = request.env["leaders.return.reason"].sudo().search(domain, order="sequence, id")
        return json_response([_reason_card(r) for r in reasons], 200, cors)

    @http.route("/orders/<string:reference>/returns", type="http", auth="public", methods=["GET", "POST", "OPTIONS"],
                csrf=False, save_session=False)
    @storefront_api(["GET", "POST"])
    def returns(self, cors, reference, **kw):
        rate_limit("sf_orders")
        user = bearer_user(required=True)
        order = self._order(reference, user)
        Return = request.env["leaders.vendor.return"].sudo()
        if request.httprequest.method == "GET":
            records = Return.search([("vendor_order_id", "in", order.vendor_order_ids.ids)], order="id desc")
            return json_response([self._render(r) for r in records], 200, cors)
        created = self._create(order, json_body())
        return json_response({"returns": [self._render(r) for r in created]}, 201, cors)

    def _render(self, ret):
        out = {"reference": ret.name, "status": ret.state, "createdAt": ret.request_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "items": [{"productId": str(l.product_id.product_tmpl_id.id), "quantity": int(l.quantity)}
                         for l in ret.line_ids]}
        if ret.reason_id:
            out["reason"] = _reason_card(ret.reason_id)
        return out

    def _create(self, order, body):
        errors = {}
        reason = request.env["leaders.return.reason"].sudo().search(
            [("id", "=", int(body["reasonId"]) if str(body.get("reasonId") or "").isdigit() else 0),
             ("leaders_parts_allowed", "=", True)], limit=1)
        if not reason:
            errors["reasonId"] = "A spare part is returned only when it does not match the order."
        if body.get("notInstalled") is not True:
            errors["notInstalled"] = "Confirm that the part was not installed."
        comment = (body.get("comment") or "").strip()[:1000] if isinstance(body.get("comment"), str) else ""
        photos = self._photos(body.get("photos"), errors)
        if reason and reason.requires_photo and not photos:
            errors["photos"] = "Add at least one photo of the part."
        items = body.get("items") if isinstance(body.get("items"), list) else []
        if not items:
            errors["items"] = "Select at least one product to return."
        wanted = {}
        for index, item in enumerate(items):
            item = item if isinstance(item, dict) else {}
            pid, qty = str(item.get("productId") or ""), item.get("quantity")
            if not pid.isdigit() or isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
                errors["items.%s" % index] = "A product id and a quantity of at least 1 are required."
                continue
            tmpl = request.env["product.template"].sudo().browse(int(pid)).exists()
            if not tmpl or tmpl.leaders_kind != "part":
                errors["items.%s" % index] = "Only spare parts can be returned."
                continue
            wanted[tmpl.id] = wanted.get(tmpl.id, 0) + qty
        if errors:
            raise validation_error(errors)
        # one request per seller: the vendor order lines that hold the products
        per_order = {}
        for tmpl_id, qty in wanted.items():
            lines = order.vendor_order_ids.line_ids.filtered(lambda l: l.product_id.product_tmpl_id.id == tmpl_id)
            if not lines:
                raise validation_error({"items": "This product is not in the order."})
            line = lines[:1]
            per_order.setdefault(line.order_id, []).append((0, 0, {"vendor_line_id": line.id, "quantity": qty}))
        Return = request.env["leaders.vendor.return"].sudo()
        created = Return
        try:
            for vendor_order, lines in per_order.items():
                created |= Return.create({
                    "vendor_order_id": vendor_order.id, "line_ids": lines, "reason_id": reason.id,
                    "reason": "%s: %s" % (reason.name, comment) if comment else reason.name,
                    "photo_ids": [(0, 0, dict(p)) for p in photos], "leaders_not_installed": True,
                })
        except (UserError, ValidationError) as error:  # window passed, not shipped yet, more than delivered...
            raise validation_error({"items": str(error.args[0] if error.args else error)})
        return created

    @staticmethod
    def _photos(raw, errors):
        raw = raw if isinstance(raw, list) else []
        if len(raw) > MAX_RETURN_PHOTOS:
            errors["photos"] = "You can attach at most %s photos." % MAX_RETURN_PHOTOS
            return []
        photos = []
        for index, photo in enumerate(raw):
            data = photo.get("data") if isinstance(photo, dict) else None
            try:
                content = base64.b64decode(data or "", validate=True)
            except (binascii.Error, ValueError, TypeError):
                content = b""
            if not content:
                errors["photos.%s" % index] = "Send the photo as base64 image data."
                continue
            name = str(photo.get("name") or "photo")[:100]
            photos.append({"name": name, "datas": base64.b64encode(content)})
        return photos
