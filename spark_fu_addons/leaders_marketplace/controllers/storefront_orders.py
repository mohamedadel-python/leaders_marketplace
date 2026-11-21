"""Storefront checkout, promo and delivery-region endpoints."""
import json
import re
from datetime import timedelta

from odoo import http, fields
from odoo.http import request

from ..services.storefront import Storefront, fmt_num, minor
from ..services.storefront_cart import PERIOD_DAYS
from ..tools.auth_errors import AuthApiError, json_response, validation_error
from .storefront_common import bearer_user, json_body, online_payments_ready, rate_limit, storefront_api


FRONTEND = "https://spark-fu.com"


def _gov_key(state):
    """The governorate key the storefront uses: the English name in lower case without spaces ("cairo")."""
    return re.sub(r"\W", "", (state.with_context(lang="en_US").name or "").lower())


STAGES = ("placed", "preparing", "shipped", "outForDelivery", "delivered")


def _name_sort_key(name):
    """Alphabetical order of a place name, as a reader of its language expects: Arabic ignores the leading "ال" and
    reads أ إ آ as ا and a number as nothing ("6 أكتوبر" sits with the أ names); Latin names sort by their letters."""
    text = re.sub(r"[0-9\u0660-\u0669\s]+", " ", (name or "").strip()).strip().lower()
    text = text.translate(str.maketrans("أإآٱىةؤئ", "اااايهوي"))
    if text.startswith("ال") and len(text) > 2:
        text = text[2:]
    return text


