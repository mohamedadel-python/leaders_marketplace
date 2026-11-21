"""Equipment requests of the storefront (project guide, feature 4): equipment is not added to the cart.

``POST /equipment-requests`` buys (priced, with or without services), asks a quote (no price) or books a rental;
``GET /equipment-requests/<reference>`` follows it (status, what to pay and the payment link).
"""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import date, timedelta

from odoo import SUPERUSER_ID, fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ..models.equipment_request import EQUIPMENT_SERVICES
from ..services.storefront import Storefront, minor
from ..services.storefront_cart import PERIOD_DAYS
from ..tools.auth_errors import AuthApiError, json_response, validation_error
from .auth_api import CORS_PARAM
from .storefront_common import bearer_user, json_body, rate_limit, storefront_api

STATUS = {"pending": "requested", "offered": "awaitingPayment", "inProgress": "confirmed", "executing": "inProgress",
          "reviewing": "requested",
          "contacting": "requested", "completed": "completed", "cancelled": "cancelled"}


def _gov_key(state):
    return re.sub(r"\W", "", (state.with_context(lang="en_US").name or "").lower())


class StorefrontEquipment(http.Controller):

    def _base(self):
        return request.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")

    def _render(self, rec):
        out = {"reference": rec.name, "kind": rec.kind, "status": STATUS.get(rec.state, rec.state),
               "productId": str(rec.product_tmpl_id.id), "quantity": rec.quantity,
               "services": [s for s in (rec.equipment_services or "").split(",") if s],
               "placedAt": rec.create_date.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if rec.unit_price:
            out["unitPrice"] = minor(rec.unit_price)
        if rec.services_amount:
            out["servicesPrice"] = minor(rec.services_amount)
        order = rec.payment_order_id
        if order and order.state in ("draft", "sent"):
            out["amountDue"] = minor(order.amount_total)
            out["paymentUrl"] = "%s/orders/%s/payment" % (self._base(), order.leaders_public_ref)
        if rec.kind == "quote" and rec.quote_order_id and rec.leaders_offer_open():
            from .storefront_quotes import StorefrontQuotes  # the offer of an equipment quote, as /quotes/<ref> shows it
            out["offer"] = StorefrontQuotes._offer(rec)
            out["total"] = out["offer"]["total"]
            if rec.state == "offered":
                out["amountDue"] = out["offer"]["total"]  # paid in cash on delivery: POST .../payment {"method": "cash"}
        if rec.kind == "rent" and rec.rent_delivery_fee and rec.state == "offered":
            out["amountDue"] = minor(rec.rent_delivery_fee)  # paid in cash on delivery: no payment link
        if rec.rental_id:
            out["rental"] = {"from": str(rec.rental_id.date_start), "to": str(rec.rental_id.date_end),
                             "pricePerDay": minor(rec.rental_id.customer_price_day)}
        if rec.sf_payment_method == "cash":
            out["paymentMethod"] = "cash"
        elif order and any(t.state in ("done", "authorized") for t in order.sudo().transaction_ids):
            out["paymentMethod"] = "online"
        details = self._details(rec)
        booking = details.pop("rental", None)
        out.update(details)
        if booking and "rental" in out:  # the booked period joins the rental dates
            out["rental"].update(booking)
        return out

    def _details(self, rec):
        """What the track page shows besides the stages: the machine, the services, the delivery and the money."""
        from .vendor_apply import GOVERNORATES_AR
        rec = rec.sudo()
        arabic = (request.env.lang or "").startswith("ar")
        out = {"currency": rec.currency_id.name or request.env.company.currency_id.name}
        tmpl = rec.product_tmpl_id
        if tmpl:
            card = Storefront(request.env).product(tmpl, detail=False)
            out["product"] = {key: card[key] for key in ("id", "slug", "name", "brand", "model", "image") if key in card}
        # services: names in the request's language; a price per line only when a single service was priced
        slugs = [s for s in (rec.equipment_services or "").split(",") if s]
        pages = request.env["product.template"].sudo().search(
            [("leaders_service_page", "=", True), ("leaders_slug", "in", slugs)]) if slugs else []
        names = {page.leaders_slug: page.name for page in pages}
        names.update({o.slug: o.name for o in tmpl.sudo()._leaders_vendor_own_services()} if tmpl else {})
        lines = [{"slug": slug, "name": names.get(slug, slug)} for slug in slugs]
        vendor_slugs = {s for s in (rec.equipment_vendor_services or "").split(",") if s}
        if len(lines) == 1 and rec.kind == "purchase":
            amount = rec.vendor_services_amount if lines[0]["slug"] in vendor_slugs else rec.services_amount
            if amount:
                lines[0]["amount"] = minor(amount)
        out["serviceLines"] = lines
        state = rec.shipping_state_id
        out["shipping"] = {
            # requests placed before the name was kept: the signed-in person, not the company the request is filed on
            "name": rec.contact_name or (bearer_user(required=False) or rec.partner_id).name or "",
            "phone": rec.phone or "",
            "governorate": _gov_key(state) if state else "",
            "governorateName": (GOVERNORATES_AR.get(state.code) if arabic else None)
            or state.with_context(lang="en_US").name or "",
            "address": rec.shipping_address or "",
        }
        if rec.note:
            out["note"] = rec.note
        if rec.rental_id and rec.rent_period:
            out["rental"] = {"period": rec.rent_period, "units": rec.rent_units or 1}
        # money once it is known
        order = rec.payment_order_id
        if rec.kind == "rent" and rec.rental_id:
            fee = (order.amount_total if order and order.state != "cancel" else 0.0) or rec.rent_delivery_fee
            if fee:
                out["deliveryFee"] = minor(fee)
            days = max((rec.rental_id.date_end - rec.rental_id.date_start).days, 0)
            if days and rec.rental_id.customer_price_day:
                out["total"] = minor(rec.rental_id.customer_price_day * days + fee)
        elif rec.kind == "purchase" and rec.unit_price:
            platform_slugs = [s for s in slugs if s not in vendor_slugs]
            vendor_priced = not vendor_slugs or rec.vendor_services_amount
            platform_priced = not platform_slugs or rec.services_amount
            if platform_priced and vendor_priced:  # services asked: the total waits for their price
                out["total"] = minor(rec.unit_price * max(rec.quantity, 1) + (rec.services_amount or 0.0)
                                     + (rec.vendor_services_amount or 0.0))
        return out

    @http.route("/equipment-requests", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False,
                save_session=False)
    @storefront_api(["POST"])
    def create(self, cors, **kw):
        rate_limit("sf_orders")
        user = bearer_user(required=True)
        body = json_body()
        store = Storefront(request.env)
        errors = {}
        pid = str(body.get("productId") or "")
        tmpl = store.Template.search(store.base_domain() + [("id", "=", int(pid))], limit=1) if pid.isdigit() else None
        if not tmpl:
            raise AuthApiError(404, "not_found", "Product not found.")
        if tmpl.leaders_kind == "part":
            raise validation_error({"productId": "Spare parts are ordered with the cart."})
        listing = body.get("listing") or "buy"
        if listing not in ("buy", "rent"):
            errors["listing"] = "Must be buy or rent."
        services = body.get("services") if isinstance(body.get("services"), list) else []
        allowed = set(tmpl._leaders_available_services().mapped("leaders_slug")) | set(
            tmpl._leaders_vendor_own_services().mapped("slug"))
        if any(s not in allowed for s in services):  # only the services chosen on the product
            errors["services"] = "This service is not available with this product."
        quantity = body.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 99:
            errors["quantity"] = "Quantity must be between 1 and 99."
        shipping = body.get("shipping") if isinstance(body.get("shipping"), dict) else {}
        for key in ("name", "phone", "governorate"):
            if not isinstance(shipping.get(key), str) or not shipping[key].strip():
                errors["shipping.%s" % key] = "Required."
        return_url = self._return_url(body, errors)
        if errors:
            raise validation_error(errors)
        state = request.env["res.country.state"].with_context(lang="en_US").sudo().search([]).filtered(
            lambda s: _gov_key(s) == shipping["governorate"].strip().lower())[:1]
        partner = user.partner_id.commercial_partner_id
        common = {"partner_id": partner.id, "phone": shipping["phone"].strip(), "product_tmpl_id": tmpl.id,
                  "title": tmpl.display_name, "quantity": quantity, "note": (body.get("note") or "")[:2000] or False,
                  "equipment_services": ",".join(services) or False, "shipping_state_id": state.id or False,
                  "shipping_address": (shipping.get("address") or "").strip()[:200] or False,
                  "contact_name": shipping["name"].strip()[:120],
                  "return_url": return_url}
        Request = request.env["leaders.customer.request"].sudo()
        if listing == "rent":
            return self._rent(cors, store, tmpl, body, services, state, partner, common, Request)
        offer = request.env["leaders.marketplace.offer"]
        platform = body.get("offerId") == "platform"
        if platform:  # the platform's own stock, next to the vendors' offers
            if not store.platform_card(tmpl):
                raise validation_error({"offerId": "This seller does not sell this product any more."})
        elif body.get("offerId"):
            offer = store.offers(tmpl).filtered(lambda o: str(o.id) == str(body["offerId"]))
            if not offer:
                raise validation_error({"offerId": "This seller does not sell this product any more."})
        if not offer and not platform:
            store.refuse_unsold(tmpl)  # a priced machine that no vendor sells cannot be bought
        price = (offer.price + tmpl.sudo().leaders_inspection_cost) if offer else (
            store.platform_price(tmpl) if platform else tmpl.leaders_display_price)
        if price <= 0:  # no price: a quote request
            rec = Request.create(dict(common, kind="quote", offer_id=offer.id or False))  # the seller to price it
            return json_response(self._render(rec), 201, cors)
        store.stock_check(tmpl, offer, quantity, platform=platform)
        rec = Request.create(dict(common, kind="purchase", unit_price=price, offer_id=offer.id or False))
        return json_response(self._render(rec), 201, cors)

    @staticmethod
    def _return_url(body, errors):
        """Optional ``returnUrl``: a page on one of our own storefront origins (the CORS list), nothing else.
        ``{reference}`` in it is replaced by the request's reference, which the client does not know yet."""
        url = body.get("returnUrl")
        if url in (None, ""):
            return False
        allowed = {o.strip().rstrip("/") for o in (request.env["ir.config_parameter"].sudo().get_param(
            CORS_PARAM) or "").split(",") if o.strip()}
        parts = urlsplit(url) if isinstance(url, str) and len(url) <= 500 else None
        if not parts or "%s://%s" % (parts.scheme, parts.netloc) not in allowed or not parts.path.startswith("/"):
            errors["returnUrl"] = "Must be a page of the storefront."
            return False
        return url

    def _rent(self, cors, store, tmpl, body, services, state, partner, common, Request):
        if not tmpl.leaders_rentable or not tmpl.leaders_rent_price_day:
            raise validation_error({"listing": "This product cannot be rented."})
        period = body.get("rentPeriod") or "day"
        if period not in ("day", "week", "month"):
            raise validation_error({"rentPeriod": "Unknown rent period."})
        try:
            start = date.fromisoformat(body.get("startDate")) if body.get("startDate") else fields.Date.context_today(Request)
        except (TypeError, ValueError):
            raise validation_error({"startDate": "Enter a date as yyyy-mm-dd."})
        if start < fields.Date.context_today(Request):
            raise validation_error({"startDate": "The date must be today or later."})
        units = body.get("rentUnits", 1)
        if isinstance(units, bool) or not isinstance(units, int) or not 1 <= units <= 1000:
            raise validation_error({"rentUnits": "Must be a whole number of months."})
        contract = request.env["leaders.site.page"].sudo().search([("slug", "=", "rent-contract"), ("published", "=", True)],
                                                                  limit=1)
        if contract and body.get("contractAccepted") is not True:
            raise validation_error({"contractAccepted": "Accept the rental contract to book."})
        days = PERIOD_DAYS[period] * units
        rec = Request.create(dict(common, kind="rent", quantity=1, unit_price=tmpl._leaders_rent_customer_price(),
                                  rent_period=period, rent_units=units))
        try:
            with request.env.cr.savepoint():
                rental = request.env["leaders.rental"].sudo().create({
                    "product_tmpl_id": tmpl.id, "partner_id": partner.id, "state_id": state.id or False,
                    "date_start": start, "date_end": start + timedelta(days=days),
                    "supplier_price_day": tmpl.leaders_rent_price_day,
                    "customer_price_day": tmpl._leaders_rent_customer_price(),
                    "delivery_requested": "delivery" in services,
                    "contract_accepted_on": fields.Datetime.now() if contract else False,
                    "contract_version": contract.write_date.strftime("%Y-%m-%d %H:%M") if contract else False})
        except ValidationError as error:  # over two years on a used machine, already rented on these dates
            raise validation_error({"rentUnits": str(error.args[0] if error.args else error)})
        rec.rental_id = rental
        if "delivery" in services:  # the delivery is the only thing paid at booking
            zone = request.env["leaders.vendor.delivery.zone"].sudo().search(
                [("is_platform", "=", True), ("active", "=", True), ("state_id", "=", state.id)], limit=1)
            if not zone:
                raise validation_error({"shipping.governorate": "We do not deliver to this governorate."})
            rec.rent_delivery_fee = zone.price  # collected in cash: no quotation, the request carries it
        return json_response(self._render(rec), 201, cors)

    @http.route("/equipment-requests/<string:reference>", type="http", auth="public", methods=["GET", "OPTIONS"],
                csrf=False, save_session=False)
    @storefront_api(["GET"])
    def detail(self, cors, reference, **kw):
        rate_limit("sf_orders")
        user = bearer_user(required=True)
        rec = request.env["leaders.customer.request"].sudo().search([
            ("name", "=", reference), ("kind", "in", ("purchase", "rent", "quote")),
            ("partner_id", "=", user.partner_id.commercial_partner_id.id)], limit=1)
        if not rec:
            raise AuthApiError(404, "not_found", "Request not found.")
        return json_response(self._render(rec), 200, cors)

    @http.route("/equipment-requests/<string:reference>/cancel", type="http", auth="public",
                methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def cancel(self, cors, reference, **kw):
        """The customer cancels while the request is ``requested`` or ``awaitingPayment``: the unpaid payment order and
        the rental reservation are cancelled with it. Any later stage is 409 ``invalid_stage``."""
        rate_limit("sf_orders")
        user = bearer_user(required=True)
        rec = request.env["leaders.customer.request"].sudo().search([
            ("name", "=", reference), ("kind", "in", ("purchase", "rent", "quote")),
            ("partner_id", "=", user.partner_id.commercial_partner_id.id)], limit=1)
        if not rec:
            raise AuthApiError(404, "not_found", "Request not found.")
        if STATUS.get(rec.state) not in ("requested", "awaitingPayment"):
            raise AuthApiError(409, "invalid_stage", "This request can no longer be cancelled.")
        # as the superuser: the request runs as the public user, which may not read the order lines of the tracking
        rec = rec.with_user(SUPERUSER_ID).sudo()
        order = rec.payment_order_id
        if order and order.state in ("draft", "sent"):
            order._action_cancel()
        if rec.rental_id:
            rec.rental_id.action_cancel()
        rec.state = "cancelled"
        rec.env.flush_all()
        return json_response(self._render(rec), 200, cors)

    @http.route("/equipment-requests/<string:reference>/payment", type="http", auth="public",
                methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def payment(self, cors, reference, **kw):
        """``method: "cash"``: pay on delivery. Allowed while ``awaitingPayment``; the request is ``confirmed`` at once.
        The payment order stays as the record of what is collected on delivery (confirmed, cash on delivery),
        so it no longer asks for an online payment."""
        rate_limit("sf_orders")
        user = bearer_user(required=True)
        rec = request.env["leaders.customer.request"].sudo().search([
            ("name", "=", reference), ("kind", "in", ("purchase", "rent", "quote")),
            ("partner_id", "=", user.partner_id.commercial_partner_id.id)], limit=1)
        if not rec or (rec.kind == "quote" and rec.line_ids):  # a cart quote is paid with POST /quotes/<ref>/payment
            raise AuthApiError(404, "not_found", "Request not found.")
        if json_body().get("method") != "cash":
            raise validation_error({"method": "Only cash is accepted here."})
        if rec.kind == "quote" and rec.quote_order_id and rec.state in ("offered", "confirming"):
            return self._pay_quote_offer_cash(cors, rec)
        if STATUS.get(rec.state) != "awaitingPayment" or (rec.kind == "quote" and not rec.payment_order_id):
            raise AuthApiError(409, "invalid_stage", "There is nothing to pay at this stage.")
        rec = rec.with_user(SUPERUSER_ID).sudo()
        rec.sf_payment_method = "cash"
        order = rec.payment_order_id
        if order and order.state in ("draft", "sent"):
            order.write({"leaders_payment_method": "cod", "require_payment": False})
            order.action_confirm()  # confirms the request (inProgress)
        if rec.state in ("pending", "offered"):
            rec.state = "inProgress"
        rec.env.flush_all()
        return json_response(self._render(rec), 200, cors)

    def _pay_quote_offer_cash(self, cors, rec):
        """An equipment quote whose offer the admin sent: paying on delivery from this page accepts the offer as
        sent (nothing removed) and confirms it, the same two steps as POST /quotes/<ref>/accept then
        /quotes/<ref>/payment, so the track page needs no other route."""
        rec = rec.with_user(SUPERUSER_ID).sudo()
        order = rec.quote_order_id
        if order.state not in ("draft", "sent") or not rec.leaders_offer_open():
            raise AuthApiError(409, "invalid_stage", "There is nothing to pay at this stage.")
        if rec.state == "offered":
            rec.write({"offer_accepted_at": fields.Datetime.now(), "state": "confirming"})
        order.write({"leaders_payment_method": "cod", "require_payment": False})
        rec.sf_payment_method = "cash"
        order.action_confirm()  # moves the request to executing
        if rec.state == "confirming":
            rec.state = "executing"
        rec.env.flush_all()
        return json_response(self._render(rec), 200, cors)

    @http.route("/equipment-requests/<string:reference>/payment/return", type="http", auth="public", methods=["GET"],
                csrf=False, save_session=False)
    def payment_return(self, reference, **kw):
        """Gateway return: back to the request's ``returnUrl`` with ``?payment=success`` or ``?payment=failed``."""
        rec = request.env["leaders.customer.request"].sudo().search([("name", "=", reference)], limit=1)
        if not rec or not rec.return_url:
            return request.redirect("/", local=True)
        order = rec.payment_order_id
        paid = bool(order) and any(t.state in ("done", "authorized") for t in order.transaction_ids)
        url = rec.return_url.replace("{reference}", rec.name).replace("%7Breference%7D", rec.name)
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "payment"] + [("payment", "success" if paid else "failed")]
        return request.redirect(urlunsplit(parts._replace(query=urlencode(query))), local=False)

    @http.route("/equipment-requests/<string:reference>/received", type="http", auth="public",
                methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def received(self, cors, reference, **kw):
        """The customer confirms receipt ("تم استلام الطلب") of a confirmed or in-progress request: it is completed."""
        rate_limit("sf_orders")
        user = bearer_user(required=True)
        rec = request.env["leaders.customer.request"].sudo().search([
            ("name", "=", reference), ("kind", "in", ("purchase", "rent", "quote")),
            ("partner_id", "=", user.partner_id.commercial_partner_id.id)], limit=1)
        if not rec or (rec.kind == "quote" and rec.line_ids):  # a cart quote is received with POST /quotes/<ref>/received
            raise AuthApiError(404, "not_found", "Request not found.")
        if STATUS.get(rec.state) not in ("confirmed", "inProgress"):
            raise AuthApiError(409, "invalid_stage", "This request cannot be marked as received now.")
        rec = rec.with_user(SUPERUSER_ID).sudo()
        rec.state = "completed"
        rec.env.flush_all()
        return json_response(self._render(rec), 200, cors)
