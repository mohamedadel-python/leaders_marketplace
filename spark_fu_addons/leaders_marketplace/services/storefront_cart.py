"""Storefront cart: guest cart by cookie, user cart by user, merge after login."""
from odoo import fields

from ..models.storefront import RENT_PERIODS, SERVICES
from ..tools.auth_errors import AuthApiError, validation_error
from .storefront import LISTINGS, Storefront, minor

PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "custom": 1}
MAX_QTY = 999


class CartService:

    def __init__(self, env, user=None, token=None):
        self.env = env
        self.user, self.token = user, token
        self.Cart = env["leaders.cart"].sudo()
        self.Line = env["leaders.cart.line"].sudo()
        self.store = Storefront(env)

    # ------------------------------------------------------------ the cart
    def find(self):
        if self.user:
            return self.Cart.search([("user_id", "=", self.user.id)], limit=1)
        return self.Cart.search([("token", "=", self.token), ("user_id", "=", False)], limit=1) if self.token else self.Cart

    def find_or_create(self):
        cart = self.find()
        return cart or self.Cart.create({"user_id": self.user.id} if self.user else {})

    # ----------------------------------------------------------- rendering
    def unit_price(self, line):
        t = line.product_tmpl_id
        if line.listing == "rent":
            if line.rent_period == "custom":  # "agree a term with us": quoted, never priced
                return None
            return minor(t._leaders_rent_customer_price() * PERIOD_DAYS.get(line.rent_period or "day", 1)) if t.leaders_rent_price_day else None
        if line.platform_seller:  # the platform's own stock, at the platform's price
            return minor(self.store.platform_price(t))
        if line.offer_id:  # the seller the customer picked sells at its own price
            return minor(line.offer_id.price + t.sudo().leaders_inspection_cost) if line.offer_id.price > 0 else None
        return minor(t.leaders_display_price) if t.leaders_display_price > 0 else None

    def service_lines(self, line):
        """The services of a cart line that are real services of the product (a code like ``operator`` that is no
        service page stays in ``options.services`` and is not listed here)."""
        asked = [s for s in (line.services or "").split(",") if s]
        if not asked:
            return []
        pages = {p.leaders_slug: p for p in self.store.product_services(line.product_tmpl_id)}
        return [self.store.service_option(pages[s]) for s in asked if s in pages]

    def render(self, cart):
        lines, subtotal = [], 0
        for line in (cart.line_ids if cart else []):
            options = {"listing": line.listing}
            if line.rent_period:
                options["rentPeriod"] = line.rent_period
            if line.source:
                options["source"] = line.source
            if line.services:
                options["services"] = [s for s in line.services.split(",") if s]
            if line.platform_seller:
                options["offerId"] = "platform"
            elif line.offer_id:
                options["offerId"] = str(line.offer_id.id)
            if line.backorder:
                options["backorder"] = line.backorder
            data = {"lineId": line.line_key, "product": self.store.product(line.product_tmpl_id),
                    "quantity": line.quantity, "options": options}
            service_lines = self.service_lines(line)
            if service_lines:  # optional: the services asked with this line, as the product page lists them
                data["serviceLines"] = service_lines
            if not self.store.is_visible(line.product_tmpl_id):  # optional: false when the product left the storefront
                data["available"] = False
            unit = self.unit_price(line)
            if unit is not None:
                data["unitPrice"] = unit
                subtotal += unit * line.quantity
            lines.append(data)
        # services are quote-only: they add nothing to what is paid, each one becomes a request linked to the order
        return {"id": str(cart.id) if cart else "", "lines": lines, "subtotal": subtotal,
                "servicesTotal": 0, "total": subtotal,
                "updatedAt": (cart.write_date if cart else fields.Datetime.now()).strftime("%Y-%m-%dT%H:%M:%SZ")}

    # ---------------------------------------------------------- validation
    def _quantity(self, value, errors, default=None):
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_QTY:
            errors["quantity"] = "Quantity must be a whole number between 1 and %s." % MAX_QTY
        return value

    def _options(self, tmpl, raw, errors, base=None):
        raw = raw if isinstance(raw, dict) else {}
        if tmpl.leaders_kind and tmpl.leaders_kind != "part":  # equipment is ordered with a request, not the cart
            raise AuthApiError(409, "equipment_request_only",
                               "Equipment is ordered with a purchase or rent request, not the cart.")
        listing = raw.get("listing", base["listing"] if base else "buy")
        if tmpl.leaders_out_of_stock:
            raise AuthApiError(409, "out_of_stock", "This product is out of stock.")
        if listing == "buy":
            self.store.refuse_unsold(tmpl)
        if listing not in LISTINGS:
            errors["options.listing"] = "Must be buy or rent."
        elif listing == "rent" and not tmpl.leaders_rentable:
            errors["options.listing"] = "This product cannot be rented."
        elif listing == "buy" and tmpl.leaders_display_price <= 0:
            pass
        period = raw.get("rentPeriod", base["rent_period"] if base else None)
        if listing == "rent":
            period = period or "day"
            if period not in dict(RENT_PERIODS):
                errors["options.rentPeriod"] = "Unknown rent period."
        else:
            period = None
        source = raw.get("source", base["source"] if base else None)
        if source:
            allowed = self.store._sources(tmpl)
            if tmpl.leaders_kind != "part" or source not in self.store.api_sources() or (allowed and source not in allowed):
                errors["options.source"] = "Source applies to parts: one of %s." % (", ".join(allowed) or "turkish, chinese, egyptian")
        services = raw.get("services", (base["services"] or "").split(",") if base else [])
        services = services if isinstance(services, list) else []
        known = set(SERVICES) | {p.leaders_slug for p in self.store.product_services(tmpl)}  # codes and the product's services
        if any(s not in known for s in services):
            errors["options.services"] = "Unknown service."
        offer = self.env["leaders.marketplace.offer"]
        offer_id = raw.get("offerId", ("platform" if base.get("platform_seller") else base["offer_id"]) if base else None)
        platform = False
        if offer_id == "platform" and listing == "buy":
            platform = bool(self.store.platform_card(tmpl))
            if not platform:
                errors["options.offerId"] = "This seller does not sell this product any more."
        elif offer_id and listing == "buy":
            offer = self.store.offers(tmpl).filtered(lambda o: str(o.id) == str(offer_id))
            if not offer:
                errors["options.offerId"] = "This seller does not sell this product any more."
        backorder = raw.get("backorder", base["backorder"] if base else 0)
        if isinstance(backorder, bool) or not isinstance(backorder, int) or not 0 <= backorder <= MAX_QTY:
            errors["options.backorder"] = "Units to source must be a whole number between 0 and %s." % MAX_QTY
            backorder = 0
        elif backorder and listing != "buy":
            errors["options.backorder"] = "Only bought products can be sourced."
        return {"listing": listing, "rent_period": period, "source": source or False,
                "services": ",".join(sorted(set(s for s in services if s in known))) or False,
                "offer_id": offer.id or False, "platform_seller": platform, "backorder": backorder}

    def _product(self, product_id):
        try:
            tmpl = self.store.Template.search(self.store.base_domain() + [("id", "=", int(product_id))], limit=1)
        except (TypeError, ValueError):
            tmpl = self.store.Template
        if not tmpl:
            raise AuthApiError(404, "not_found", "Product not found.")
        return tmpl

    # ------------------------------------------------------------- actions
    def add(self, body):
        tmpl = self._product(body.get("productId"))
        errors = {}
        quantity = self._quantity(body.get("quantity"), errors, 1)
        options = self._options(tmpl, body.get("options"), errors)
        if errors:
            raise validation_error(errors)
        cart = self.find_or_create()
        key = self.Line.make_key(tmpl.id, options["listing"], options["rent_period"], options["source"],
                                 (options["services"] or "").split(","),
                                 "platform" if options["platform_seller"] else options["offer_id"])
        existing = cart.line_ids.filtered(lambda l: l.line_key == key)
        if options["listing"] == "buy":  # never more than the seller holds
            self.store.stock_check(tmpl, self.env["leaders.marketplace.offer"].sudo().browse(options["offer_id"]),
                                   min((existing.quantity if existing else 0) + quantity, MAX_QTY),
                                   platform=options["platform_seller"])
        if existing:
            existing.quantity = min(existing.quantity + quantity, MAX_QTY)
            if isinstance(body.get("options"), dict) and "backorder" in body["options"]:
                existing.backorder = options["backorder"]
        else:
            self.Line.create(dict(options, cart_id=cart.id, product_tmpl_id=tmpl.id, quantity=quantity))
        cart._touch()
        return cart

    def line(self, cart, line_id):
        line = cart.line_ids.filtered(lambda l: l.line_key == line_id) if cart else self.Line
        if not line:
            raise AuthApiError(404, "not_found", "Cart line not found.")
        return line[:1]

    def update(self, line_id, body):
        cart = self.find()
        line = self.line(cart, line_id)
        errors = {}
        quantity = self._quantity(body.get("quantity"), errors)
        base = {"listing": line.listing, "rent_period": line.rent_period, "source": line.source,
                "services": line.services, "offer_id": line.offer_id.id, "platform_seller": line.platform_seller,
                "backorder": line.backorder}
        options = self._options(line.product_tmpl_id, body.get("options"), errors, base) \
            if "options" in body else None
        if errors:
            raise validation_error(errors)
        if options:
            line.write(options)
            twin = (cart.line_ids - line).filtered(lambda l: l.line_key == line.line_key)
            if twin:
                line.quantity += twin.quantity
                twin.unlink()
        if quantity is not None:
            if line.listing == "buy":
                self.store.stock_check(line.product_tmpl_id, line.offer_id, quantity, platform=line.platform_seller)
            line.quantity = quantity
        cart._touch()
        return cart

    def remove(self, line_id):
        cart = self.find()
        line = cart.line_ids.filtered(lambda l: l.line_key == line_id) if cart else self.Line
        if line:
            line.unlink()
            cart._touch()
        return cart

    def clear(self):
        cart = self.find()
        if cart:
            cart.line_ids.unlink()

    def merge_guest(self, guest_token):
        """Fold the guest cart into the user's, summing identical lines."""
        mine = self.find_or_create()
        guest = self.Cart.search([("token", "=", guest_token), ("user_id", "=", False)], limit=1) if guest_token else self.Cart
        for line in guest.line_ids:
            twin = mine.line_ids.filtered(lambda l: l.line_key == line.line_key)
            if twin:
                twin.quantity = min(twin.quantity + line.quantity, MAX_QTY)
            else:
                line.cart_id = mine
        guest.unlink()
        mine._touch()
        return mine
