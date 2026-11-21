"""Equipment and spare-parts import sheets (the two sheets of the project brief).

The columns are the Arabic titles of the sheets, mapped to column keys, so a vendor
uploads the sheet as it is. Rules are those of the brief: brands come from the
admin-managed list, a used product needs year and hours, a rental needs a rentable
subcategory, serial numbers are unique on the platform, the free-text description
carries every specification that is not a filter.
"""
import uuid
from urllib.parse import urlparse

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import vendor_import as base
from .catalog_spec import normalize_model_name

KINDS = ("equipment", "spare_parts", "products")
# "products": the one sheet of the project guide for every product; each row is an equipment or a spare part
# by its subcategory, and its images must be more than one
MIN_IMAGES = {"equipment": 3, "spare_parts": 1, "products": 2}

_AR = {
    "اسم المنتج (عربي)": "name", "اسم المنتج (إنجليزي)": "name_en",
    "اسم القطعة (عربي)": "name", "اسم القطعة (إنجليزي)": "name_en",
    "الفئة الفرعية": "category", "الماركة": "brand", "الموديل": "model", "المصدر": "origin",
    "الحالة": "condition", "القدرة أو السعة": "capacity", "وحدة القدرة": "capacity_unit",
    "مصدر الطاقة": "energy", "السعر": "price", "سنة الصنع": "year", "ساعات التشغيل": "hours",
    "الرقم التسلسلي": "serial", "مدة الضمان (شهور)": "warranty_months", "متاح للإيجار": "rentable",
    "سعر الإيجار لليوم": "rent_price_day", "رابط فيديو التشغيل": "video_url", "روابط الصور": "image_urls",
    "الوصف (عربي)": "description", "الوصف (إنجليزي)": "description_en",
    "التاجز (Tags)": "tags", "رقم القطعة": "part_number", "الأرقام البديلة": "alt_numbers",
    "المعدات المتوافقة": "compatible",
    "المحرك": "engine", "سعة المحرك (لتر)": "engine_size", "القدرة المستمرة": "prime_power", "المدينة": "city", "مصادر الطلب": "sources",
    "الأسماء المشابهة": "similar_names",
    # the unified sheet of the project guide
    "الموديل (العائلة)": "family", "التاجز": "tags", "للبيع أم للإيجار": "listing_type", "صور المنتج": "image_urls",
    "الضمان مقدم من": "warranty_provider", "نطاق الضمان": "warranty_scope", "صورة العداد": "meter_photo_url",
    "صورة لوحة البيانات": "plate_photo_url",
    "الكمية": "initial_qty", "كود المنتج (SKU)": "sku", "كود المنتج": "sku",
}
base.HEADER_ALIASES.update({base._norm_header(k): v for k, v in _AR.items()})

_EQUIPMENT = [
    ("name", True), ("name_en", False), ("category", True), ("brand", True), ("model", True),
    ("origin", True), ("condition", True), ("capacity", True), ("capacity_unit", True),
    ("energy", False), ("price", False), ("year", False), ("hours", False), ("serial", False),
    ("warranty_months", True), ("rentable", True), ("rent_price_day", False), ("video_url", False),
    ("image_urls", True), ("description", True), ("description_en", False),
    ("engine", False), ("engine_size", False), ("prime_power", False), ("city", False), ("similar_names", False),
    ("warranty_provider", False), ("warranty_scope", False), ("meter_photo_url", False), ("plate_photo_url", False),
    ("initial_qty", False), ("sku", False)]
_PARTS = [
    ("name", True), ("name_en", False), ("category", True), ("tags", True), ("part_number", True),
    ("alt_numbers", False), ("sources", False), ("compatible", True), ("brand", True), ("origin", True), ("condition", True),
    ("price", True), ("warranty_months", False), ("image_urls", True), ("description", True),
    ("description_en", False), ("similar_names", False), ("initial_qty", False), ("sku", False)]
