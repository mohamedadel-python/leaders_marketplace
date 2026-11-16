"""B3: variants, extra images and specifications for vendor-owned products."""
import re

from odoo import api, fields, models, _
from odoo.exceptions import UserError

PARAM_ENABLED = "leaders_marketplace.vendor_variants_enabled"
PARAM_NEW_ATTRS = "leaders_marketplace.allow_vendor_attributes"
PARAM_MAX_IMAGES = "leaders_marketplace.vendor_max_extra_images"

MAX_VARIANTS = 100
MAX_NAME_LEN = 64
MAX_NEW_VALUES = 20
MAX_SPEC_LINES = 50
MAX_SPECS_LEN = 4000


class ProductTemplate(models.Model):
    _inherit = "product.template"

    leaders_specs = fields.Text(
        string="Specifications", translate=True,
        help="One 'name: value' per line. Shown as a table on the website product page.")

    # -- settings ---------------------------------------------------------
    @api.model
    def _leaders_variants_enabled(self):
        value = self.env["ir.config_parameter"].sudo().get_param(PARAM_ENABLED)
        return str(value).lower() in ("true", "1")

    @api.model
    def _leaders_allow_vendor_attributes(self):
        value = self.env["ir.config_parameter"].sudo().get_param(PARAM_NEW_ATTRS)
        return str(value).lower() in ("true", "1")

    @api.model
    def _leaders_max_extra_images(self):
        try:
            limit = int(self.env["ir.config_parameter"].sudo().get_param(PARAM_MAX_IMAGES) or 0)
        except (TypeError, ValueError):
            limit = 0
        return limit if limit > 0 else 8

    # -- ownership --------------------------------------------------------
    def _leaders_vendor_owns_template(self, vendor):
        """Like ``_leaders_vendor_owns_content`` but also true while the template has no offer yet
        (variants are configured before the offers exist). Any offer of another vendor refuses."""
        self.ensure_one()
        vendor = vendor.commercial_partner_id
        if not vendor or self.marketplace_owner_vendor_id.commercial_partner_id != vendor:
            return False
        offers = self.with_context(active_test=False).marketplace_offer_ids
        return all(o.vendor_id.commercial_partner_id == vendor for o in offers)

    # -- specifications ---------------------------------------------------
    @api.model
    def _leaders_clean_specs(self, text):
        """Normalise portal input: trimmed 'name: value' lines, bounded."""
        lines = []
        for raw in (text or "").splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            if line:
                lines.append(line)
        if len(lines) > MAX_SPEC_LINES:
            raise UserError(_("At most %s specification lines are allowed.", MAX_SPEC_LINES))
        result = "\n".join(lines)
        if len(result) > MAX_SPECS_LEN:
            raise UserError(_("The specifications are too long (max %s characters).", MAX_SPECS_LEN))
        return result

    def _leaders_specs_rows(self):
        """[(name, value)] parsed from ``leaders_specs`` (a line without ':' is a value-only row)."""
        self.ensure_one()
        rows = []
        for line in (self.leaders_specs or "").splitlines():
            line = line.strip()
            if not line:
                continue
            key, sep, value = line.partition(":")
            rows.append((key.strip(), value.strip()) if sep else ("", line))
        return rows

    # -- attributes -------------------------------------------------------
    @api.model
    def _leaders_pickable_attributes(self):
        """Attributes a vendor may choose: variant axes (always) and specs (no_variant)."""
        return self.env["product.attribute"].sudo().search(
            [("create_variant", "in", ("always", "no_variant"))], order="sequence, name")

    def _leaders_current_selection(self):
        self.ensure_one()
        return {line.attribute_id.id: set(line.value_ids.ids)
                for line in self.attribute_line_ids}

    @api.model
    def _leaders_create_vendor_attribute_values(self, attribute, names):
        """Add values to an existing attribute (setting-gated; moderated)."""
        if not self._leaders_allow_vendor_attributes():
            raise UserError(_("Adding new attribute values is not allowed."))
        Value = self.env["product.attribute.value"].sudo()
        names = self._leaders_clean_names(names)
        values = Value
        for name in names:
            existing = Value.search([("attribute_id", "=", attribute.id), ("name", "=ilike", name)], limit=1)
            values |= existing or Value.create({"attribute_id": attribute.id, "name": name})
        return values

    @api.model
    def _leaders_clean_names(self, names):
        result = []
        for name in names:
            name = re.sub(r"\s+", " ", name or "").strip()
            if not name:
                continue
            if len(name) > MAX_NAME_LEN:
                raise UserError(_("A name is too long (max %s characters).", MAX_NAME_LEN))
            if name.lower() not in [n.lower() for n in result]:
                result.append(name)
        if len(result) > MAX_NEW_VALUES:
            raise UserError(_("At most %s new values can be added at once.", MAX_NEW_VALUES))
        return result

    @api.model
    def _leaders_create_vendor_attribute(self, name, value_names, as_spec=False):
        """Create (or reuse by name) an attribute with values; setting-gated; moderated."""
        if not self._leaders_allow_vendor_attributes():
            raise UserError(_("Creating new attributes is not allowed."))
        name = re.sub(r"\s+", " ", name or "").strip()
        if not name:
            raise UserError(_("The attribute name is required."))
        if len(name) > MAX_NAME_LEN:
            raise UserError(_("A name is too long (max %s characters).", MAX_NAME_LEN))
        Attribute = self.env["product.attribute"].sudo()
        attribute = Attribute.search([("name", "=ilike", name)], limit=1)
        if not attribute:
            attribute = Attribute.create({
                "name": name, "display_type": "radio",
                "create_variant": "no_variant" if as_spec else "always"})
        return attribute, self._leaders_create_vendor_attribute_values(attribute, value_names)

    def _leaders_apply_attribute_selection(self, selection):
        """Make the template's attribute lines match ``selection`` ({attribute_id: [value_ids]}).

        Odoo generates the variants from the lines. Refused (UserError, nothing written):
        - values that are not real values of an allowed attribute;
        - more than MAX_VARIANTS resulting variants;
        - adding a NEW attribute line, or removing values/lines, once offers or orders exist on
          the affected variants (Odoo would archive the variant carrying them).
        Only 'always' attributes generate variants; 'dynamic' ones are not offered.
        """
        self.ensure_one()
        template = self.sudo()
        allowed = self._leaders_pickable_attributes()
        wanted = {}
        for attribute_id, value_ids in selection.items():
            attribute = allowed.filtered(lambda a: a.id == attribute_id)
            if not attribute:
                raise UserError(_("This attribute cannot be used."))
            values = attribute.value_ids.filtered(lambda v: v.id in set(value_ids))
            if len(values) != len(set(value_ids)):
                raise UserError(_("Unknown value for attribute %s.", attribute.name))
            if values:
                wanted[attribute] = values
        current = {line.attribute_id: line for line in template.attribute_line_ids}
        # variant count
        count = 1
        for attribute, values in wanted.items():
            if attribute.create_variant == "always":
                count *= len(values)
        if count > MAX_VARIANTS:
            raise UserError(_("Too many variants (%(n)s): the limit is %(max)s.", n=count, max=MAX_VARIANTS))

        commands = []
        for attribute, values in wanted.items():
            line = current.get(attribute)
            if line:
                removed = line.value_ids - values
                if removed and attribute.create_variant == "always" and template._leaders_variants_in_use(
                        lambda p: bool(p.product_template_attribute_value_ids.product_attribute_value_id & removed)):
                    raise UserError(_(
                        "You cannot remove %(values)s: variants using it already have offers or orders.",
                        values=", ".join(removed.mapped("name"))))
                if set(values.ids) != set(line.value_ids.ids):
                    commands.append((1, line.id, {"value_ids": [(6, 0, values.ids)]}))
            else:
                if attribute.create_variant == "always" and template._leaders_variants_in_use():
                    raise UserError(_(
                        "You cannot add a new option to a product whose variants already have offers "
                        "or orders. Add values to an existing option instead."))
                commands.append((0, 0, {"attribute_id": attribute.id, "value_ids": [(6, 0, values.ids)]}))
        for attribute, line in current.items():
            if attribute not in allowed:
                continue  # e.g. a 'dynamic' option set by staff: not managed from the portal
            if attribute not in wanted:
                if attribute.create_variant == "always" and template._leaders_variants_in_use():
                    raise UserError(_(
                        "You cannot remove the option %s: its variants already have offers or orders.",
                        attribute.name))
                commands.append((2, line.id, 0))
        if commands:
            template.write({"attribute_line_ids": commands})
        return True

    def _leaders_variants_in_use(self, predicate=None):
        """Any (even archived) variant of the template - optionally matching ``predicate`` - that
        has an offer or a sale order line."""
        self.ensure_one()
        products = self.with_context(active_test=False).product_variant_ids
        if predicate:
            products = products.filtered(predicate)
        if not products:
            return False
        if self.env["leaders.marketplace.offer"].sudo().with_context(active_test=False).search_count(
                [("product_id", "in", products.ids)]):
            return True
        return bool(self.env["sale.order.line"].sudo().search_count([("product_id", "in", products.ids)]))

    def _leaders_variants_without_offer(self, vendor):
        """Active variants of the template that the vendor has no offer for yet."""
        self.ensure_one()
        offered = self.env["leaders.marketplace.offer"].sudo().with_context(active_test=False).search(
            [("product_tmpl_id", "=", self.id), ("vendor_id", "=", vendor.id)]).product_id
        return self.sudo().product_variant_ids - offered

    # -- extra images -----------------------------------------------------
    def _leaders_check_extra_images(self, new_count, remove_ids=()):
        """Refuse when the extra gallery would exceed the limit."""
        self.ensure_one()
        limit = self._leaders_max_extra_images()
        remaining = len(self.sudo().product_template_image_ids.filtered(lambda i: i.id not in set(remove_ids)))
        if remaining + new_count > limit:
            raise UserError(_("A product can have at most %s extra images.", limit))

    def _leaders_add_extra_image(self, image_b64, name=None):
        self.ensure_one()
        return self.env["product.image"].sudo().create({
            "name": name or self.name or _("Image"),
            "image_1920": image_b64,
            "product_tmpl_id": self.id,
        })

    def _leaders_remove_extra_images(self, image_ids):
        """Delete extra images (only ones that belong to this template)."""
        self.ensure_one()
        images = self.sudo().product_template_image_ids.filtered(lambda i: i.id in set(image_ids))
        images.unlink()
        return True


