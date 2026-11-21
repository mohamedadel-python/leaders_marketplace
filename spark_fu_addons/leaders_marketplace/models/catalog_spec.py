"""Equipment / spare-parts catalogue fields.

The storefront is an external app that reads the catalogue through the API, so
the fields live on ``product.template`` and the category tree on
``product.public.category``. A field exists only when it serves one of three
purposes: linking a part to its equipment, filtering, or steering platform
behaviour (condition drives inspection, rental flag drives the rental path).
Everything else belongs in the free-text description.
"""
import re
import unicodedata

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import html2plaintext

CONDITIONS = [("new", "New"), ("used", "Used"), ("refurbished", "Refurbished")]
USED_LIKE = ("used", "refurbished")
CAPACITY_UNITS = [
    ("kva", "KVA"), ("kw", "kW"), ("ton", "Ton"), ("m3", "Cubic metre"),
    ("amp", "Ampere"), ("m", "Metre"), ("l", "Litre"),
]
FIELD_SETS = [("equipment", "Equipment"), ("part", "Spare parts & consumables")]
GROUPS = [("parts", "Parts"), ("power", "Power"), ("equipment", "Equipment"), ("services", "Services")]
KVA_UNITS = ("kva", "kw")
MIN_YEAR = 1950


# Arabic diacritics and the tatweel are dropped from a slug; letters and digits of both scripts are kept
_AR_MARKS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")


# Arabic letters -> Latin, for slugs: the storefront's category pages take ASCII links only
_AR_LATIN = str.maketrans({
    "ا": "a", "أ": "a", "إ": "i", "آ": "a", "ء": "", "ؤ": "o", "ئ": "e", "ب": "b", "ت": "t", "ث": "th",
    "ج": "g", "ح": "h", "خ": "kh", "د": "d", "ذ": "z", "ر": "r", "ز": "z", "س": "s", "ش": "sh", "ص": "s",
    "ض": "d", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f", "ق": "q", "ك": "k", "ل": "l", "م": "m",
    "ن": "n", "ه": "h", "ة": "a", "و": "w", "ي": "y", "ى": "a",
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4", "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
})


def slug_ascii(text):
    """ASCII slug from an English or Arabic name: 'Air Compressors' -> 'air-compressors',
    'ضواغط هوائية' -> 'dwaght-hwaeya' (Arabic written in Latin letters)."""
    return slugify(slug_words(text).translate(_AR_LATIN))


def slug_words(text):
    """URL segment that keeps Arabic: words of letters and digits (Latin lower-cased, Arabic as written)
    joined by single hyphens. 'Air Compressors' -> 'air-compressors'; 'ضواغط هوائية' -> 'ضواغط-هوائية'."""
    text = _AR_MARKS.sub("", unicodedata.normalize("NFC", text or ""))
    words = []
    for word in re.split(r"[^0-9A-Za-z\u0621-\u063a\u0641-\u064a\u0660-\u0669\u06f0-\u06f9]+", text):
        if word:
            words.append(word.lower() if word.isascii() else word)
    return "-".join(words)


