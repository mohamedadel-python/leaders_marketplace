"""Storefront API: product serialisation, filters, facets and sorting.

Money leaves as integer minor units (piastres); filter bounds arrive in whole units.
Optional fields are omitted when unset, never sent as null.
"""
import math
import re

from odoo.osv import expression

from ..models.catalog_spec import normalize_search
from ..tools.auth_errors import AuthApiError, validation_error

PLATFORM_SELLS_PARAM = "leaders_marketplace.platform_sells_products"
PLATFORM_OFFER = "platform"  # offerId of the platform's own stock when vendors sell the same product too

MAX_PAGE_SIZE = 48
MAX_QUERY = 100
KINDS = ("power", "equipment", "part")
SORTS = ("newest", "brandAsc", "brandDesc", "priceAsc", "priceDesc")
POWER_BANDS = {"upTo100": (None, 100), "from100": (100, 250), "from250": (250, 500), "above500": (500, None)}
HOURS_BANDS = {"under1000": (None, 1000), "from1000": (1000, 5000), "over5000": (5000, None)}
CONDITIONS = ("new", "used", "refurbished")
LISTINGS = ("buy", "rent")
GROUP_KINDS = {"parts": "part", "power": "power", "equipment": "equipment"}
# labels of the fixed filter options (GET /products/filters): (English, Arabic)
LISTING_LABELS = {"buy": ("Buy", "شراء"), "rent": ("Rent", "إيجار")}
CONDITION_LABELS = {"new": ("New", "جديد"), "used": ("Used", "مستعمل"), "refurbished": ("Refurbished", "مُجدّد")}
HOURS_LABELS = {"under1000": ("Under 1,000 hours", "أقل من 1000 ساعة"),
                "from1000": ("1,000 – 5,000 hours", "1000 – 5000 ساعة"),
                "over5000": ("Over 5,000 hours", "أكثر من 5000 ساعة")}
# GET /products/filters: the request parameters of each option group (dropped when counting that group)
FILTER_GROUP_PARAMS = {
    "categories": ("categoryId", "subcategory", "section"), "listings": ("listing",), "conditions": ("condition",),
    "brands": ("brandId", "brand"), "powerSources": ("powerSourceId", "powerSource"), "sources": ("sourceId", "source"),
    "hours": ("hours",), "price": ("priceMin", "priceMax"), "kva": ("kvaMin", "kvaMax", "power"),
    "engine": ("engineMin", "engineMax"),
    "year": ("yearMin", "yearMax"), "family": ("family",), "warranty": ("warranty",),
}
# warranty bands of the catalog filter, in months (inclusive); "none" is no warranty
WARRANTY_BANDS = {"none": (0, 0), "lt6m": (1, 5), "6to12m": (6, 12), "gt12m": (13, None)}
TONES = {"parts": "soft", "power": "brand", "equipment": "primary", "services": "soft"}
UNIT_LABELS = {"kva": "kVA", "kw": "kW", "ton": "ton", "m3": "m³", "amp": "A", "m": "m"}
# the Arabic site gets these units and free-text spec values in Arabic; kVA, kW and names stay as they are
UNIT_LABELS_AR = {"ton": "طن", "m3": "م³", "m": "م"}
INTERVAL_RE = re.compile(r"^every\s+([\d,.]+)\s*(h|hr|hrs|hour|hours)$", re.IGNORECASE)


def minor(amount):
    return int(round((amount or 0.0) * 100))


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def csv_list(text):
    return [v.strip() for v in (text or "").split(",") if v.strip()]


def fmt_num(value):
    return ("%g" % value) if value == int(value) else ("%g" % round(value, 2))