class MarketplaceOffer(models.Model):
    _inherit = "leaders.marketplace.offer"

    @api.model
    def _leaders_create_variant_offers(self, vendor, template, rows, fulfillment_type, currency,
                                       markets):
        """Create ONE offer per variant, atomically.

        ``rows`` = [{"product": product.product, "sku", "price", "stock", "discount_percent"}].
        Raises UserError (caller rolls back through its savepoint). Every row is validated
        before anything is written: SKU unique per vendor (also inside the batch), tier limit
        counting one per offer, FBL refused when disabled, no FBL initial stock.
        """
        Offer = self.sudo()
        self._leaders_check_fulfillment_type(fulfillment_type)
        if not rows:
            raise UserError(_("Select at least one variant."))
        tier = vendor.vendor_tier_id
        if tier and tier.product_limit:
            current = Offer.search_count([("vendor_id", "=", vendor.id), ("active", "=", True)])
            if current + len(rows) > tier.product_limit:
                raise UserError(_("Your vendor tier product limit has been reached."))
        seen = set()
        for row in rows:
            sku = (row.get("sku") or "").strip()
            product = row["product"]
            if not sku:
                raise UserError(_("Seller SKU is required for %s.", product.display_name))
            if sku.lower() in seen:
                raise UserError(_("Seller SKU %s is used twice.", sku))
            seen.add(sku.lower())
            if Offer.with_context(active_test=False).search_count(
                    [("vendor_id", "=", vendor.id), ("seller_sku", "=", sku)]):
                raise UserError(_("Seller SKU %s is already used by another of your offers.", sku))
            if row.get("price", 0) < 0:
                raise UserError(_("A valid price is required for %s.", product.display_name))
            if row.get("stock", 0) < 0:
                raise UserError(_("The stock quantity cannot be negative."))
            if fulfillment_type == "fbl" and row.get("stock", 0):
                raise UserError(_("FBL stock is controlled by warehouse inbound receipts, not direct vendor adjustments."))
            if Offer.with_context(active_test=False).search_count([
                    ("vendor_id", "=", vendor.id), ("product_id", "=", product.id),
                    ("fulfillment_type", "=", fulfillment_type)]):
                raise UserError(_("You already have an offer for this product with the same fulfillment."))
            row["sku"] = sku
        location = vendor._marketplace_stock_location(fulfillment_type)
        offers = Offer.browse()
        for row in rows:
            offer = Offer.create({
                "product_tmpl_id": template.id,
                "product_id": row["product"].id,
                "vendor_id": vendor.id,
                "seller_sku": row["sku"],
                "price": row["price"],
                "currency_id": currency.id,
                "fulfillment_type": fulfillment_type,
                "stock_location_id": location.id,
                "market_ids": [(6, 0, markets.ids)],
            })
            if row.get("stock"):
                offer._leaders_set_fbm_stock(row["stock"])
            if row.get("discount_percent"):
                offer._leaders_set_discount({"type": "percent", "percent": row["discount_percent"]})
            offers |= offer
        return offers