def slugify(text):
    """ASCII URL segment: 'Perkins P110-5 Silent' -> 'perkins-p110-5-silent'."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def normalize_model_name(text):
    """Key used to match model names typed differently ('P110-5' / 'p110 5')."""
    return re.sub(r"[\s\-_/\.]+", "", (text or "")).lower()


class LeadersDictionary(models.AbstractModel):
    """A list the admin manages (source, energy source): a stable ``code``, translated names and
    ``aliases`` so a sheet can say 'تركي', 'Turkish' or 'turkey' and mean the same record."""
    _name = "leaders.dictionary"
    _description = "Managed Catalog List"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True, help="Stable key; never shown to customers.")
    aliases = fields.Char(help="Other spellings a sheet may use, comma separated (any language).")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    product_count = fields.Integer(compute="_compute_product_count")

    _sql_constraints = [("code_unique", "unique(code)", "This code already exists.")]

    def _product_field(self):
        raise NotImplementedError

    def _compute_product_count(self):
        field = self._product_field()
        for rec in self:
            rec.product_count = self.env["product.template"].sudo().with_context(active_test=False).search_count(
                [(field, "=", rec.id)])

    @api.model
    def leaders_find(self, text):
        """The record a sheet cell names: by code, alias or name in any language; empty when unknown."""
        key = normalize_arabic((text or "").strip())
        if not key:
            return self.browse()
        langs = [code for code, _name in self.env["res.lang"].get_installed()] or ["en_US"]
        for rec in self.with_context(active_test=False).search([]):
            names = {normalize_arabic(rec.with_context(lang=l).name or "") for l in langs}
            others = {normalize_arabic(a) for a in (rec.aliases or "").split(",")}
            if key in names | others | {normalize_arabic(rec.code)}:
                return rec
        return self.browse()

    def unlink(self):
        field = self._product_field()
        if self.env["product.template"].sudo().with_context(active_test=False).search_count([(field, "in", self.ids)]):
            raise UserError(_("A list entry used by products cannot be deleted; archive it instead."))
        return super().unlink()


class LeadersSource(models.Model):
    _name = "leaders.source"
    _inherit = "leaders.dictionary"
    _description = "Product Source"

    api_code = fields.Char("Storefront code", help="Value sent to the storefront (turkish, chinese, egyptian). "
                                                   "Empty: the source is not offered as an order source.")
    is_original = fields.Boolean("Original", help="Parts of this source are the maker's original parts "
                                                  "(the others are shown as aftermarket).")

    def _product_field(self):
        return "leaders_source_id"


class LeadersEnergySource(models.Model):
    _name = "leaders.energy.source"
    _inherit = "leaders.dictionary"
    _description = "Energy Source"

    def _product_field(self):
        return "leaders_energy_id"


class LeadersBrand(models.Model):
    _name = "leaders.brand"
    _description = "Brand"
    _order = "name"

    name = fields.Char(required=True, translate=True, index=True)
    active = fields.Boolean(default=True)
    key = fields.Char(compute="_compute_key", store=True, index=True)
    product_count = fields.Integer(compute="_compute_product_count")

    _sql_constraints = [("key_unique", "unique(key)", "This brand already exists.")]

    @api.depends("name")
    def _compute_key(self):
        for brand in self:
            brand.key = normalize_model_name(brand.with_context(lang="en_US").name or brand.name)

    def _compute_product_count(self):
        data = self.env["product.template"].sudo()._read_group(
            [("leaders_brand_id", "in", self.ids)], ["leaders_brand_id"], ["__count"])
        counts = {brand.id: count for brand, count in data}
        for brand in self:
            brand.product_count = counts.get(brand.id, 0)

    def unlink(self):
        if self.env["product.template"].sudo().with_context(active_test=False).search_count(
                [("leaders_brand_id", "in", self.ids)]):
            raise UserError(_("A brand with products cannot be deleted; archive it instead."))
        return super().unlink()


class LeadersProductTag(models.Model):
    _name = "leaders.product.tag"
    _description = "Spare Part Tag"
    _order = "name"

    name = fields.Char(required=True, translate=True, index=True)
    active = fields.Boolean(default=True)
    key = fields.Char(compute="_compute_key", store=True, index=True)

    _sql_constraints = [("key_unique", "unique(key)", "This tag already exists.")]

    @api.depends("name")
    def _compute_key(self):
        for tag in self:
            tag.key = normalize_model_name(tag.with_context(lang="en_US").name or tag.name)


class LeadersEquipmentModel(models.Model):
    """A machine model (``P110-5``): what equipment *is* and what parts *fit*."""
    _name = "leaders.equipment.model"
    _description = "Equipment Model"
    _order = "name"

    name = fields.Char(required=True, index=True)
    brand_id = fields.Many2one("leaders.brand", help="Maker of the machine; parts name it as 'Brand Model' in compatibility.")
    key = fields.Char(compute="_compute_key", store=True, index=True)
    equipment_ids = fields.One2many("product.template", "leaders_model_id")
    part_ids = fields.Many2many(
        "product.template", "leaders_part_model_rel", "model_id", "template_id", string="Compatible Parts")
    equipment_count = fields.Integer(compute="_compute_counts")
    part_count = fields.Integer(compute="_compute_counts")
    active = fields.Boolean(default=True)

    _sql_constraints = [("key_unique", "unique(key)", "This equipment model already exists.")]

    @api.depends("name")
    def _compute_key(self):
        for rec in self:
            rec.key = normalize_model_name(rec.name)

    @api.depends("equipment_ids", "part_ids")
    def _compute_counts(self):
        for rec in self:
            rec.equipment_count = len(rec.equipment_ids)
            rec.part_count = len(rec.part_ids)

    @api.model
    def _leaders_get_or_create(self, names):
        """Models for a list of names; unknown ones are created."""
        result = self.browse()
        for name in names:
            name = (name or "").strip()
            key = normalize_model_name(name)
            if not key:
                continue
            found = self.with_context(active_test=False).search([("key", "=", key)], limit=1)
            result |= found or self.create({"name": name})
        return result


class ProductPublicCategory(models.Model):
    _inherit = "product.public.category"

    leaders_can_buy = fields.Boolean(
        "Can be bought", default=True,
        help="Off hides the buy path for products of this subcategory.")
    leaders_can_rent = fields.Boolean(
        "Can be rented", help="Off hides the rental path and refuses a rental price.")
    leaders_is_service = fields.Boolean(
        "Is a service", help="Services are defined by the admin and ordered through a service request, not the cart.")
    leaders_accepts_used = fields.Boolean(
        "Accepts used", default=True, help="Off limits the subcategory to new products and hides the condition filter.")
    leaders_delivery_surcharge = fields.Float(
        "Delivery surcharge", help="Optional amount added once to the governorate delivery price of an order holding "
                                   "products of this subcategory (the highest surcharge of the order is used).")
    leaders_inspection_required = fields.Boolean(
        "Inspection before publishing",
        help="A used product of this subcategory is published only after the platform marks it inspected.")
    leaders_field_set = fields.Selection(
        FIELD_SETS, string="Product fields",
        help="Which set of fields a product of this subcategory carries (equipment sheet or spare-parts sheet).")
    leaders_slug = fields.Char("Slug", index=True, help="URL segment used by the storefront.")
    leaders_group = fields.Selection(GROUPS, string="Storefront group")
    leaders_hidden = fields.Boolean(
        "Hidden", help="A category that has products is hidden instead of deleted so its archived links keep working.")
    leaders_description = fields.Text(
        "Storefront description", translate=True,
        help="One or two sentences: the lead of the category page and the second line of a sub-category chip.")
    leaders_icon = fields.Binary(
        "Storefront icon", attachment=True,
        help="Square, single-colour SVG (or PNG) shown on the sub-category chip.")

    _sql_constraints = [("leaders_slug_unique", "unique(leaders_slug)", "This category slug is already used.")]

    # ---- the storefront slug is made from the English name, never typed: a hand-typed slug (Arabic, spaces,
    # a test value) broke the category's page. The categories shipped with the module keep theirs: the
    # storefront's routes and translations are built on them.
    def _leaders_shipped(self):
        self.ensure_one()
        return bool(self.env["ir.model.data"].sudo().search_count([
            ("model", "=", self._name), ("res_id", "=", self.id), ("module", "=", "leaders_marketplace")]))

    def _leaders_on_storefront(self):
        """A category of the storefront's trees: it has a group or sits under a category that has a slug."""
        self.ensure_one()
        return bool(self.leaders_group or self.parent_id.leaders_slug)

    def _leaders_auto_slug(self):
        """Unique ASCII slug from the category's name, English or Arabic (Arabic written in Latin letters):
        'Air Compressors' -> 'air-compressors', 'ضواغط هوائية' -> 'dwaght-hwaeya'. Never a space. (An Arabic
        slug opened the blog's pages but not the storefront's category pages.)"""
        self.ensure_one()
        name = self.with_context(lang="en_US").name or self.with_context(lang="ar_001").name or ""
        base = slug_ascii(name)[:60].strip("-") or "category-%s" % self.id
        slug, n = base, 1
        while self.sudo().with_context(active_test=False).search_count([("leaders_slug", "=", slug), ("id", "!=", self.id)]):
            n += 1
            slug = "%s-%s" % (base, n)
        return slug

    def _leaders_refresh_slugs(self):
        for categ in self:
            if categ._leaders_shipped() or not categ._leaders_on_storefront():
                continue
            if categ.parent_id and categ._leaders_top().leaders_group == "services" and \
                    self.env["product.template"].sudo().with_context(active_test=False).search_count(
                        [("leaders_service_page", "=", True), ("public_categ_ids", "in", categ.ids)]):
                continue  # a services tile keeps the slug of its service: links and service requests use it
            slug = categ._leaders_auto_slug()
            if slug != categ.leaders_slug:
                super(ProductPublicCategory, categ).write({"leaders_slug": slug})

    @api.model_create_multi
    def create(self, vals_list):
        typed = []
        for vals in vals_list:
            if not self.env.su:  # from the screen: never typed, made from the name below
                vals.pop("leaders_slug", None)
            typed.append(bool(vals.get("leaders_slug")))
        categories = super().create(vals_list)
        # code and data files (the shipped categories) may give the slug; otherwise it is made from the name
        categories.filtered(lambda c: not c.leaders_slug or not self.env.su)._leaders_refresh_slugs()
        return categories

    def write(self, vals):
        if "leaders_slug" in vals and not self.env.su:
            vals = {k: v for k, v in vals.items() if k != "leaders_slug"}  # read-only on the screen
        res = super().write(vals) if vals else True
        if {"name", "parent_id", "leaders_group"} & set(vals) and "leaders_slug" not in vals:
            self._leaders_refresh_slugs()
        return res

    def _leaders_top(self):
        self.ensure_one()
        node = self
        while node.parent_id:
            node = node.parent_id
        return node

    @api.model
    def _leaders_hidden_tree(self, extra=None):
        """Ids of the hidden categories and of everything under them (``extra``: also treated as hidden)."""
        hidden = self.sudo().search([("leaders_hidden", "=", True)]) | (extra or self.browse())
        return set(self.sudo().search([("id", "child_of", hidden.ids)]).ids) if hidden else set()

    def _leaders_products_hidden_by(self):
        """Storefront products that disappear when this category is hidden (no other visible category)."""
        self.ensure_one()
        hidden = self._leaders_hidden_tree(extra=self._origin or self)
        subtree = self.sudo().search([("id", "child_of", (self._origin or self).ids)]).ids
        products = self.env["product.template"].sudo().search([
            ("public_categ_ids", "in", subtree), ("marketplace_state", "=", "published"), ("is_published", "=", True)])
        return products.filtered(lambda t: set(t.public_categ_ids.ids) <= hidden)

    @api.onchange("leaders_hidden")
    def _onchange_leaders_hidden(self):
        if self.leaders_hidden and not self._origin.leaders_hidden and self._origin.id:
            count = len(self._leaders_products_hidden_by())
            return {"warning": {
                "title": _("Hide this category?"),
                "message": _("%s published product(s) will leave the storefront with it (the products in another "
                             "visible category stay). Nothing is deleted: untick Hidden to bring everything back.", count),
            }}

    def unlink(self):
        used = self.env["product.template"].sudo().with_context(active_test=False).search_count(
            [("public_categ_ids", "in", self.ids)])
        if used:
            raise UserError(_("A category that has products cannot be deleted; hide it instead."))
        return super().unlink()

    @api.constrains("leaders_can_rent", "leaders_is_service", "leaders_can_buy")
    def _check_leaders_settings(self):
        for categ in self:
            if categ.leaders_is_service and (categ.leaders_can_rent or categ.leaders_can_buy):
                raise ValidationError(_("A service cannot be bought or rented."))

    def _leaders_effective(self):
        """The settings of the nearest category (self, then its parents) that sets a field set."""
        self.ensure_one()
        node = self
        while node:
            if node.leaders_field_set or node.leaders_is_service:
                return node
            node = node.parent_id
        return self