class StorefrontOrders(http.Controller):

    def _base_url(self):
        return request.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")

    def _order_by_ref(self, reference):
        return request.env["sale.order"].sudo().search([("leaders_public_ref", "=", reference)], limit=1)

    @http.route("/orders/<string:reference>/payment", type="http", auth="public", methods=["GET"], csrf=False, save_session=False)
    def payment_page(self, reference, locale="en", **kw):
        """Hosted payment step: sends the shopper to the order's payment page, which returns to /payment/return."""
        order = self._order_by_ref(reference)
        if not order:
            return request.redirect("%s/%s/checkout/payment" % (FRONTEND, "ar" if locale == "ar" else "en"), local=False)
        return request.redirect(order.get_portal_url(), local=False)

    @http.route("/orders/<string:reference>/payment/return", type="http", auth="public", methods=["GET"], csrf=False, save_session=False)
    def payment_return(self, reference, locale="en", **kw):
        """Gateway return: paid or authorised goes to the confirmation page, anything else back to the payment step."""
        locale = "ar" if locale == "ar" else "en"
        order = self._order_by_ref(reference)
        paid = bool(order) and any(t.state in ("done", "authorized") for t in order.transaction_ids)
        target = "%s/%s/orders/%s/confirmed" % (FRONTEND, locale, reference) if paid else "%s/%s/checkout/payment" % (FRONTEND, locale)
        return request.redirect(target, local=False)

    def _order_for(self, reference, user):
        order = request.env["sale.order"].sudo().search([
            ("leaders_public_ref", "=", reference), ("partner_id", "=", user.partner_id.commercial_partner_id.id)
        ], limit=1)
        if not order:
            raise AuthApiError(404, "not_found", "Order not found.")
        return order

    def _zone(self, governorate):
        states = request.env["res.country.state"].with_context(lang="en_US").sudo().search([])
        state = states.filtered(lambda st: _gov_key(st) == governorate.lower())[:1]
        zone = request.env["leaders.vendor.delivery.zone"].sudo().search(
            [("is_platform", "=", True), ("active", "=", True), ("state_id", "=", state.id)], limit=1) if state else None
        return state, zone

    def _promo(self, code):
        promo = request.env["leaders.promo.code"].sudo().search([("name", "=ilike", code)], limit=1)
        if not promo:
            raise AuthApiError(404, "not_found", "Promo code not found.")
        if promo.date_end and promo.date_end < fields.Date.today():
            raise AuthApiError(410, "promo_expired", "This promo code has expired.")
        return promo

    def _offer(self, template, options, index):
        """The seller the customer picked (``options.offerId``), validated; empty: the Buy Box winner later."""
        offer_id = options.get("offerId")
        if not offer_id or options.get("listing") == "rent":
            return request.env["leaders.marketplace.offer"]
        if offer_id == "platform":  # the platform's own stock: no vendor offer
            if not Storefront(request.env).platform_card(template):
                raise validation_error({"items.%s.options.offerId" % index: "This seller does not sell this product any more."})
            return request.env["leaders.marketplace.offer"]
        offer = Storefront(request.env).offers(template).filtered(lambda o: str(o.id) == str(offer_id))
        if not offer:
            raise validation_error({"items.%s.options.offerId" % index: "This seller does not sell this product any more."})
        return offer

    def _unit_price(self, template, options, index, offer=None):
        """The server price in minor units of one unit, or a validation error for unpriced products."""
        if offer:
            return minor(offer.price + template.sudo().leaders_inspection_cost)
        if options.get("offerId") == "platform" and options.get("listing", "buy") == "buy":
            return minor(Storefront(request.env).platform_price(template))
        if options.get("listing") == "rent":
            if not template.leaders_rentable or not template.leaders_rent_price_day or options.get("rentPeriod") == "custom":
                raise validation_error({"items.%s" % index: "This product is sold on quote only."})
            return minor(template._leaders_rent_customer_price() * PERIOD_DAYS.get(options.get("rentPeriod") or "day", 1))
        if template.leaders_display_price <= 0:
            raise validation_error({"items.%s" % index: "This product is sold on quote only."})
        return minor(template.leaders_display_price)

    @http.route("/orders", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def place(self, cors, **kw):
        rate_limit("sf_orders")
        user = bearer_user(required=True)
        body = json_body()
        errors = {}
        items = body.get("items") if isinstance(body.get("items"), list) else []
        shipping = body.get("shipping") if isinstance(body.get("shipping"), dict) else {}
        if not items:
            errors["items"] = "At least one item is required."
        for key in ("name", "phone", "governorate", "city", "address"):
            if not isinstance(shipping.get(key), str) or not shipping[key].strip():
                errors["shipping.%s" % key] = "Required."
        if body.get("payment") not in ("cod", "card", "paypal", "applePay"):
            errors["payment"] = "Invalid payment method."
        elif body["payment"] != "cod" and not online_payments_ready(request.env):
            # no gateway yet: an order that can never be paid is not created
            errors["payment"] = "Online payment is not available yet. Choose cash on delivery."
        if errors:
            raise validation_error(errors)
        state, zone = self._zone(shipping["governorate"].strip())
        if not zone:
            raise validation_error({"shipping.governorate": "We do not deliver to this governorate."})
        promo = self._promo(body["promoCode"]) if body.get("promoCode") else None

        lines = []
        asked = {}  # units asked so far per (seller, product), across the lines
        for index, item in enumerate(items):
            item = item if isinstance(item, dict) else {}
            template = request.env["product.template"].sudo().search([("id", "=", item.get("productId"))], limit=1) \
                if isinstance(item.get("productId"), (int, str)) and str(item.get("productId")).isdigit() else None
            product = template.product_variant_id if template else None
            if not product:
                raise AuthApiError(404, "not_found", "Product not found.")
            quantity = item.get("quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 999:
                raise validation_error({"items.%s.quantity" % index: "Quantity must be between 1 and 999."})
            options = item.get("options") if isinstance(item.get("options"), dict) else {}
            options.setdefault("listing", "buy")
            backorder = options.get("backorder", 0)
            if isinstance(backorder, bool) or not isinstance(backorder, int) or not 0 <= backorder <= 999:
                raise validation_error({"items.%s.options.backorder" % index: "Units to source must be 0 to 999."})
            if not backorder:
                options.pop("backorder", None)
            if template.leaders_kind and template.leaders_kind != "part":
                raise validation_error({"items.%s" % index: "Equipment is ordered with a purchase or rent request, not the cart."})
            if template.leaders_out_of_stock:
                raise AuthApiError(409, "out_of_stock", "This product is out of stock.",
                                   fields={"items.%s" % index: "Out of stock."})
            if options.get("listing", "buy") == "buy":
                Storefront(request.env).refuse_unsold(template, fields={"items.%s" % index: "Out of stock."})
            offer = self._offer(template, options, index)
            unit = self._unit_price(template, options, index, offer)
            if item.get("unitPrice") != unit:
                raise AuthApiError(409, "price_changed", "The product price changed.",
                                   fields={"items.%s" % index: "Price changed."})
            if options.get("listing") == "buy":
                # lines of the same product from the same seller share one stock: the check counts them together
                seller = ("platform" if options.get("offerId") == "platform" else offer.id, template.id)
                asked[seller] = asked.get(seller, 0) + quantity
                try:
                    Storefront(request.env).stock_check(template, offer, asked[seller],
                                                        platform=options.get("offerId") == "platform")
                except AuthApiError as error:  # say which line
                    error.fields = {"items.%s.quantity" % index: error.fields.get("quantity")}
                    error.extra["index"] = index
                    raise
            options["_offer"] = offer.id or False
            lines.append((template, product, quantity, options, unit))

        partner = user.partner_id.commercial_partner_id
        shipping_partner = request.env["res.partner"].sudo().create({
            "name": shipping["name"].strip(), "phone": shipping["phone"].strip(),
            "email": shipping.get("email") or False, "street": shipping["address"].strip(),
            "city": shipping["city"].strip(), "state_id": state.id,
            "country_id": state.country_id.id, "parent_id": partner.id, "type": "delivery",
        })
        goods = sum(unit * quantity for _t, _p, quantity, _o, unit in lines) / 100.0
        if zone.minimum_order and goods < zone.minimum_order:
            raise validation_error({"items": "The minimum order for delivery to this governorate is %s." % fmt_num(zone.minimum_order)})
        def needs_delivery(options):  # a rental is delivered only when the renter asked for it
            services = options.get("services") or []
            services = services if isinstance(services, list) else str(services).split(",")
            return options.get("listing") != "rent" or "delivery" in services
        shipped = [line for line in lines if needs_delivery(line[3])]
        fee = 0.0 if not shipped or (zone.free_shipping_threshold and goods >= zone.free_shipping_threshold) else \
            zone.price + self._delivery_surcharge([template for template, _p, _q, _o, _u in shipped])
        rent_lines = [line for line in lines if line[3].get("listing") == "rent"]
        contract = request.env["leaders.site.page"].sudo().search([("slug", "=", "rent-contract"), ("published", "=", True)],
                                                                  limit=1)
        if rent_lines and contract and body.get("contractAccepted") is not True:
            raise validation_error({"contractAccepted": "Accept the rental contract to book."})
        order = request.env["sale.order"].sudo().create({
            "partner_id": partner.id, "partner_invoice_id": partner.id,
            "partner_shipping_id": shipping_partner.id,
            "leaders_sf_payment": body["payment"],
            "leaders_payment_method": "cod" if body["payment"] == "cod" else "online",
            "leaders_sf_percent_off": promo.percent_off if promo else 0,
            "leaders_sf_delivery_fee": fee, "leaders_sf_lead_min": zone.lead_days_min,
            "leaders_sf_lead_max": zone.lead_days_max, "leaders_sf_city_key": shipping["city"].strip(),
            "leaders_sf_governorate_key": shipping["governorate"].strip(),
            "leaders_sf_notes": shipping.get("notes") or False,
            "leaders_sf_history": json.dumps({"placed": fields.Datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")}),
        })
        for template, product, quantity, options, unit in lines:
            offer_id = options.pop("_offer", False)
            vals = {
                "order_id": order.id, "product_id": product.id, "product_uom_qty": quantity,
                "name": template.display_name, "price_unit": unit / 100.0,
                "discount": promo.percent_off if promo else 0.0, "tax_id": [(5, 0, 0)],  # the shown price is final
                "leaders_sf_options": json.dumps(options),
            }
            if offer_id:  # the vendor order goes to the seller the customer picked
                vals["marketplace_offer_id"] = offer_id
            elif options.get("offerId") == "platform":  # the platform's own stock: never handed to a vendor
                vals["leaders_platform_sold"] = True
            request.env["sale.order.line"].sudo().create(vals)
        reference = order.leaders_public_ref_get()
        sourcing = [(product, options["backorder"]) for _t, product, _q, options, _u in lines if options.get("backorder")]
        if sourcing:  # units beyond stock the customer asked us to source: a to-do, not billed
            order._leaders_open_sourcing_tasks(sourcing)
        locale = "ar" if (request.httprequest.headers.get("Accept-Language") or "").lower().startswith("ar") else "en"
        order.leaders_sf_locale = locale
        for template, _p, quantity, options, _u in rent_lines:  # the platform rents the machine in its own name
            days = PERIOD_DAYS.get(options.get("rentPeriod") or "day", 1)
            today = fields.Date.context_today(order)
            services = options.get("services") or []
            services = services if isinstance(services, list) else str(services).split(",")
            request.env["leaders.rental"].sudo().create({
                "product_tmpl_id": template.id, "partner_id": partner.id, "sale_order_id": order.id,
                "state_id": state.id, "date_start": today, "date_end": today + timedelta(days=days * max(quantity, 1)),
                "supplier_price_day": template.leaders_rent_price_day,
                "customer_price_day": template._leaders_rent_customer_price(),
                "delivery_requested": "delivery" in services,
                "contract_accepted_on": fields.Datetime.now() if contract else False,
                "contract_version": contract.write_date.strftime("%Y-%m-%d %H:%M") if contract else False})
        if fee:
            delivery = request.env.ref("leaders_marketplace.product_product_marketplace_delivery")
            request.env["sale.order.line"].sudo().create({
                "order_id": order.id, "product_id": delivery.id, "product_uom_qty": 1, "price_unit": fee,
                "name": "Delivery", "is_delivery": True, "tax_id": [(5, 0, 0)]})
        if body["payment"] != "cod":
            order.write({"require_payment": True, "prepayment_percent": 1.0})
        else:  # cash: a subscription vendor's part goes to them now (online orders go once paid)
            order._leaders_route_direct_lines()
        cart = request.env["leaders.cart"].sudo().search([("user_id", "=", user.id)], limit=1)
        if cart:  # only what was ordered; unpriced lines stay in the quote tab
            ordered = set()
            for template, _p, _q, options, _u in lines:
                services = options.get("services")
                services = services if isinstance(services, list) else (services or "").split(",")
                ordered.add(request.env["leaders.cart.line"].make_key(
                    template.id, options.get("listing"), options.get("rentPeriod"), options.get("source"), services,
                    int(options["offerId"]) if str(options.get("offerId") or "").isdigit() and options.get("listing") == "buy" else None))
            cart.line_ids.filtered(lambda l: l.line_key in ordered).unlink()
        result = {"reference": reference}
        if body["payment"] != "cod":
            result["paymentUrl"] = "%s/orders/%s/payment?locale=%s" % (self._base_url(), reference, locale)
        return json_response(result, 201, cors)

    @http.route("/orders/<string:reference>", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def detail(self, cors, reference, **kw):
        rate_limit("sf_orders")
        order = self._order_for(reference, bearer_user(required=True))
        return json_response(self._render(order), 200, cors)

    @http.route("/orders/<string:reference>/cancel", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["POST"])
    def cancel(self, cors, reference, **kw):
        rate_limit("sf_orders")
        user = bearer_user(required=True)
        order = self._order_for(reference, user)
        stage, _history = self._stage(order)
        if order._leaders_sf_state() == "cancel" or stage not in ("placed", "preparing"):
            raise AuthApiError(409, "not_cancellable", "This order can no longer be cancelled.")
        parts = order.sudo().vendor_order_ids.filtered(lambda v: v.state != "cancelled")
        if parts:  # the parts already sent to a vendor are cancelled with the checkout
            parts.write({"cancelled_by_type": "customer"})
            parts.with_context(leaders_customer_cancel=True).action_cancel()
        if order.state != "cancel":
            order._action_cancel()
        return json_response(self._render(order), 200, cors)

    def _stage(self, order):
        """(stage, history): the furthest stage the order reached and when each stage was reached.

        An order can hold several parts: what the platform sells itself and one part per vendor. Each part has its own
        stage and the order is where its slowest part is, so it is never shown delivered while a part is still being
        prepared. A shipment that left a vendor (its picking is done) is shipped; it is delivered when the vendor's
        shipment is confirmed delivered."""
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        history = json.loads(order.leaders_sf_history or "{}")
        history.setdefault("placed", order.create_date.strftime(fmt))
        rank = {name: i for i, name in enumerate(STAGES)}
        company_stage = "placed"
        if order._leaders_sf_state() in ("sale", "done"):
            company_stage = "preparing"
            history.setdefault("preparing", (order.date_order or order.create_date).strftime(fmt))
        own = order.picking_ids.filtered(lambda p: p.state != "cancel")
        if own and all(p.state == "done" for p in own):
            company_stage = "delivered"
        elif any(p.state == "done" for p in own):
            company_stage = "shipped"
        vendor_orders = order.sudo().vendor_order_ids.filtered(lambda v: v.state != "cancelled")
        parts = [company_stage] if (order._marketplace_company_lines() or not vendor_orders) else []
        for vendor_order in vendor_orders:
            groups = vendor_order.fulfillment_group_ids.filtered(lambda g: g.state != "cancelled" and not g.is_replacement)
            sent = groups.picking_ids.filtered(lambda p: p.state == "done")
            part = "preparing"  # it reached the vendor
            if sent:
                part = "shipped"
            if groups and all(g.state == "delivered" for g in groups):
                part = "delivered"
            parts.append(part)
        stage = min(parts, key=rank.get)
        if stage in ("placed", "preparing") and any(rank[p] >= rank["shipped"] for p in parts):
            stage = "preparing"  # a part is already on its way: the order is at least being prepared
        pickings = (own | vendor_orders.fulfillment_group_ids.picking_ids).filtered(lambda p: p.state == "done")
        if stage in ("shipped", "delivered") and pickings:
            history.setdefault(stage, max(pickings.mapped("date_done")).strftime(fmt))
        if stage == "delivered":
            history.setdefault("shipped", min(pickings.mapped("date_done")).strftime(fmt)) if pickings else None
        if stage != "placed":
            history.setdefault("preparing", (order.date_order or order.create_date).strftime(fmt))
        return stage, {k: v for k, v in history.items() if k in STAGES}

    def _render(self, order):
        lines, goods = [], 0
        for product, quantity, price_unit, options in order._leaders_sf_lines():
            template = product.product_tmpl_id
            unit = minor(price_unit)
            goods += unit * int(quantity)
            row = {"productId": str(template.id), "slug": template.leaders_slug or str(template.id),
                   "name": template.display_name, "quantity": int(quantity), "unitPrice": unit,
                   "options": json.loads(options or "{}")}
            if template.image_1920:
                row["image"] = "%s/web/image/product.template/%s/image_1024" % (self._base_url(), template.id)
            lines.append(row)
        shipping = order.partner_shipping_id
        details = {"name": shipping.name or "", "phone": shipping.phone or "",
                   "governorate": order.leaders_sf_governorate_key or _gov_key(shipping.state_id),
                   "city": order.leaders_sf_city_key or shipping.city or "", "address": shipping.street or ""}
        if shipping.email:
            details["email"] = shipping.email
        if order.leaders_sf_notes:
            details["notes"] = order.leaders_sf_notes
        stage, history = self._stage(order)
        discount = int(round(goods * order.leaders_sf_percent_off / 100.0))
        fee = minor(order.leaders_sf_delivery_fee)
        placed = order.create_date
        low, high = order.leaders_sf_lead_min or 3, order.leaders_sf_lead_max or max(order.leaders_sf_lead_min, 5)
        result = {"reference": order.leaders_public_ref_get(), "placedAt": placed.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "stage": stage, "history": history, "cancelled": order._leaders_sf_state() == "cancel",
                  "lines": lines, "shipping": details, "payment": order.leaders_sf_payment or "cod",
                  "totals": {"subtotal": goods, "shipping": fee, "discount": discount, "total": goods - discount + fee},
                  "eta": {"from": (placed + timedelta(days=low)).date().isoformat(),
                          "to": (placed + timedelta(days=high)).date().isoformat()}}
        done = order.picking_ids.filtered(lambda p: p.state == "done" and "carrier_id" in p._fields and p.carrier_id)
        if done:
            result["courier"] = {"name": done[0].carrier_id.name, "phone": done[0].carrier_id.company_id.phone or ""}
        until = self._returnable_until(order)
        if until:  # optional: the last moment a return can be asked; omitted when nothing can be returned now
            result["returnableUntil"] = until.strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    @staticmethod
    def _returnable_until(order):
        """End of the return window (shipment + window days) of the shipped parts still returnable, latest first."""
        window = int(request.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.return_window_days", 14) or 0)
        if not window:
            return None
        parts = order.sudo().vendor_order_ids.filtered(lambda v: v.date_shipped and v._customer_returnable_lines())
        return max((v.date_shipped + timedelta(days=window) for v in parts), default=None)

    @staticmethod
    def _delivery_surcharge(templates):
        """The highest delivery surcharge of the subcategories in the order, charged once."""
        store = Storefront(request.env)
        return max([store.subcategory(t)._leaders_effective().leaders_delivery_surcharge
                    for t in templates if store.subcategory(t)] or [0.0])

    @http.route("/promo-codes/<string:code>", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def promo(self, cors, code, **kw):
        rate_limit("sf_orders")
        bearer_user(required=True)
        promo = self._promo(code)
        return json_response({"code": promo.name, "percentOff": promo.percent_off}, 200, cors)

    @http.route("/delivery/regions", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False)
    @storefront_api(["GET"])
    def regions(self, cors, **kw):
        from .vendor_apply import GOVERNORATES_AR
        zones = request.env["leaders.vendor.delivery.zone"].sudo().search([("is_platform", "=", True), ("active", "=", True)])
        arabic = (request.httprequest.headers.get("Accept-Language") or "").lower().startswith("ar")
        regions = []
        for zone in zones:
            governorate = _gov_key(zone.state_id)
            cities = [c.strip() for c in (zone.storefront_cities or "").split(",") if c.strip()]
            # name: the governorate in the Accept-Language language, for the list (BUG-00901); the key stays as is
            name = (GOVERNORATES_AR.get(zone.state_id.code) if arabic else None) \
                or zone.state_id.with_context(lang="en_US").name or governorate
            region = {"governorate": governorate, "name": name, "cities": cities, "fee": minor(zone.price)}
            # optional keys: free delivery from this amount of goods / minimum order (far governorates)
            if zone.free_shipping_threshold > 0:
                region["freeShippingThreshold"] = minor(zone.free_shipping_threshold)
            if zone.minimum_order > 0:
                region["minimumOrder"] = minor(zone.minimum_order)
            regions.append(region)
        # the list reads in the order of the language it is shown in (the zones come in the English order)
        regions.sort(key=lambda r: _name_sort_key(r["name"]))
        return json_response(regions, 200, cors)