_PRODUCTS = [
    ("name", True), ("name_en", True), ("category", True), ("brand", True), ("family", True), ("tags", False),
    ("origin", True), ("condition", True), ("capacity", False), ("capacity_unit", False), ("energy", False),
    ("price", False), ("serial", False), ("year", False), ("hours", False), ("warranty_months", False),
    ("listing_type", True), ("rent_price_day", False), ("video_url", False), ("image_urls", True),
    ("description", True), ("description_en", True), ("similar_names", False),
    ("warranty_provider", False), ("warranty_scope", False), ("meter_photo_url", False), ("plate_photo_url", False),
    ("initial_qty", False), ("sku", False)]
base.COLUMNS["products"] = _PRODUCTS
base.EXAMPLES["products"] = [
    "مولد ديزل 100 ك.ف.أ", "Silent Diesel Generator 100 kVA", "مولدات كهربائية", "Perkins", "P110-5", "مولد صامت",
    "أوريجنال", "جديد", "100", "KVA", "ديزل", "850000", "PK-EXAMPLE", "", "", "24", "للبيع", "",
    "https://example.com/v.mp4", "https://example.com/1.jpg, https://example.com/2.jpg", "وصف المولد",
    "Generator description", "genset, مولد ديزل", "manufacturer", "Engine and alternator, 2 years", "", "", "3", "GEN-100-01"]
base.COLUMNS["equipment"] = _EQUIPMENT
base.COLUMNS["spare_parts"] = _PARTS
base.EXAMPLES["equipment"] = [
    "Silent Diesel Generator 100 KVA", "", "Electric Generators", "Perkins", "P110-5", "original", "new",
    "100", "kva", "diesel", "850000", "", "", "PK-EXAMPLE", "24", "yes", "4500", "https://example.com/v.mp4",
    "https://example.com/1.jpg, https://example.com/2.jpg, https://example.com/3.jpg", "Specs here", "",
    "Perkins 1506A", "8.8", "90 kVA", "cairo", "diesel genset, مولد ديزل", "manufacturer", "Engine and alternator", "", "",
    "3", "GEN-100-01"]
base.EXAMPLES["spare_parts"] = [
    "Engine Oil Filter", "", "Filters", "oil filter, 93mm", "LF9009", "", "", "P110-5, P150-3", "Fleetguard",
    "original", "new", "320", "6", "https://example.com/1.jpg", "Specs here", "", "oil filter, فلتر زيت",
    "25", "LF9009-01"]

_CONDITION = {"new": "new", "جديد": "new", "used": "used", "مستعمل": "used",
              "refurbished": "refurbished", "مجدد": "refurbished"}
_UNIT = {"kva": "kva", "kw": "kw", "ton": "ton", "طن": "ton", "m3": "m3", "متر مكعب": "m3",
         "amp": "amp", "أمبير": "amp", "امبير": "amp", "m": "m", "متر": "m", "l": "l", "لتر": "l"}
_YES = {"yes": True, "y": True, "نعم": True, "true": True, "1": True,
        "no": False, "n": False, "لا": False, "false": False, "0": False, "": False}


def _pick(mapping, text):
    return mapping.get((text or "").strip().lower())


def _split(text, separators=",،;"):
    for sep in separators[1:]:
        text = text.replace(sep, separators[0])
    return [x.strip() for x in text.split(separators[0]) if x.strip()]


def _http_url(text):
    parsed = urlparse(text)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