class ProductTemplate(models.Model):
    _inherit = "product.template"

    leaders_field_set = fields.Selection(
        FIELD_SETS, compute="_compute_leaders_field_set", store=True, index=True)
    leaders_brand_id = fields.Many2one("leaders.brand", string="Brand", index=True, ondelete="restrict")
    leaders_source_id = fields.Many2one("leaders.source", string="Source", index=True, ondelete="restrict")
    leaders_source_ids = fields.Many2many(
        "leaders.source", "leaders_tmpl_source_rel", "template_id", "source_id", string="Order sources",
        help="Parts: the sources it can be ordered in. Empty: the single source above.")
    leaders_condition = fields.Selection(CONDITIONS, string="Condition", index=True)
    leaders_warranty_months = fields.Integer("Warranty (months)")
    # equipment
    leaders_model_id = fields.Many2one("leaders.equipment.model", string="Model", index=True)
    leaders_capacity = fields.Float("Capacity / power")
    leaders_capacity_unit = fields.Selection(CAPACITY_UNITS, string="Capacity unit")
    leaders_energy_id = fields.Many2one("leaders.energy.source", string="Energy source", index=True, ondelete="restrict")
    leaders_year = fields.Integer("Year of manufacture")
    leaders_hours = fields.Integer("Operating hours")
    leaders_serial = fields.Char("Serial number", copy=False, index=True)
    leaders_rentable = fields.Boolean("Available for rent")
    leaders_rent_price_day = fields.Float("Rent per day")
    # form-only view of leaders_rentable as the sheet's "for sale or rent" choice; not stored, the API reads leaders_rentable
    leaders_listing_type = fields.Selection(
        [("sale", "For sale"), ("rent", "For rent")], string="For sale or rent",
        compute="_compute_leaders_listing_type", inverse="_inverse_leaders_listing_type")
    leaders_video_url = fields.Char("Operation video")
    # spare parts
    leaders_part_number = fields.Char("Part number", index=True)
    leaders_alt_numbers = fields.Char("Alternative numbers", help="Comma separated manufacturer numbers.")
    leaders_similar_names = fields.Char(
        "Similar names", help="Other names the product is known by (dealer, market, technicians), comma separated. "
                              "The storefront search looks in them; they are not shown on the product card.")
    leaders_tag_ids = fields.Many2many("leaders.product.tag", string="Tags")
    leaders_compatible_model_ids = fields.Many2many(
        "leaders.equipment.model", "leaders_part_model_rel", "template_id", "model_id",
        string="Compatible equipment")
    leaders_search_text = fields.Char(
        compute="_compute_leaders_search_text", store=True,
        help="Arabic-normalised name, model and part numbers for the storefront search.")
    leaders_slug = fields.Char("Slug", copy=False, index=True)
    leaders_kind = fields.Selection(
        [("power", "Power"), ("equipment", "Equipment"), ("part", "Part")], string="Shelf",
        compute="_compute_leaders_kind", store=True, index=True)
    leaders_out_of_stock = fields.Boolean("Out of stock", help="Shown as out of stock; adding it to a cart is refused.")
    leaders_featured = fields.Boolean("Featured", help="Shown in the storefront landing rows.")
    leaders_inspected = fields.Boolean("Inspected", copy=False, help="The platform inspected this product (storefront badge).")
    leaders_engine = fields.Char("Engine")
    leaders_engine_size = fields.Float("Engine size (L)")
    leaders_prime_power = fields.Char("Prime power", help="Continuous rating as text, e.g. '225 kVA'.")
    leaders_city = fields.Char("City key", help="Storefront city key, e.g. cairo.")
    leaders_kva = fields.Float(compute="_compute_leaders_kva", store=True, index=True,
                               help="Rating on the kVA filter: capacity when the unit is KVA or kW.")
    leaders_display_price = fields.Float(
        compute="_compute_leaders_display_price", store=True, index=True,
        help="Lowest active offer price, else the list price. 0 means 'request a quote'.")
    leaders_inspection_exempt = fields.Boolean(
        "Exempt from inspection", groups="leaders_marketplace.group_marketplace_manager", copy=False,
        help="Only the admin can exempt a used product from the pre-listing inspection.")
    leaders_inspection_exempt_by = fields.Many2one(
        "res.users", readonly=True, copy=False, groups="leaders_marketplace.group_marketplace_manager")
    leaders_requires_inspection = fields.Boolean(
        "Waits for inspection", compute="_compute_leaders_requires_inspection", store=True,
        help="Used, in a subcategory that asks for an inspection before publishing, and not exempted.")

    _sql_constraints = [
        ("leaders_slug_unique", "unique(leaders_slug)", "This product slug is already used."),
        ("leaders_serial_unique", "unique(leaders_serial)",
         "This serial number already exists on the platform."),
    ]

    # ---- computes ----------------------------------------------------------
    @api.depends("public_categ_ids.leaders_field_set", "public_categ_ids.parent_id.leaders_field_set")
    def _compute_leaders_field_set(self):
        for tmpl in self:
            sets = {c._leaders_effective().leaders_field_set for c in tmpl.public_categ_ids}
            sets.discard(False)
            tmpl.leaders_field_set = next(iter(sets)) if len(sets) == 1 else False

    @api.depends("public_categ_ids.leaders_group", "public_categ_ids.parent_id.leaders_group")
    def _compute_leaders_kind(self):
        kinds = {"parts": "part", "power": "power", "equipment": "equipment"}
        for tmpl in self:
            found = {kinds.get(c._leaders_top().leaders_group) for c in tmpl.public_categ_ids}
            found.discard(None)
            tmpl.leaders_kind = next(iter(found)) if len(found) == 1 else False

    @api.depends("leaders_capacity", "leaders_capacity_unit")
    def _compute_leaders_kva(self):
        for tmpl in self:
            tmpl.leaders_kva = tmpl.leaders_capacity if tmpl.leaders_capacity_unit in KVA_UNITS else 0.0

    @api.depends("list_price", "marketplace_offer_ids.price", "marketplace_offer_ids.state",
                 "marketplace_offer_ids.active", "leaders_inspection_cost")
    def _compute_leaders_display_price(self):
        for tmpl in self:
            offers = tmpl.sudo().marketplace_offer_ids.filtered("active")   # any editor of the product may recompute it
            if offers:   # vendor-sold: the lowest live offer price, 0 (a quote) when none is priced
                prices = [o.price for o in offers if o.state == "active" and o.price > 0]
                price = min(prices) if prices else 0.0
            else:        # admin-defined product: its own list price
                price = tmpl.list_price or 0.0
            # the pre-listing inspection the vendor paid is recovered from the buyer inside the price
            tmpl.leaders_display_price = price + tmpl.sudo().leaders_inspection_cost if price else 0.0

    @api.depends("leaders_rentable")
    def _compute_leaders_listing_type(self):
        for tmpl in self:
            tmpl.leaders_listing_type = "rent" if tmpl.leaders_rentable else "sale"

    def _inverse_leaders_listing_type(self):
        for tmpl in self:
            tmpl.leaders_rentable = tmpl.leaders_listing_type == "rent"

    @api.depends("leaders_condition", "leaders_inspection_exempt", "public_categ_ids.leaders_inspection_required",
                 "public_categ_ids.parent_id.leaders_inspection_required")
    def _compute_leaders_requires_inspection(self):
        """Used, in a subcategory that asks for an inspection before publishing, and not exempted by the admin."""
        for tmpl in self:
            categ = tmpl.public_categ_ids[:1]
            tmpl.leaders_requires_inspection = bool(
                tmpl.leaders_condition in USED_LIKE and categ and categ._leaders_effective().leaders_inspection_required
                and not tmpl.sudo().leaders_inspection_exempt)

    def action_marketplace_approve(self):
        waiting = self.filtered(lambda t: t.leaders_requires_inspection and not t.leaders_inspected)
        if waiting:
            raise UserError(_("These used products must be inspected before they are published:\n- %s",
                              "\n- ".join(waiting.mapped("display_name"))))
        return super().action_marketplace_approve()

    @api.depends("name", "description_sale", "description_ecommerce", "default_code", "leaders_part_number",
                 "leaders_alt_numbers", "leaders_similar_names", "leaders_model_id.name", "leaders_brand_id.name", "leaders_engine",
                 "leaders_engine_size", "leaders_prime_power", "leaders_capacity", "leaders_capacity_unit",
                 "leaders_energy_id", "leaders_hours", "leaders_condition", "leaders_city", "leaders_warranty_months",
                 "leaders_specs", "leaders_tag_ids.name", "leaders_compatible_model_ids.name",
                 "leaders_source_id", "public_categ_ids.name", "public_categ_ids.parent_id.name")
    def _compute_leaders_search_text(self):
        """Everything the storefront ``q`` search looks in, in every active language, normalised.

        Names, descriptions and category names in Arabic and English; brand, model, SKU and part
        numbers; every ``specs`` value as the storefront shows it, with its unit.
        """
        from ..services.storefront import Storefront  # the service imports this module
        langs = self.env["res.lang"].sudo().search([("active", "=", True)]).mapped("code") or ["en_US"]
        for tmpl in self:
            parts = [tmpl.default_code, tmpl.leaders_part_number, tmpl.leaders_alt_numbers, tmpl.leaders_similar_names,
                     tmpl.leaders_model_id.name, tmpl.leaders_brand_id.name, tmpl.leaders_engine,
                     tmpl.leaders_specs]
            parts += tmpl.leaders_tag_ids.mapped("name") + tmpl.leaders_compatible_model_ids.mapped("name")
            for lang in langs:
                rec = tmpl.with_context(lang=lang)
                parts += [rec.name, rec.description_sale, html2plaintext(rec.description_ecommerce or "")]
                for categ in rec.public_categ_ids:
                    parts += [categ.name, categ.parent_id.name]
                if tmpl.leaders_kind:
                    parts += [str(v) for v in Storefront(rec.env)._specs(rec).values()]
            seen = dict.fromkeys(p.strip() for p in parts if isinstance(p, str) and p.strip())
            tmpl.leaders_search_text = normalize_search(" ".join(seen))

    # ---- rules -------------------------------------------------------------
    @api.constrains("leaders_year", "leaders_hours", "leaders_capacity", "leaders_capacity_unit",
                    "leaders_rent_price_day", "leaders_warranty_months")
    def _check_leaders_numbers(self):
        for tmpl in self:
            if tmpl.leaders_year and not MIN_YEAR <= tmpl.leaders_year <= fields.Date.today().year + 1:
                raise ValidationError(_("Year of manufacture %s is not valid.", tmpl.leaders_year))
            if min(tmpl.leaders_hours, tmpl.leaders_warranty_months) < 0 \
                    or tmpl.leaders_capacity < 0 or tmpl.leaders_rent_price_day < 0:
                raise ValidationError(_("Hours, capacity, rent price and warranty cannot be negative."))
            # the storefront shows the capacity with its unit ("60 kVA"); without a unit it shows nothing
            if tmpl.leaders_capacity > 0 and not tmpl.leaders_capacity_unit:
                raise ValidationError(_("Choose the capacity unit (kVA, kW, ton...) next to the capacity."))

    @api.constrains("leaders_serial")
    def _check_leaders_serial(self):
        for tmpl in self:
            if tmpl.leaders_serial and not tmpl.leaders_serial.strip():
                raise ValidationError(_("The serial number cannot be blank."))

    def _leaders_make_slug(self, vals=None):
        """Unique, stable URL segment from brand, model and name."""
        self.ensure_one()
        brand = self.env["leaders.brand"].browse((vals or {}).get("leaders_brand_id") or self.leaders_brand_id.id)
        ident = self.leaders_model_id.name or self.leaders_part_number
        bits = [brand.with_context(lang="en_US").name, ident] if ident else [
            brand.with_context(lang="en_US").name, self.with_context(lang="en_US").name]
        base = slug_ascii(" ".join(b for b in bits if b))[:80].strip("-") or "product-%s" % self.id
        slug, n = base, 1
        while self.sudo().with_context(active_test=False).search_count([("leaders_slug", "=", slug), ("id", "!=", self.id)]) \
                or self.env["leaders.vendor.service"].sudo().with_context(active_test=False).search_count([("slug", "=", slug)]):
            n += 1
            slug = "%s-%s" % (base, n)
        return slug

    def _leaders_sync_model_brand(self):
        for tmpl in self.filtered(lambda t: t.leaders_model_id and t.leaders_brand_id):
            if not tmpl.leaders_model_id.brand_id:
                tmpl.leaders_model_id.sudo().brand_id = tmpl.leaders_brand_id

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not self.env.su:  # the link is made by the platform, never typed on the screen
                vals.pop("leaders_slug", None)
            if not vals.get("leaders_slug"):
                service_slug = self._leaders_service_slug_for(vals)
                if service_slug:  # a service needs its link before it is saved (/services/<slug>)
                    vals["leaders_slug"] = service_slug
            if vals.get("leaders_serial"):
                vals["leaders_serial"] = vals["leaders_serial"].strip()
            elif "leaders_serial" in vals:
                vals["leaders_serial"] = False   # several blanks must not collide on the unique index
        records = super().create(vals_list)
        records._leaders_after_save()
        return records

    def write(self, vals):
        if "leaders_slug" in vals and not self.env.su:
            vals = {k: v for k, v in vals.items() if k != "leaders_slug"}  # read-only on the screen
        if "leaders_serial" in vals:
            vals["leaders_serial"] = (vals["leaders_serial"] or "").strip() or False
        if "leaders_inspection_exempt" in vals:
            vals["leaders_inspection_exempt_by"] = self.env.user.id if vals["leaders_inspection_exempt"] else False
        result = super().write(vals)
        if {"leaders_brand_id", "leaders_model_id", "public_categ_ids", "name", "leaders_part_number"} & set(vals):
            self._leaders_after_save(renamed=True)
        return result

    def _leaders_after_save(self, renamed=False):
        for tmpl in self.filtered(lambda t: not t.leaders_service_page and (
                t.leaders_kind or t.leaders_field_set or t.leaders_brand_id)):
            if not tmpl.leaders_slug:  # as the platform: the screen's user cannot write the link
                tmpl.sudo().leaders_slug = tmpl._leaders_make_slug()
            elif renamed and tmpl.marketplace_state not in ("published", "suspended") and not tmpl.is_published:
                # follows the name until the product goes live; then the link stays, so shared links keep working
                slug = tmpl._leaders_make_slug()
                if slug != tmpl.leaders_slug:
                    tmpl.sudo().leaders_slug = slug
        self._leaders_sync_model_brand()

    @api.model
    def _leaders_service_slug_for(self, vals):
        """The link of a new service: its services sub-category's, or one made from its name; empty when the
        product is not a service."""
        ids = []
        for command in vals.get("public_categ_ids") or []:
            if isinstance(command, (list, tuple)) and command and command[0] == 6:
                ids += list(command[2])
            elif isinstance(command, (list, tuple)) and command and command[0] == 4:
                ids.append(command[1])
        categories = self.env["product.public.category"].sudo().browse(ids).exists()
        services = categories.filtered(lambda c: c._leaders_top().leaders_group == "services")
        if not services:
            return False
        tile = services.filtered("parent_id")[:1]
        if tile and tile.leaders_slug:
            return tile.leaders_slug[:-len("-service")] if tile.leaders_slug.endswith("-service") else tile.leaders_slug
        base = slug_ascii(vals.get("name") if isinstance(vals.get("name"), str) else "")[:60].strip("-") or "service"
        slug, n = base, 1
        while self.sudo().with_context(active_test=False).search_count([("leaders_slug", "=", slug)]):
            n += 1
            slug = "%s-%s" % (base, n)
        return slug

    def _leaders_catalog_problems(self):
        """Missing / contradictory catalogue data of one product, as a list of messages.

        Run when a product is submitted: a draft may be incomplete, a submitted
        one may not. Products outside the equipment / parts trees are not checked.
        """
        self.ensure_one()
        problems = []
        categ = self.public_categ_ids[:1]
        if not categ:
            return problems
        eff = categ._leaders_effective()
        if len(self.public_categ_ids) > 1 and not self.leaders_field_set:
            problems.append(_("Choose one subcategory; the product is in categories with different field sets."))
        if eff.leaders_is_service:
            return [_("Services are defined by the admin; they cannot be submitted by a vendor.")]
        if not self.leaders_field_set:
            return problems
        for name, label in (("leaders_brand_id", _("Brand")), ("leaders_source_id", _("Source")),
                            ("leaders_condition", _("Condition"))):
            if not self[name]:
                problems.append(_("%s is required.", label))
        if self.leaders_condition in USED_LIKE:
            if not eff.leaders_accepts_used:
                problems.append(_("The category %s accepts new products only.", eff.name))
            if self.leaders_field_set == "equipment":
                if not self.leaders_year:
                    problems.append(_("Year of manufacture is required for used equipment."))
                if not self.leaders_hours:
                    problems.append(_("Operating hours are required for used equipment."))
        if self.leaders_field_set == "equipment":
            for name, label in (("leaders_model_id", _("Model")), ("leaders_capacity", _("Capacity / power")),
                                ("leaders_capacity_unit", _("Capacity unit"))):
                if not self[name]:
                    problems.append(_("%s is required.", label))
            if self.leaders_condition in USED_LIKE and not self.leaders_video_url:
                problems.append(_("%s is required.", _("Operation video")))
            if self.leaders_rentable:
                if not eff.leaders_can_rent:
                    problems.append(_("The category %s cannot be rented.", eff.name))
                if not self.leaders_rent_price_day:
                    problems.append(_("Rent per day is required for a rentable product."))
        else:
            # the project guide's unified form: the model (family) links the part to its equipment; the part
            # number (written in the name) and the tags are optional
            if not self.leaders_compatible_model_ids:
                problems.append(_("Compatible equipment is required."))
        return problems

    def action_marketplace_submit(self):
        for tmpl in self:
            problems = tmpl._leaders_catalog_problems()
            if problems:
                raise UserError(_("%(name)s cannot be submitted:\n- %(problems)s",
                                  name=tmpl.display_name, problems="\n- ".join(problems)))
        return super().action_marketplace_submit()


_ARABIC_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي",
})


def normalize_arabic(text):
    """Fold the Arabic spellings that users type interchangeably, drop diacritics, lower-case Latin."""
    text = re.sub(r"[ً-ْـ]", "", text or "")
    return text.translate(_ARABIC_MAP).lower()


# Arabic-Indic and Persian digits -> Latin digits
_DIGITS_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize_search(text):
    """``normalize_arabic`` plus Latin digits and single spaces: the form both the stored search text and ``q`` take."""
    text = normalize_arabic(text).translate(_DIGITS_MAP)
    text = re.sub(r"[ٰؐ-ؚۖ-ۭ]", "", text)  # remaining Quranic marks / superscript alef
    return re.sub(r"\s+", " ", text).strip()