class Storefront:

    def __init__(self, env):
        self.env = env
        self.Template = env["product.template"].sudo()
        self.Category = env["product.public.category"].sudo()
        self._hidden_categories = None
        self.base_url = (env["ir.config_parameter"].sudo().get_param("web.base.url") or "").rstrip("/")

    # ---------------------------------------------------------------- urls
    def url(self, model, rec_id, field="image_1024"):
        return "%s/web/image/%s/%s/%s" % (self.base_url, model, rec_id, field)

    # ------------------------------------------------------------ products
    def base_domain(self):
        # a used product that waits for its inspection is not shown, even if it was published before
        domain = [("marketplace_state", "=", "published"), ("is_published", "=", True), ("leaders_kind", "!=", False),
                  "|", ("leaders_requires_inspection", "=", False), ("leaders_inspected", "=", True)]
        # a rented (or out of service) machine leaves the storefront until it is back
        domain.append(("leaders_asset_state", "not in", ["rented", "out_of_service"]))
        # a hidden category hides its products too, unless they are also in a visible category
        if self._hidden_categories is None:
            self._hidden_categories = self.Category._leaders_hidden_tree()
        if self._hidden_categories:
            domain.append(("public_categ_ids", "in", self._visible_categories()))
        return domain

    def _visible_categories(self):
        return self.Category.search([("id", "not in", list(self._hidden_categories))]).ids or [0]

    def subcategory(self, tmpl):
        for categ in tmpl.public_categ_ids:
            if categ.parent_id and categ.leaders_slug:
                return categ
        return tmpl.public_categ_ids[:1]

    def _spec_lines(self, tmpl):
        out = {}
        for line in (tmpl.leaders_specs or "").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                out[re.sub(r"[\s_\-]+", "", key.strip().lower())] = value.strip()
        return out

    def product(self, tmpl, detail=True):
        t = tmpl.sudo()
        subcat = self.subcategory(t)
        eff = subcat._leaders_effective() if subcat else self.Category
        rentable = t.leaders_rentable
        buyable = eff.leaders_can_buy if subcat else True
        listing = "buy" if buyable or not rentable else "rent"
        available = (["buy"] if buyable else []) + (["rent"] if rentable else [])
        data = {
            "id": str(t.id), "slug": t.leaders_slug or str(t.id), "name": t.name or "",
            "brand": t.leaders_brand_id.name or "",
            "model": (t.leaders_model_id.name if t.leaders_kind != "part" else t.leaders_part_number) or "",
            "kind": t.leaders_kind, "subcategory": subcat.leaders_slug or "", "listing": listing,
            "inspected": bool(t.leaders_inspected),
            "listedAt": iso(t.marketplace_publish_date or t.create_date),
        }
        if available != [listing]:
            data["availableFor"] = available
        if t.image_1920:
            data["image"] = self.url("product.template", t.id)
        gallery = ([data["image"]] if t.image_1920 else []) + [
            self.url("product.image", i.id) for i in t.product_template_image_ids if i.image_1920]
        if gallery:
            data["gallery"] = gallery
        if t.leaders_display_price > 0:
            data["price"] = minor(t.leaders_display_price)
        if rentable and t.leaders_rent_price_day:
            # the platform rents it out in its own name, at the vendor's price plus the platform margin
            data["rentPrice"] = minor(t._leaders_rent_customer_price())
            data["rentSeller"] = self.env.company.name
        description = (t.description_sale or "").strip()
        if description:  # optional: the free-text description, in the Accept-Language language
            data["description"] = description
        sku = t.leaders_part_number or t.default_code
        if sku:
            data["sku"] = sku
        sources = self._sources(t)
        if sources:
            data["sources"] = sources
        services = t._leaders_available_services() if t.leaders_kind else []
        if services:  # optional: the services the customer can ask with this product
            data["services"] = [{"slug": s.leaders_slug, "name": s.name} for s in services]
        own = t._leaders_vendor_own_services() if t.leaders_kind else []
        if own:  # the vendor's own services ride in the same list, with a slug of their own
            data["services"] = data.get("services", []) + [s._leaders_card() for s in own]
        offers = self.offers(t) if "buy" in available else []
        if offers:  # optional: one card per seller with its price and quantity (Buy Box winner first)
            data["offers"] = [self.offer_card(o) for o in offers]
            own = self.platform_card(t)
            if own:  # the platform's own stock is one more seller
                data["offers"].append(own)
            data["stockQuantity"] = int(offers[0].available_qty())  # optional: units on hand at the first seller
        if not offers and "buy" in available and self.platform_sells():
            own = self.platform_qty(t)
            if own is not None:
                data["stockQuantity"] = own  # optional: units the platform holds of its own product
        if t.leaders_out_of_stock or (not rentable and self.no_seller(t)):
            data["inStock"] = False
        data["specs"] = self._specs(t)
        return data

    # ------------------------------------------------------------- sellers
    def offers(self, tmpl):
        """Live seller offers of a product: the Buy Box winner first, then by price."""
        Offer = self.env["leaders.marketplace.offer"].sudo()
        offers = Offer.search([("product_tmpl_id", "=", tmpl.id), ("state", "=", "active"), ("active", "=", True),
                               ("price", ">", 0)])._leaders_filter_by_market()
        return offers.sorted(lambda o: (not o.is_buybox_winner, o.price, o.id))

    def platform_sells(self):
        """Admin switch: the platform sells its own catalogue products (those without a vendor offer)."""
        return (self.env["ir.config_parameter"].sudo().get_param(PLATFORM_SELLS_PARAM) or "") in ("1", "True", "true")

    def platform_qty(self, tmpl):
        """Units the platform holds of its own product (free stock), or None when its stock is not tracked."""
        t = tmpl.sudo()
        if not t.is_storable:
            return None
        # the platform's own units only: vendor stock (FBM and FBL) always carries the vendor as owner
        quants = self.env["stock.quant"].sudo().search([
            ("product_id", "in", t.product_variant_ids.ids), ("location_id.usage", "=", "internal"),
            ("owner_id", "=", False), ("company_id", "in", (self.env.company.id, False))])
        return max(int(sum(q.quantity - q.reserved_quantity for q in quants)), 0)

    def platform_price(self, tmpl):
        t = tmpl.sudo()
        return t.list_price + (t.leaders_inspection_cost or 0.0)

    def platform_card(self, tmpl):
        """The platform as one more seller card, next to the vendors' offers: when it sells its own products and
        holds stock of this one. ``id`` is ``"platform"``."""
        if not self.platform_sells() or tmpl.sudo().list_price <= 0:
            return None
        qty = self.platform_qty(tmpl)
        if not qty or qty <= 0:
            return None
        return {"id": PLATFORM_OFFER, "seller": self.env.company.name,
                "unitPrice": minor(self.platform_price(tmpl)), "available": qty}

    def no_seller(self, tmpl):
        """A priced product nobody can sell now. With no live vendor offer, the project guide (p.3) has the
        platform hold no stock and sell nothing itself; when the admin lets the platform sell its own products,
        its own stock decides. A product without a price is asked for with a quote."""
        if tmpl.sudo().leaders_display_price <= 0 or self.offers(tmpl):
            return False
        if not self.platform_sells():
            return True
        qty = self.platform_qty(tmpl)
        return qty is not None and qty <= 0

    def refuse_unsold(self, tmpl, fields=None):
        if self.no_seller(tmpl):
            raise AuthApiError(409, "out_of_stock", "This product is out of stock.", fields=fields)

    def offer_card(self, offer):
        vendor = offer.vendor_id.commercial_partner_id
        # a commission vendor sells under the platform's name (the buyer never sees who they are)
        seller = self.env.company.name if vendor._leaders_name_hidden() else (vendor.vendor_store_name or vendor.name or "")
        return {"id": str(offer.id), "seller": seller,
                "unitPrice": minor(offer.price + offer.product_tmpl_id.sudo().leaders_inspection_cost),
                "available": int(offer.available_qty())}

    def stock_check(self, tmpl, offer, quantity, platform=False):
        """409 ``insufficient_stock`` when the seller has less than ``quantity``: the error says what this seller
        still has and lists the other sellers of the same product that hold stock (buy listing only).
        ``platform``: the customer picked the platform's own stock."""
        if platform:
            available = self.platform_qty(tmpl) or 0
            if quantity > available:
                raise AuthApiError(409, "insufficient_stock", "Only %s available from this seller." % available,
                                   fields={"quantity": "Only %s available from this seller." % available},
                                   extra={"available": available, "offerId": PLATFORM_OFFER,
                                          "alternatives": [self.offer_card(o) for o in self.offers(tmpl)
                                                           if o.available_qty() > 0]})
            return
        offer = offer or self.offers(tmpl)[:1]
        if not offer:  # sold by the platform itself: its own stock, when tracked
            available = self.platform_qty(tmpl) if self.platform_sells() else None
            if available is not None and quantity > available:
                raise AuthApiError(409, "insufficient_stock", "Only %s available." % available,
                                   fields={"quantity": "Only %s available." % available},
                                   extra={"available": available, "alternatives": []})
            return
        available = int(offer.available_qty())
        if quantity <= available:
            return
        others = [self.offer_card(o) for o in self.offers(tmpl) if o != offer]
        own = self.platform_card(tmpl)
        if own:
            others.append(own)
        raise AuthApiError(409, "insufficient_stock", "Only %s available from this seller." % available,
                           fields={"quantity": "Only %s available from this seller." % available},
                           extra={"available": available, "offerId": str(offer.id),
                                  "alternatives": [o for o in others if o["available"] > 0]})

    def api_sources(self):
        """Storefront code -> source, for the active sources offered as order sources."""
        return {r.api_code: r for r in self.env["leaders.source"].sudo().search([("api_code", "!=", False)])}

    def _sources(self, t):
        if t.leaders_kind != "part":
            return []
        # the project guide: one source per product (the single "Source" field); "sources" stays a list
        return [c for c in t.leaders_source_id.mapped("api_code") if c]

    def _specs(self, t):
        specs = {}
        unit = t.leaders_capacity_unit
        # litre (oil volume) is a backend-only unit: it is not a power rating, so the storefront "power" spec skips it
        if t.leaders_capacity and unit and unit in UNIT_LABELS:
            labels = dict(UNIT_LABELS, **UNIT_LABELS_AR) if self._arabic() else UNIT_LABELS
            specs["power"] = "%s %s" % (fmt_num(t.leaders_capacity), labels.get(unit, unit))
        if t.leaders_kva:
            specs["kva"] = t.leaders_kva
        if t.leaders_prime_power:
            specs["primePower"] = t.leaders_prime_power
        if t.leaders_engine:
            specs["engine"] = t.leaders_engine
        if t.leaders_engine_size:
            specs["engineSize"] = "%s L" % fmt_num(t.leaders_engine_size)
        if t.leaders_energy_id:
            specs["powerSource"] = t.leaders_energy_id.code
        if t.leaders_hours:
            specs["runningHours"] = "{:,}".format(t.leaders_hours)
        if t.leaders_condition:
            specs["condition"] = t.leaders_condition
        if t.leaders_city:
            specs["location"] = t.leaders_city
        if t.leaders_kind == "part":
            if t.leaders_tag_ids:
                specs["partType"] = t.leaders_tag_ids[0].name
            names = ["%s %s" % (m.brand_id.name, m.name) if m.brand_id else m.name
                     for m in t.leaders_compatible_model_ids]
            if names:
                specs["compatibility"] = ", ".join(names)
            specs["origin"] = "original" if t.leaders_source_id.is_original else "aftermarket"
        lines = self._spec_lines(t)
        for key, name in (("dimensions", "dimensions"), ("thread", "thread"), ("changeinterval", "changeInterval")):
            if lines.get(key):
                specs[name] = lines[key]
        if self._arabic():
            match = INTERVAL_RE.match(specs.get("changeInterval", ""))
            if match:
                specs["changeInterval"] = "كل %s ساعة" % match.group(1)
        if t.leaders_warranty_months:
            months = t.leaders_warranty_months
            if self._arabic():  # Arabic plural: 3 to 10 take the plural, the rest the singular
                specs["warranty"] = "%s %s" % (months, "أشهر" if 3 <= months <= 10 else "شهر")
            else:
                specs["warranty"] = "%s months" % months
        # the guide (page 19): who gives the warranty and what it covers show clearly on the product card
        if t.leaders_warranty_provider:
            labels = {"vendor": ("Vendor", "المورّد"), "manufacturer": ("Manufacturer", "الشركة المصنّعة"),
                      "platform": ("Platform", "المنصة")}
            specs["warrantyBy"] = self._label(labels.get(t.leaders_warranty_provider, (t.leaders_warranty_provider,) * 2))
        if t.leaders_warranty_scope and t.leaders_warranty_scope.strip():
            specs["warrantyScope"] = t.leaders_warranty_scope.strip()
        return specs

    def _arabic(self):
        return (self.env.lang or "").startswith("ar")

    # ------------------------------------------------------------- filters
    def _range(self, field, lo, hi, domain):
        domain.append((field, ">", 0))
        if lo is not None:
            domain.append((field, ">=", lo))
        if hi is not None:
            domain.append((field, "<=", hi))

    def _brand_ids(self, names):
        ids = []
        Brand = self.env["leaders.brand"].sudo()
        for name in names:
            # the brand's name in the request's language or in English: a link made on the English site keeps
            # working on the Arabic one once the brands carry Arabic names
            ids += Brand.search([("name", "=ilike", name)]).ids
            ids += Brand.with_context(lang="en_US").search([("name", "=ilike", name)]).ids
        return list(dict.fromkeys(ids)) or [0]

    def _ids(self, params, key, errors):
        """The comma list of record ids in ``params[key]``: None when absent, 422 when not digits."""
        values = csv_list(params.get(key))
        if not values:
            return None
        if not all(v.isdigit() for v in values):
            errors[key] = "Must be a comma-separated list of ids."
            return None
        return [int(v) for v in values]

    def _live_ids(self, model, ids, extra=None):
        """The ids that exist and are active; an unknown or archived id matches nothing."""
        return self.env[model].sudo().search([("id", "in", ids)] + (extra or [])).ids

    def narrow_domain(self, params, errors):
        """The part of the filter shared with the facets: kind, subcategory, q, listing."""
        domain = self.base_domain()
        kind = params.get("kind")
        if kind:
            if kind not in KINDS:
                errors["kind"] = "Unknown kind."
            else:
                domain.append(("leaders_kind", "=", kind))
        category_ids = self._ids(params, "categoryId", errors)
        if category_ids is not None:
            # a category matches its sub-categories too; the old slug forms sent alongside are OR-ed in
            live = self._live_ids("product.public.category", category_ids)
            clauses = [[("public_categ_ids", "child_of", live)] if live else [("id", "=", 0)]]
            if params.get("subcategory"):
                clauses.append([("public_categ_ids.leaders_slug", "=", params["subcategory"])])
            if csv_list(params.get("section")):
                clauses.append([("public_categ_ids.leaders_slug", "in", csv_list(params.get("section")))])
            domain += expression.OR(clauses)
        elif params.get("subcategory"):
            domain.append(("public_categ_ids.leaders_slug", "=", params["subcategory"]))
        query = (params.get("q") or "").strip()
        if len(query) > MAX_QUERY:
            errors["q"] = "q can be at most %s characters." % MAX_QUERY
        elif query:
            # every word must appear somewhere in the stored search text (names, descriptions, categories,
            # brand, model, SKU and specs values, Arabic and English)
            for word in normalize_search(query).split(" "):
                if word:
                    domain.append(("leaders_search_text", "ilike", word))
        months = params.get("rentMonths")
        if months:
            if not str(months).isdigit():
                errors["rentMonths"] = "Must be a whole number of months."
            elif int(months) > 24:  # rentals over two years are for new equipment only
                domain.append(("leaders_condition", "=", "new"))
        listing = params.get("listing")
        if listing:
            if listing not in LISTINGS:
                errors["listing"] = "Must be buy or rent."
            elif listing == "rent":
                domain.append(("leaders_rentable", "=", True))
            else:
                # rent-only categories never appear under "buy"
                rent_only = self.Category.search([("leaders_slug", "!=", False)]).filtered(
                    lambda c: not c._leaders_effective().leaders_can_buy)
                if rent_only:
                    domain.append(("public_categ_ids", "not in", rent_only.ids))
        return domain

    def full_domain(self, params, errors):
        domain = self.narrow_domain(params, errors)
        pmin, pmax = number(params.get("priceMin")), number(params.get("priceMax"))
        if pmin is not None or pmax is not None:
            self._range("leaders_display_price", pmin, pmax, domain)
        emin, emax = number(params.get("engineMin")), number(params.get("engineMax"))
        if emin is not None or emax is not None:
            self._range("leaders_engine_size", emin, emax, domain)
        kmin, kmax = number(params.get("kvaMin")), number(params.get("kvaMax"))
        band = params.get("power")
        if band:
            if band not in POWER_BANDS:
                errors["power"] = "Unknown power band."
            elif kmin is None and kmax is None:
                kmin, kmax = POWER_BANDS[band]
        if kmin is not None or kmax is not None:
            self._range("leaders_kva", kmin, kmax, domain)
        values = csv_list(params.get("condition"))
        if any(v not in CONDITIONS for v in values):
            errors["condition"] = "Unknown value."
        elif values:
            domain.append(("leaders_condition", "in", values))
        # each filter takes the old value form, the record ids (xxxId) or both (OR-ed)
        values = csv_list(params.get("powerSource"))
        energy_ids = self._ids(params, "powerSourceId", errors)
        clauses = []
        if values:
            known = set(self.env["leaders.energy.source"].sudo().search([]).mapped("code"))
            if any(v not in known for v in values):
                errors["powerSource"] = "Unknown value."
            else:
                clauses.append([("leaders_energy_id.code", "in", values)])
        if energy_ids is not None:
            clauses.append([("leaders_energy_id", "in", self._live_ids("leaders.energy.source", energy_ids) or [0])])
        if clauses:
            domain += expression.OR(clauses)
        brands = csv_list(params.get("brand"))
        brand_ids = self._ids(params, "brandId", errors)
        if brands or brand_ids is not None:
            ids = (self._brand_ids(brands) if brands else []) + (
                self._live_ids("leaders.brand", brand_ids) if brand_ids else [])
            domain.append(("leaders_brand_id", "in", ids or [0]))
        sections = csv_list(params.get("section"))
        if sections and not params.get("categoryId"):  # with categoryId it is OR-ed in narrow_domain
            domain.append(("public_categ_ids.leaders_slug", "in", sections))
        sources = csv_list(params.get("source"))
        source_ids = self._ids(params, "sourceId", errors)
        clauses = []
        if sources:
            if any(v not in self.api_sources() for v in sources):
                errors["source"] = "Unknown value."
            else:
                clauses += [[("leaders_source_id.api_code", "in", sources)]]
        if source_ids is not None:
            # only the sources offered as order sources (the ones Product.sources lists)
            ids = self._live_ids("leaders.source", source_ids, [("api_code", "!=", False)]) or [0]
            clauses += [[("leaders_source_id", "in", ids)]]
        if clauses:
            domain += expression.OR(clauses)
        hours = csv_list(params.get("hours"))
        if hours:
            if any(v not in HOURS_BANDS for v in hours):
                errors["hours"] = "Unknown value."
            else:
                clauses = []
                for v in hours:
                    lo, hi = HOURS_BANDS[v]
                    clause = [("leaders_hours", ">", 0)]
                    if lo is not None:
                        clause.append(("leaders_hours", ">=", lo))
                    if hi is not None:
                        clause.append(("leaders_hours", "<=", hi))
                    clauses.append(clause)
                domain += expression.OR(clauses)
        if params.get("location"):
            domain.append(("leaders_city", "=", params["location"]))
        ymin, ymax = params.get("yearMin"), params.get("yearMax")
        for key, value in (("yearMin", ymin), ("yearMax", ymax)):
            if value not in (None, "") and not str(value).isdigit():
                errors[key] = "Must be a year, e.g. 2015."
        if (ymin or ymax) and not errors.get("yearMin") and not errors.get("yearMax"):
            domain.append(("leaders_year", ">", 0))
            if ymin:
                domain.append(("leaders_year", ">=", int(ymin)))
            if ymax:
                domain.append(("leaders_year", "<=", int(ymax)))
        families = csv_list(params.get("family"))
        if families:
            model_ids = self._family_ids(families)
            domain += expression.OR([[("leaders_model_id", "in", model_ids)],
                                     [("leaders_compatible_model_ids", "in", model_ids)]])
        bands = csv_list(params.get("warranty"))
        if bands:
            if any(v not in WARRANTY_BANDS for v in bands):
                errors["warranty"] = "Unknown value. One of: %s." % ", ".join(WARRANTY_BANDS)
            else:
                clauses = []
                for v in bands:
                    lo, hi = WARRANTY_BANDS[v]
                    if v == "none":
                        clauses.append(["|", ("leaders_warranty_months", "=", 0), ("leaders_warranty_months", "=", False)])
                        continue
                    clause = [("leaders_warranty_months", ">=", lo)]
                    if hi is not None:
                        clause.append(("leaders_warranty_months", "<=", hi))
                    clauses.append(clause)
                domain += expression.OR(clauses)
        return domain

    def _family_ids(self, values):
        """The machine models a ``family`` filter names, by key (normalized name) or name; [0] when none."""
        from ..models.catalog_spec import normalize_model_name
        keys = {normalize_model_name(v) for v in values}
        return self.env["leaders.equipment.model"].sudo().search([("key", "in", list(keys))]).ids or [0]

    @staticmethod
    def _families(product):
        """The model families a product belongs to: its model (equipment) or the models it fits (parts)."""
        return product.leaders_model_id or product.leaders_compatible_model_ids

    def ordered_ids(self, domain, sort):
        Template = self.Template
        if sort in ("brandAsc", "brandDesc"):
            order = "leaders_brand_id %s, id" % ("asc" if sort == "brandAsc" else "desc")
            return Template.search(domain, order=order).ids
        if sort in ("priceAsc", "priceDesc"):
            direction = "asc" if sort == "priceAsc" else "desc"
            priced = Template.search(domain + [("leaders_display_price", ">", 0)],
                                     order="leaders_display_price %s, id" % direction).ids
            unpriced = Template.search(domain + [("leaders_display_price", "<=", 0)], order="id").ids
            return priced + unpriced
        return Template.search(domain, order="marketplace_publish_date desc, id desc").ids

    def listing(self, params):
        errors = {}
        featured_on = params.get("featured") in ("true", "1")
        # featured=true pages like a kind listing ({items, totalCount, page, pageSize}) and needs no kind. The older
        # landing call (featured=true with limit, no page / pageSize) keeps answering a plain list.
        featured_paged = featured_on and (not params.get("limit") or bool(params.get("page")) or bool(params.get("pageSize")))
        landing = (bool(params.get("featured")) or bool(params.get("limit"))) and not featured_paged
        # the global search (q alone) looks across every kind; any other listing names a kind or a category
        if not landing and not featured_paged and not params.get("kind") and not params.get("categoryId") \
                and not (params.get("q") or "").strip():
            errors["kind"] = "kind is required."
        sort = params.get("sort") or "newest"
        if sort not in SORTS:
            errors["sort"] = "Unknown sort."
        page_size = int(number(params.get("pageSize")) or 8) if params.get("pageSize") else 8
        page = max(int(number(params.get("page")) or 1), 1)
        if page_size > MAX_PAGE_SIZE or page_size < 1:
            errors["pageSize"] = "pageSize must be between 1 and %s." % MAX_PAGE_SIZE
        domain = self.full_domain(params, errors)
        if errors:
            raise validation_error(errors)
        if landing:
            limit = min(max(int(number(params.get("limit")) or 8), 1), 100)
            if params.get("featured") in ("true", "1"):
                featured = self.Template.search(domain + [("leaders_featured", "=", True)],
                                                order="marketplace_publish_date desc, id desc", limit=limit)
                rows = featured or self.Template.browse(self.ordered_ids(domain, "newest")[:limit])
            else:
                rows = self.Template.browse(self.ordered_ids(domain, sort)[:limit])
            return [self.product(t) for t in rows]
        if featured_paged:
            # the flagged products; when none is flagged, the newest ones (the same fallback as the landing list)
            ids = self.ordered_ids(domain + [("leaders_featured", "=", True)], sort) or self.ordered_ids(domain, "newest")
            chunk = self.Template.browse(ids[(page - 1) * page_size: page * page_size])
            return {"items": [self.product(t) for t in chunk], "totalCount": len(ids), "page": page, "pageSize": page_size}
        ids = self.ordered_ids(domain, sort)
        log = None
        if page == 1:  # one log row per search, not per page turned
            log = self.env["leaders.search.log"].leaders_record(params, len(ids), self.env.lang)
        chunk = self.Template.browse(ids[(page - 1) * page_size: page * page_size])
        out = {"items": [self.product(t) for t in chunk], "totalCount": len(ids), "page": page, "pageSize": page_size}
        if log:  # optional: sent back by POST /products/search-click when a result is opened
            out["searchId"] = str(log.id)
        return out

    # -------------------------------------------------------------- facets
    def facets(self, params):
        errors = {}
        domain = self.narrow_domain({k: params.get(k) for k in ("kind", "subcategory", "q", "listing")}, errors)
        if errors:
            raise validation_error(errors)
        rows = self.Template.search(domain)

        def count(values):
            out = {}
            for v in values:
                out[v] = out.get(v, 0) + 1
            return out

        brands = count(r.leaders_brand_id.name for r in rows if r.leaders_brand_id)
        conditions = count(r.leaders_condition for r in rows if r.leaders_condition)
        powers = count(r.leaders_energy_id.code for r in rows if r.leaders_energy_id)
        sections = count(self.subcategory(r).leaders_slug for r in rows if self.subcategory(r).leaders_slug)
        sources = count(s for r in rows for s in self._sources(r))
        hours = {}
        for band, (lo, hi) in HOURS_BANDS.items():
            n = sum(1 for r in rows if r.leaders_hours > 0 and (lo is None or r.leaders_hours >= lo)
                    and (hi is None or r.leaders_hours <= hi))
            if n:
                hours[band] = n
        ranges = {}
        for key, field, whole in (("price", "leaders_display_price", True), ("kva", "leaders_kva", False),
                                  ("engine", "leaders_engine_size", False)):
            values = [r[field] for r in rows if r[field] > 0]
            if values:
                lo, hi = min(values), max(values)
                ranges[key] = ({"min": math.floor(lo), "max": math.ceil(hi)} if whole else {"min": lo, "max": hi})
        return {
            "brands": [{"value": k, "count": v} for k, v in sorted(brands.items())],
            "conditions": [{"value": k, "count": v} for k, v in conditions.items()],
            "sections": [{"value": k, "count": v} for k, v in sections.items()],
            "powerSources": [{"value": k, "count": v} for k, v in powers.items()],
            "sources": [{"value": k, "count": v} for k, v in sources.items()],
            "hours": [{"value": k, "count": v} for k, v in hours.items()],
            "ranges": ranges,
        }

    # ------------------------------------------------------------ filters
    def _label(self, labels):
        return labels[1] if self._arabic() else labels[0]

    def _ranges(self, rows_for):
        ranges = {}
        for key, field, whole in (("price", "leaders_display_price", True), ("kva", "leaders_kva", False),
                                  ("engine", "leaders_engine_size", False)):
            values = [r[field] for r in rows_for(key) if r[field] > 0]
            if values:
                lo, hi = min(values), max(values)
                ranges[key] = ({"min": math.floor(lo), "max": math.ceil(hi)} if whole else {"min": lo, "max": hi})
        return ranges

    def _with_logos(self, options):
        """Brand options get an optional ``logo`` URL when the brand has one."""
        brands = self.env["leaders.brand"].sudo().browse([int(o["id"]) for o in options])
        with_logo = set(brands.filtered("logo").ids)
        for option in options:
            if int(option["id"]) in with_logo:
                option["logo"] = "%s/brands/%s/logo" % (self.base_url, option["id"])
        return options

    def _new_filter_groups(self, rows_for):
        """``year`` (range), ``family`` (model series) and ``warranty`` (bands); each omitted when empty."""
        out = {}
        years = [r.leaders_year for r in rows_for("year") if r.leaders_year > 0]
        if years:
            out["year"] = {"min": min(years), "max": max(years)}
        families = {}
        for r in rows_for("family"):
            for model in self._families(r):
                families[model] = families.get(model, 0) + 1
        if families:
            out["family"] = sorted(({"value": m.key or str(m.id), "label": m.name, "count": n}
                                    for m, n in families.items()), key=lambda o: o["label"].casefold())
        rows = rows_for("warranty")
        if rows:
            counts = dict.fromkeys(WARRANTY_BANDS, 0)
            for r in rows:
                months = r.leaders_warranty_months or 0
                for band, (lo, hi) in WARRANTY_BANDS.items():
                    if months >= lo and (hi is None or months <= hi):
                        counts[band] += 1
                        break
            out["warranty"] = [{"value": band, "count": n} for band, n in counts.items()]
        return out

    def _shelf_categories(self, kind):
        """The product shelves' categories, each top category followed by its sub-categories."""
        tops = self.Category.search([("parent_id", "=", False), ("leaders_slug", "!=", False),
                                     ("leaders_hidden", "=", False), ("leaders_group", "in", list(GROUP_KINDS))],
                                    order="sequence, id")
        out = []

        def walk(categ, shelf):
            out.append((categ, shelf))
            for child in categ.child_id.filtered(lambda c: c.leaders_slug and not c.leaders_hidden).sorted(
                    lambda c: (c.sequence, c.id)):
                walk(child, shelf)
        for top in tops:
            shelf = GROUP_KINDS[top.leaders_group]
            if not kind or kind == shelf:
                walk(top, shelf)
        return out

    def filters(self, params):
        """GET /products/filters: every option of every product filter with an id, a label and a count.

        Each group is counted with every filter of the request except its own (standard faceting)."""
        params = {k: v for k, v in params.items() if k not in ("page", "pageSize", "sort")}
        errors = {}
        self.full_domain(params, errors)
        if errors:
            raise validation_error(errors)
        cache = {}

        def rows_for(group):
            if group not in cache:
                drop = FILTER_GROUP_PARAMS[group]
                cache[group] = self.Template.search(
                    self.full_domain({k: v for k, v in params.items() if k not in drop}, {}))
            return cache[group]

        def counted(group, key):
            out = {}
            for r in rows_for(group):
                for rec in key(r):
                    out[rec] = out.get(rec, 0) + 1
            return sorted(({"id": str(rec.id), "label": rec.name or "", "count": n} for rec, n in out.items()),
                          key=lambda o: (o["label"].casefold(), int(o["id"])))

        kind = params.get("kind") if params.get("kind") in KINDS else None
        # the shelves in play: the kind, else those of the requested categories, else all three
        shelves = {kind} if kind else set()
        if not kind and params.get("categoryId"):
            shelves = {GROUP_KINDS.get(c._leaders_top().leaders_group)
                       for c in self.Category.browse(self._ids(params, "categoryId", {})).exists()}
        parts_only = shelves == {"part"}

        categories = []
        products = rows_for("categories")
        product_categs = [set(p.public_categ_ids.ids) for p in products]
        for categ, shelf in self._shelf_categories(kind):
            subtree = set(self.Category.search([("id", "child_of", categ.id)]).ids)
            categories.append({"id": str(categ.id), "slug": categ.leaders_slug, "kind": shelf,
                               "parentId": str(categ.parent_id.id) if categ.parent_id else None,
                               "label": categ.name or "", "count": sum(1 for ids in product_categs if ids & subtree)})

        rest = {k: v for k, v in params.items() if k != "listing"}
        listings = [{"id": code, "label": self._label(LISTING_LABELS[code]),
                     "count": self.Template.search_count(self.full_domain(dict(rest, listing=code), {}))}
                    for code in (("buy",) if parts_only else LISTINGS)]

        by_condition = {}
        for r in rows_for("conditions"):
            by_condition[r.leaders_condition] = by_condition.get(r.leaders_condition, 0) + 1
        conditions = [{"id": code, "label": self._label(CONDITION_LABELS[code]), "count": by_condition.get(code, 0)}
                      for code in CONDITIONS]

        hours = []
        if not parts_only:
            for band, (lo, hi) in HOURS_BANDS.items():
                n = sum(1 for r in rows_for("hours") if r.leaders_hours > 0 and (lo is None or r.leaders_hours >= lo)
                        and (hi is None or r.leaders_hours <= hi))
                hours.append({"id": band, "label": self._label(HOURS_LABELS[band]), "count": n})

        def sources(r):
            if r.leaders_kind != "part":
                return []
            return r.leaders_source_id.filtered(lambda src: src.api_code and src.active)

        return {
            "categories": categories,
            "listings": listings,
            "conditions": conditions,
            "brands": self._with_logos(counted("brands", lambda r: r.leaders_brand_id.filtered("active"))),
            "powerSources": counted("powerSources", lambda r: r.leaders_energy_id.filtered("active")),
            "sources": counted("sources", sources),
            "hours": hours,
            "ranges": self._ranges(rows_for),
            **self._new_filter_groups(rows_for),
        }

    # ---------------------------------------------------------- categories
    def categories(self):
        out = []
        counts = {}
        for kind_rows in self.Template.read_group(self.base_domain(), ["id:count"], ["public_categ_ids"], lazy=False):
            if kind_rows["public_categ_ids"]:
                counts[kind_rows["public_categ_ids"][0]] = kind_rows["__count"]
        # a services tile opens the one published service it holds and carries that service's name
        tile_names, tile_services = {}, {}
        for tmpl in self.Template.search(self.service_domain(), order="sequence, id"):
            tile_names.setdefault(tmpl._leaders_service_tile().id, tmpl.name)
            tile_services.setdefault(tmpl._leaders_service_tile().id, tmpl)
        for categ in self.Category.search([("leaders_hidden", "=", False), ("leaders_slug", "!=", False)],
                                          order="sequence, id"):
            top = categ._leaders_top()
            group = top.leaders_group or "parts"
            if group == "services" and categ.parent_id and categ.id not in tile_names:
                continue  # no published service behind the tile: it would open a 404
            href = "/%s" % categ.leaders_slug if not categ.parent_id else "/%s/%s" % (top.leaders_slug, categ.leaders_slug)
            if group == "services" and categ.id in tile_services:  # a services tile opens its service's page
                href = "/%s/%s" % (top.leaders_slug, tile_services[categ.id].leaders_slug)
            total = counts.get(categ.id, 0) + sum(counts.get(c.id, 0) for c in self.Category.search([("id", "child_of", categ.id)]) - categ)
            tile = {"id": str(categ.id), "slug": categ.leaders_slug, "group": group, "href": href,
                    "labelKey": categ.leaders_slug, "productCount": total, "tone": TONES.get(group, "soft"),
                    "parentSlug": categ.parent_id._leaders_top().leaders_slug if categ.parent_id else None}
            tile["name"] = tile_names.get(categ.id) or categ.name
            if categ.leaders_description:
                tile["description"] = categ.leaders_description
            if categ.image_1920:
                tile["image"] = self.url("product.public.category", categ.id)
            elif categ.id in tile_services and tile_services[categ.id].image_1920:
                # a services tile without its own picture shows its service's picture
                tile["image"] = self.url("product.template", tile_services[categ.id].id)
            if categ.leaders_icon:
                tile["icon"] = self.url("product.public.category", categ.id, "leaders_icon")
            if categ.leaders_delivery_surcharge > 0:  # optional: added once to the delivery of an order with it
                tile["deliverySurcharge"] = minor(categ.leaders_delivery_surcharge)
            out.append(tile)
        return out

    # ------------------------------------------------------------ services
    def service_domain(self):
        return [("leaders_service_page", "=", True), ("is_published", "=", True)]

    def services(self):
        return [self.service_summary(t) for t in self.Template.search(self.service_domain(), order="sequence, id")]

    @staticmethod
    def service_icon(tmpl):
        """``(record, field)`` holding the icon: the service's own, else its landing tile's; empty when none."""
        if tmpl.leaders_service_icon:
            return tmpl, "leaders_service_icon"
        tile = tmpl._leaders_service_tile() if tmpl else tmpl
        if tile and tile.leaders_icon:
            return tile, "leaders_icon"
        return None, None

    def service_summary(self, tmpl):
        out = {"id": str(tmpl.id), "slug": tmpl.leaders_slug, "name": tmpl.name,
               "summary": tmpl.leaders_service_summary or "", "mandatoryOnPurchase": tmpl.leaders_service_mandatory}
        vendor = tmpl.leaders_service_vendor_id.commercial_partner_id if "leaders_service_vendor_id" in tmpl._fields else None
        if vendor and not vendor._leaders_name_hidden():  # optional: the vendor of a vendor's service
            out["provider"] = {"name": vendor.vendor_store_name or vendor.name or ""}
        if self.service_icon(tmpl)[0]:  # one URL whatever holds the icon; /web/image would refuse the product
            out["icon"] = "%s/services/%s/icon" % (self.base_url, tmpl.leaders_slug)
        if tmpl.leaders_service_request_title:
            out["requestTitle"] = tmpl.leaders_service_request_title
        if tmpl.leaders_service_coverage:
            out["coverageGovernorates"] = tmpl.leaders_service_coverage
        return out

    def service(self, tmpl):
        out = self.service_summary(tmpl)
        out.update({
            "lead": tmpl.leaders_service_lead or "", "panelTitle": tmpl.leaders_service_panel_title or "",
            "requestTitle": tmpl.leaders_service_request_title or "",  # always present on the details page
            "checklistTitle": tmpl.leaders_service_checklist_title or "",
            "checklistLead": tmpl.leaders_service_checklist_lead or "",
            "checklist": tmpl.leaders_service_checklist_ids.mapped("name"),
            "deliverable": tmpl.leaders_service_deliverable or "",
            "deliverableShort": tmpl.leaders_service_deliverable_short or "",
        })
        # scope of the service: optional keys, omitted when the admin left them empty
        for key, text in (("includes", tmpl.leaders_service_includes), ("excludes", tmpl.leaders_service_excludes)):
            lines = [line.strip(" -•\t") for line in (text or "").splitlines() if line.strip(" -•\t")]
            if lines:
                out[key] = lines
        if tmpl.leaders_service_warranty_months > 0:
            out["warrantyMonths"] = tmpl.leaders_service_warranty_months
        return out

    # ------------------------------------------------------------- shelves
    def related(self, tmpl, limit):
        domain = self.base_domain() + [("id", "!=", tmpl.id), ("leaders_kind", "=", tmpl.leaders_kind)]
        same_sub = self.Template.search(
            domain + [("public_categ_ids", "in", self.subcategory(tmpl).ids)], order="marketplace_publish_date desc, id desc",
            limit=limit)
        rest = self.Template.search(domain + [("id", "not in", same_sub.ids)],
                                    order="marketplace_publish_date desc, id desc", limit=limit - len(same_sub))
        return [self.product(t) for t in (same_sub + rest)[:limit]]

    def bought_together(self, tmpl, limit):
        """Priced parts that fit: the same brand first, then those whose compatibility names the brand."""
        parts = self.base_domain() + [("leaders_kind", "=", "part"), ("leaders_display_price", ">", 0), ("id", "!=", tmpl.id)]
        order = "marketplace_publish_date desc, id desc"
        picked = self.Template.browse()
        if tmpl.leaders_brand_id:
            picked = self.Template.search(parts + [("leaders_brand_id", "=", tmpl.leaders_brand_id.id)],
                                          order=order, limit=limit)
        named = []
        if tmpl.leaders_brand_id:
            named.append([("leaders_compatible_model_ids.brand_id", "=", tmpl.leaders_brand_id.id)])
        if tmpl.leaders_model_id:
            named.append([("leaders_compatible_model_ids", "in", tmpl.leaders_model_id.ids)])
        if named and len(picked) < limit:
            picked |= self.Template.search(
                parts + expression.OR(named) + [("id", "not in", picked.ids)], order=order, limit=limit - len(picked))
        return [self.product(t) for t in picked[:limit]]