class VendorImportCatalog(models.Model):
    _inherit = "leaders.vendor.import"

    kind = fields.Selection(selection_add=[
        ("equipment", "Equipment sheet"), ("spare_parts", "Spare parts sheet"), ("products", "All products (one sheet)")],
        ondelete={"equipment": "cascade", "spare_parts": "cascade", "products": "cascade"})

    def _leaders_is_example_row(self, raw):
        return self.kind in KINDS and "example.com" in (raw.get("image_urls") or "")

    def _leaders_instructions(self, kind):
        if kind not in KINDS:
            return super()._leaders_instructions(kind)
        common = [
            _("One row per product. Delete the example rows (their links contain example.com) or leave them: they are skipped."),
            _("brand must be in the approved brand list; ask the admin to add a new brand."),
            _("Write every specification you do not filter by inside the description."),
            _("Every row is checked first. You download the error file, then confirm to import only the valid rows."),
        ]
        if kind == "products":
            return [
                _("One sheet for every product: the subcategory of each row decides if it is equipment or a spare part."),
                _("Model (family): the equipment model; for a spare part, the models it fits separated by commas."),
                _("For sale or rent: للبيع or للإيجار; rent per day is required for rent. At least 2 image links."),
                _("A used product needs year, hours and an operation video."),
                _("The operation video must show: the control panel while running, the hour meter, the data plate "
                  "and the machine running under load."),
            ] + common
        if kind == "equipment":
            return [
                _("category: one of the equipment subcategories. A used product needs year and hours."),
                _("capacity is a number; capacity_unit: kva, kw, ton, m3, amp or m."),
                _("rentable: yes/no; rent_price_day is required when yes. At least 3 image links; an operation video for used products."),
                _("serial must be unique on the platform."),
                _("The operation video must show: the control panel while running, the hour meter, the data plate "
                  "and the machine running under load."),
            ] + common
        return [
            _("category: spare parts, oils or filters. Oils and filters are new only."),
            _("compatible: the equipment models the part fits, separated by commas. The 'choose your equipment' filter relies on it."),
            _("tags: use the exact same spelling in every row; unknown tags are refused."),
        ] + common

    def _leaders_normalize_row(self, kind, raw):
        if kind not in KINDS:
            return super()._leaders_normalize_row(kind, raw)
        if kind == "products":
            return self._leaders_normalize_unified(raw)
        errors, payload = [], {}

        def number(column, required=False, integer=False):
            text = raw.get(column, "")
            if not text:
                if required:
                    errors.append((column, _("%s is required.", column)))
                return None
            try:
                value = self._to_number(text)
            except ValueError:
                errors.append((column, _("%(column)s must be a number, got '%(value)s'.", column=column, value=text)))
                return None
            if value < 0:
                errors.append((column, _("%s cannot be negative.", column)))
                return None
            return int(value) if integer else value

        def choice(column, mapping, required=True):
            text = raw.get(column, "")
            if not text:
                if required:
                    errors.append((column, _("%s is required.", column)))
                return False
            value = _pick(mapping, text)
            if not value:
                errors.append((column, _("%(column)s: '%(value)s' is not one of the allowed values.", column=column, value=text)))
            return value or False

        payload["similar_names"] = (raw.get("similar_names") or "").strip()
        typed_sku = (raw.get("sku") or "").strip()[:64]
        payload["sku"] = typed_sku or (raw.get("serial") or "").strip() or "CAT-" + uuid.uuid4().hex[:10].upper()
        if typed_sku and self.vendor_id and self.env["leaders.marketplace.offer"].sudo().with_context(
                active_test=False).search_count([("vendor_id", "=", self.vendor_id.id), ("seller_sku", "=", typed_sku)]):
            errors.append(("sku", _("The product code %s is already used by another of your products.", typed_sku)))
        payload["initial_qty"] = number("initial_qty", integer=True) or 0
        for column in ("name", "name_en", "category", "brand", "description", "description_en"):
            payload[column] = raw.get(column, "")
        for column in ("name", "category", "brand", "description"):
            if not payload[column]:
                errors.append((column, _("%s is required.", column)))
        payload["price"] = number("price", required=kind == "spare_parts") or 0.0
        payload["origin"] = raw.get("origin", "").strip()
        if not payload["origin"]:
            errors.append(("origin", _("origin is required.")))
        payload["condition"] = choice("condition", _CONDITION)
        payload["warranty_months"] = number("warranty_months", required=kind == "equipment" and not raw.get("_sheet"),
                                            integer=True)
        urls = _split(raw.get("image_urls", ""))
        payload["image_urls"] = urls
        for url in urls:
            if not _http_url(url):
                errors.append(("image_urls", _("'%s' is not an http(s) address.", url)))
        if len(urls) < MIN_IMAGES[raw.get("_sheet") or kind]:
            errors.append(("image_urls", _("At least %s image link(s) are required.", MIN_IMAGES[kind])))
        if kind == "equipment":
            payload["model"] = raw.get("model", "")
            if not payload["model"]:
                errors.append(("model", _("model is required.")))
            payload["capacity"] = number("capacity", required=True)
            payload["capacity_unit"] = choice("capacity_unit", _UNIT, required=not raw.get("_sheet"))
            if payload["capacity"] and not payload["capacity_unit"] and not any(c == "capacity_unit" for c, _m in errors):
                errors.append(("capacity_unit", _("Choose the capacity unit (kVA, kW, ton...) next to the capacity.")))
            payload["energy"] = raw.get("energy", "").strip()
            payload["year"] = number("year", integer=True)
            payload["hours"] = number("hours", integer=True)
            payload["serial"] = (raw.get("serial") or "").strip()
            rentable = _YES.get((raw.get("rentable") or "").strip().lower())
            if rentable is None:
                errors.append(("rentable", _("rentable must be yes or no.")))
            payload["rentable"] = bool(rentable)
            payload["rent_price_day"] = number("rent_price_day")
            payload["engine"] = raw.get("engine", "")
            payload["engine_size"] = number("engine_size")
            payload["prime_power"] = raw.get("prime_power", "")
            payload["city"] = raw.get("city", "").strip().lower()
            provider = (raw.get("warranty_provider") or "").strip().lower()
            payload["warranty_provider"] = {"vendor": "vendor", "المورد": "vendor", "manufacturer": "manufacturer",
                                            "المصنع": "manufacturer", "المصنّع": "manufacturer", "platform": "platform",
                                            "المنصة": "platform"}.get(provider, False)
            if provider and not payload["warranty_provider"]:
                errors.append(("warranty_provider", _("warranty_provider must be vendor, manufacturer or platform.")))
            payload["warranty_scope"] = raw.get("warranty_scope", "")
            payload["meter_photo_url"] = (raw.get("meter_photo_url") or "").strip()
            payload["plate_photo_url"] = (raw.get("plate_photo_url") or "").strip()
            payload["video_url"] = (raw.get("video_url") or "").strip()
            # the operation video is required for used equipment only
            if payload["video_url"] or payload["condition"] in ("used", "refurbished"):
                if not _http_url(payload["video_url"]):
                    errors.append(("video_url", _("video_url must be an http(s) address.")))
        else:
            payload["part_number"] = raw.get("part_number", "")
            if not payload["part_number"] and not raw.get("_sheet"):  # the unified sheet has no part number column
                errors.append(("part_number", _("part_number is required.")))
            payload["alt_numbers"] = raw.get("alt_numbers", "")
            sources = _split(raw.get("sources", ""))
            payload["sources"] = sources
            payload["tags"] = _split(raw.get("tags", ""))
            payload["compatible"] = _split(raw.get("compatible", ""))
            if not payload["tags"] and not raw.get("_sheet"):  # optional on the unified sheet
                errors.append(("tags", _("tags is required.")))
            if not payload["compatible"]:
                errors.append(("compatible", _("compatible is required.")))
        return payload, errors

    def _leaders_normalize_unified(self, raw):
        """A row of the unified sheet: its subcategory decides whether it is read as equipment or as a part."""
        categs = self._leaders_find_subcategory((raw.get("category") or "").strip())
        field_set = categs._leaders_effective().leaders_field_set if len(categs) == 1 else False
        if not field_set:
            return {"category": raw.get("category", ""), "_kind": False}, [
                ("category", _("Category '%s' does not exist or is ambiguous.", raw.get("category", "")))]
        row_kind = "equipment" if field_set == "equipment" else "spare_parts"
        row = dict(raw, _sheet="products")
        listing = (raw.get("listing_type") or "").strip().lower()
        if listing not in ("للبيع", "للإيجار", "للايجار", "sale", "rent", "for sale", "for rent"):
            return {"category": raw.get("category", ""), "_kind": row_kind}, [
                ("listing_type", _("listing_type must be للبيع or للإيجار (sale or rent)."))]
        row["rentable"] = "yes" if listing in ("للإيجار", "للايجار", "rent", "for rent") else "no"
        if row_kind == "equipment":
            row["model"] = raw.get("family", "")
            if not raw.get("capacity"):
                row["capacity"] = "0"
        else:
            row["compatible"] = raw.get("family", "")
        payload, errors = self._leaders_normalize_row(row_kind, row)
        payload["_kind"] = row_kind
        return payload, errors

    def _leaders_row_kind(self, payload):
        return payload.get("_kind") or self.kind

    @api.model
    def _leaders_find_by_key(self, model, name):
        key = normalize_model_name(name)
        return self.env[model].sudo().search([("key", "=", key)], limit=1) if key else self.env[model]

    def _leaders_find_subcategory(self, name):
        Category = self.env["product.public.category"].sudo()
        found = Category.search([("name", "=ilike", name)])
        for lang in ("ar_001", "ar", "en_US"):
            if found:
                break
            found = Category.with_context(lang=lang).search([("name", "=ilike", name)])
        return found

    def _leaders_check_rules(self, payload, ctx):
        if self.kind not in KINDS:
            return super()._leaders_check_rules(payload, ctx)
        vendor = self.vendor_id.commercial_partner_id
        Offer = self.env["leaders.marketplace.offer"].sudo()
        errors, warnings, resolved = [], [], {}
        if vendor.vendor_state != "approved":
            return [("name", _("Your vendor account is not active."))], [], resolved
        # category and its settings
        categs = self._leaders_find_subcategory(payload["category"])
        if len(categs) != 1:
            errors.append(("category", _("Category '%s' does not exist or is ambiguous.", payload["category"])))
            categ = False
        else:
            categ = categs._leaders_effective()
            wanted = "equipment" if self._leaders_row_kind(payload) == "equipment" else "part"
            if categ.leaders_field_set != wanted or categ.leaders_is_service or categs.leaders_hidden:
                errors.append(("category", _("Category '%(name)s' does not belong to this sheet.", name=payload["category"])))
        resolved["category"] = categs if len(categs) == 1 else self.env["product.public.category"]
        source = self.env["leaders.source"].leaders_find(payload["origin"])
        if not source:
            errors.append(("origin", _("Source '%s' is not in the approved list; ask the admin to add it.", payload["origin"])))
        resolved["source"] = source
        resolved["energy"] = self.env["leaders.energy.source"]
        if payload.get("energy"):
            resolved["energy"] = self.env["leaders.energy.source"].leaders_find(payload["energy"])
            if not resolved["energy"]:
                errors.append(("energy", _("Energy source '%s' is not in the approved list; ask the admin to add it.", payload["energy"])))
        resolved["sources"] = self.env["leaders.source"]
        for token in payload.get("sources") or []:
            found = self.env["leaders.source"].leaders_find(token)
            if not found:
                errors.append(("sources", _("Source '%s' is not in the approved list; ask the admin to add it.", token)))
            resolved["sources"] |= found
        brand = self._leaders_find_by_key("leaders.brand", payload["brand"])
        if not brand:
            errors.append(("brand", _("Brand '%s' is not in the approved list; ask the admin to add it.", payload["brand"])))
        resolved["brand"] = brand
        if payload["condition"] in ("used", "refurbished") and categ and not categ.leaders_accepts_used:
            errors.append(("condition", _("'%s' accepts new products only.", payload["category"])))
        if self._leaders_row_kind(payload) == "equipment":
            if payload["condition"] in ("used", "refurbished"):
                if not payload.get("year"):
                    errors.append(("year", _("year is required for a used product.")))
                if not payload.get("hours"):
                    errors.append(("hours", _("hours is required for a used product.")))
            year = payload.get("year")
            if year and not 1950 <= year <= fields.Date.today().year + 1:
                errors.append(("year", _("year %s is not valid.", year)))
            if payload["rentable"]:
                if categ and not categ.leaders_can_rent:
                    errors.append(("rentable", _("'%s' cannot be rented.", payload["category"])))
                if not payload.get("rent_price_day"):
                    errors.append(("rent_price_day", _("rent_price_day is required when rentable is yes.")))
            serial = payload.get("serial")
            if serial:
                seen = ctx.setdefault("serials", set())
                if serial.lower() in seen or self.env["product.template"].sudo().with_context(
                        active_test=False).search_count([("leaders_serial", "=ilike", serial)]):
                    errors.append(("serial", _("Serial number %s already exists.", serial)))
                seen.add(serial.lower())
        else:
            unknown = [t for t in payload["tags"] if not self._leaders_find_by_key("leaders.product.tag", t)]
            if unknown:
                errors.append(("tags", _("Unknown tag(s): %s. Use the exact approved spelling.", ", ".join(unknown))))
            resolved["tags"] = self.env["leaders.product.tag"].sudo().browse(
                [self._leaders_find_by_key("leaders.product.tag", t).id for t in payload["tags"]
                 if self._leaders_find_by_key("leaders.product.tag", t)])
        try:
            Offer._leaders_check_fulfillment_type("fbm")
        except UserError as exc:
            errors.append(("name", str(exc)))
        tier = vendor.vendor_tier_id
        if tier and tier.product_limit:
            current = Offer.search_count([("vendor_id", "=", vendor.id), ("active", "=", True)])
            if current + ctx.get("extra", 0) >= tier.product_limit:
                errors.append(("name", _("Your vendor tier product limit has been reached.")))
        resolved["markets"] = self.env["website"]
        if not errors:
            ctx["extra"] = ctx.get("extra", 0) + 1
        return errors, warnings, resolved

    def _leaders_set_translated(self, template, name, name_en, description, description_en):
        """The English value is the stored source; the Arabic one is written in the Arabic language."""
        Lang = self.env["res.lang"]
        arabic = next((code for code in ("ar_001", "ar") if Lang.search_count([("code", "=", code), ("active", "=", True)])), None)
        template = template.sudo()
        template.with_context(lang="en_US").write({
            "name": name_en or name, "description_sale": description_en or description})
        if arabic:
            template.with_context(lang=arabic).write({"name": name, "description_sale": description})

    def _leaders_apply_payload(self, payload, resolved):
        if self.kind not in KINDS:
            return super()._leaders_apply_payload(payload, resolved)
        vendor = self.vendor_id.commercial_partner_id
        warnings = []
        vals = {
            "name": payload["name"], "type": "consu", "is_storable": True, "sale_ok": True,
            "purchase_ok": False, "marketplace_state": "draft", "marketplace_owner_vendor_id": vendor.id,
            "public_categ_ids": [(6, 0, resolved["category"].ids)],
            "leaders_brand_id": resolved["brand"].id, "leaders_source_id": resolved["source"].id,
            "leaders_condition": payload["condition"], "leaders_warranty_months": payload.get("warranty_months") or 0,
            "leaders_similar_names": payload.get("similar_names") or False,
        }
        Model = self.env["leaders.equipment.model"].sudo()
        if self._leaders_row_kind(payload) == "equipment":
            vals.update({
                "leaders_model_id": Model._leaders_get_or_create([payload["model"]]).id,
                "leaders_capacity": payload["capacity"], "leaders_capacity_unit": payload["capacity_unit"],
                "leaders_energy_id": resolved["energy"].id or False, "leaders_year": payload.get("year") or 0,
                "leaders_hours": payload.get("hours") or 0, "leaders_serial": payload.get("serial") or False,
                "leaders_rentable": payload["rentable"], "leaders_rent_price_day": payload.get("rent_price_day") or 0.0,
                "leaders_video_url": payload["video_url"] or False, "leaders_engine": payload.get("engine") or False,
                "leaders_engine_size": payload.get("engine_size") or 0.0,
                "leaders_prime_power": payload.get("prime_power") or False,
                "leaders_city": payload.get("city") or False,
                "leaders_warranty_provider": payload.get("warranty_provider") or False,
                "leaders_warranty_scope": payload.get("warranty_scope") or False,
            })
            if self._leaders_image_urls_allowed():  # the proof photos of used equipment
                for key, field in (("meter_photo_url", "leaders_meter_photo"), ("plate_photo_url", "leaders_plate_photo")):
                    if payload.get(key):
                        image, problem = self._leaders_fetch_image(payload[key])
                        if image:
                            vals[field] = image
                        elif problem:
                            warnings.append(problem)
        else:
            vals.update({
                "leaders_part_number": payload["part_number"], "leaders_source_id": vals.get("leaders_source_id") or resolved["sources"][:1].id or False, "leaders_alt_numbers": payload.get("alt_numbers") or False,
                "leaders_tag_ids": [(6, 0, resolved["tags"].ids)],
                "leaders_compatible_model_ids": [(6, 0, Model._leaders_get_or_create(payload["compatible"]).ids)],
            })
        urls = payload["image_urls"]
        images = []
        if self._leaders_image_urls_allowed():
            for url in urls:
                image, problem = self._leaders_fetch_image(url)
                if image:
                    images.append(image)
                else:
                    warnings.append(("image_urls", problem))
        else:
            warnings.append(("image_urls", self._leaders_image_disabled_message()))
        if images:
            vals["image_1920"] = images[0]
            vals["product_template_image_ids"] = [(0, 0, {"name": "%s %s" % (payload["name"], i), "image_1920": img})
                                                  for i, img in enumerate(images[1:], start=2)]
        template = self.env["product.template"].sudo().create(vals)
        self._leaders_set_translated(template, payload["name"], payload["name_en"],
                                     payload["description"], payload["description_en"])
        product = template.product_variant_id
        offer = self.env["leaders.marketplace.offer"].sudo().create({
            "product_tmpl_id": template.id, "product_id": product.id, "vendor_id": vendor.id,
            "seller_sku": payload["sku"], "price": payload["price"],
            "currency_id": self.env.company.currency_id.id, "fulfillment_type": "fbm",
            "stock_location_id": vendor._marketplace_stock_location("fbm").id,
        })
        # the quantity on hand; a used or refurbished machine is always one piece (the guide hides its quantity)
        qty = 1 if payload["condition"] in ("used", "refurbished") else payload.get("initial_qty") or 0
        if qty:
            offer._leaders_set_fbm_stock(qty)
        if self.submit_after_import:
            offer._leaders_vendor_submit(vendor)
        return offer, warnings


class MarketplaceOfferCatalogSubmit(models.Model):
    _inherit = "leaders.marketplace.offer"

    def _leaders_vendor_submit(self, vendor):
        """A product of the equipment / parts trees is complete before it reaches review."""
        for offer in self:
            problems = offer.product_tmpl_id.sudo()._leaders_catalog_problems()
            if problems:
                raise UserError(_("%(name)s cannot be submitted:\n- %(problems)s",
                                  name=offer.product_tmpl_id.display_name, problems="\n- ".join(problems)))
        return super()._leaders_vendor_submit(vendor)
