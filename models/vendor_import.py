"""B2: vendor bulk import (xlsx / csv) with row validation, error file and batched apply."""
import base64
import csv
import io
import ipaddress
import json
import logging
import socket
from datetime import datetime
from urllib.parse import urljoin, urlparse

import pytz
import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.float_utils import float_compare, float_is_zero
from odoo.tools.mimetypes import guess_mimetype

_logger = logging.getLogger(__name__)

IMPORT_KINDS = [
    ("new_products", "New products"),
    ("catalog_offers", "Offers on catalog products"),
    ("update", "Price, stock and discount update"),
]
DISCOUNT_COLUMNS = ["discount_percent", "discount_price", "discount_start", "discount_end"]
# (column, required)
COLUMNS = {
    "new_products": [
        ("name", True), ("description", False), ("category", False), ("sku", True),
        ("price", True), ("fulfillment", False), ("initial_qty", False),
        ("image_url", False), ("markets", False)] + [(c, False) for c in DISCOUNT_COLUMNS],
    "catalog_offers": [
        ("barcode", False), ("internal_reference", False), ("sku", True), ("price", True),
        ("fulfillment", False), ("initial_qty", False), ("markets", False),
    ] + [(c, False) for c in DISCOUNT_COLUMNS],
    "update": [("sku", True), ("price", False), ("stock", False)]
              + [(c, False) for c in DISCOUNT_COLUMNS] + [("clear_discount", False)],
}
EXAMPLES = {
    "new_products": ["EXAMPLE Blue Mug", "Ceramic mug, 300 ml", "Kitchen", "EXAMPLE-MUG-1", "9.99", "fbm",
                     "25", "https://example.com/mug.jpg", "", "", "", "", ""],
    "catalog_offers": ["6223001234567", "", "EXAMPLE-CAT-1", "19.50", "fbm", "10", "", "", "", "", ""],
    "update": ["EXAMPLE-MUG-1", "10.50", "40", "", "", "", "", ""],
}
MAX_ROWS = 5000
MAX_FILE_BYTES = 10 * 1024 * 1024
SYNC_VALIDATION_ROWS = 200
MAX_REDIRECTS = 3
IMAGE_TIMEOUT = 10
PARAM_ENABLED = "leaders_marketplace.vendor_bulk_import_enabled"
PARAM_BATCH = "leaders_marketplace.vendor_import_batch_size"
CRON_XMLID = "leaders_marketplace.cron_process_vendor_imports"


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _norm_header(value):
    return _cell_text(value).lower().replace(" ", "_").replace("-", "_")


class VendorImport(models.Model):
    _name = "leaders.vendor.import"
    _description = "Vendor Bulk Import"
    _order = "id desc"
    _rec_name = "filename"
    _LEADERS_SYNC_ROWS = SYNC_VALIDATION_ROWS

    vendor_id = fields.Many2one("res.partner", required=True, index=True, ondelete="restrict",
                                domain=[("is_marketplace_vendor", "=", True)])
    user_id = fields.Many2one("res.users", string="Uploaded by", ondelete="set null")
    kind = fields.Selection(IMPORT_KINDS, required=True)
    state = fields.Selection([
        ("uploaded", "Uploaded"), ("validating", "Validating"), ("validated", "Validated"),
        ("confirmed", "Confirmed"), ("processing", "Processing"), ("done", "Done"),
        ("failed", "Failed"),
    ], default="uploaded", required=True, index=True)
    file = fields.Binary(attachment=True, required=True)
    filename = fields.Char(required=True)
    submit_after_import = fields.Boolean(
        string="Submit for review after import",
        help="New offers (and the products they create) are sent for review once created.")
    total_rows = fields.Integer(readonly=True)
    valid_rows = fields.Integer(readonly=True)
    invalid_rows = fields.Integer(readonly=True)
    applied_rows = fields.Integer(readonly=True)
    failed_rows = fields.Integer(string="Failed at apply", readonly=True)
    error_file = fields.Binary(attachment=True, readonly=True)
    error_filename = fields.Char(readonly=True)
    log = fields.Text(readonly=True)
    line_ids = fields.One2many("leaders.vendor.import.line", "import_id")

    # ------------------------------------------------------------------
    # Settings / small helpers
    # ------------------------------------------------------------------
    @api.model
    def _leaders_import_enabled(self):
        value = self.env["ir.config_parameter"].sudo().get_param(PARAM_ENABLED)
        return str(value).lower() in ("true", "1")

    @api.model
    def _leaders_batch_size(self):
        try:
            size = int(self.env["ir.config_parameter"].sudo().get_param(PARAM_BATCH, 50) or 50)
        except (TypeError, ValueError):
            size = 50
        return size if size > 0 else 50

    @api.model
    def _leaders_image_max_bytes(self):
        try:
            limit = int(self.env["ir.config_parameter"].sudo().get_param(
                "leaders_marketplace.vendor_image_max_mb", 5))
        except (TypeError, ValueError):
            limit = 5
        return max(limit, 1) * 1024 * 1024

    @api.model
    def _leaders_trigger_cron(self):
        cron = self.env.ref(CRON_XMLID, raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    def _log(self, message):
        for job in self:
            job.log = ((job.log + "\n") if job.log else "") + message

    def _user_tz(self):
        try:
            return pytz.timezone(self.user_id.tz or self.env.user.tz or "UTC")
        except pytz.UnknownTimeZoneError:
            return pytz.UTC

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------
    @api.model
    def _leaders_instructions(self, kind):
        common = [
            _("Fill one row per line. Delete the example row (its SKU starts with EXAMPLE-) or leave it: it is skipped."),
            _("Numbers use a dot or a comma as decimal separator. Negative numbers are refused."),
            _("Discount dates: YYYY-MM-DD or YYYY-MM-DD HH:MM, in your account time zone. Use either discount_percent or discount_price."),
            _("Every row is checked first. You download the error file, then confirm to import only the valid rows."),
        ]
        specific = {
            "new_products": [
                _("name, sku and price are required. category must be the name of an existing website category."),
                _("fulfillment: fbm (default) or fbl. initial_qty is the opening FBM stock."),
                _("image_url: optional http(s) address of a public image; a bad image only produces a warning."),
                _("markets: optional website names separated by ';'. Empty means all your allowed markets."),
                _("New products are created as drafts unless you tick 'submit after import'."),
            ],
            "catalog_offers": [
                _("Give the barcode or the internal reference of a published catalog product."),
                _("sku and price are required. The offer is created as a draft."),
            ],
            "update": [
                _("sku identifies one of your existing offers. Leave a column empty to keep the current value."),
                _("stock is the new FBM quantity (a correction, like 'Set' in the portal). clear_discount: yes removes the discount."),
            ],
        }
        return specific[kind] + common

    @api.model
    def _leaders_build_template(self, kind, fmt):
        """``(bytes, mimetype, filename)`` of the template of ``kind``."""
        headers = [c for c, _req in COLUMNS[kind]]
        example = EXAMPLES[kind]
        filename = "import_%s_template.%s" % (kind, fmt)
        if fmt == "csv":
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(headers)
            writer.writerow(example)
            return out.getvalue().encode("utf-8-sig"), "text/csv", filename
        import openpyxl
        from openpyxl.styles import Font
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Data"
        ws.append(headers)
        ws.append(example)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        sheet = wb.create_sheet("Instructions")
        sheet.append([_("Instructions")])
        sheet["A1"].font = Font(bold=True)
        for line in self._leaders_instructions(kind):
            sheet.append([line])
        sheet.append([])
        sheet.append([_("Columns")])
        for column, required in COLUMNS[kind]:
            sheet.append([column, _("required") if required else _("optional")])
        sheet.column_dimensions["A"].width = 60
        buf = io.BytesIO()
        wb.save(buf)
        return (buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename)

    # ------------------------------------------------------------------
    # File parsing
    # ------------------------------------------------------------------
    def _leaders_read_rows(self):
        """``(headers, [(row_number, {header: text})])`` from the uploaded file."""
        self.ensure_one()
        data = base64.b64decode(self.file or b"")
        if not data:
            raise UserError(_("The uploaded file is empty."))
        if len(data) > MAX_FILE_BYTES:
            raise UserError(_("The file is too large (max %s MB).", MAX_FILE_BYTES // (1024 * 1024)))
        name = (self.filename or "").lower()
        raw_rows = []
        if name.endswith(".xlsx"):
            import openpyxl
            try:
                wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            except Exception:
                raise UserError(_("The file is not a valid .xlsx workbook."))
            ws = wb["Data"] if "Data" in wb.sheetnames else wb.worksheets[0]
            for index, row in enumerate(ws.iter_rows(values_only=True), start=1):
                raw_rows.append((index, list(row)))
                if index > MAX_ROWS + 1:
                    break
        elif name.endswith(".csv"):
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("latin-1")
            sample = text[:4096]
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
            for index, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), start=1):
                raw_rows.append((index, row))
                if index > MAX_ROWS + 1:
                    break
        else:
            raise UserError(_("Upload an .xlsx or .csv file."))
        raw_rows = [r for r in raw_rows if any(_cell_text(v) for v in r[1])]
        if not raw_rows:
            raise UserError(_("The file has no header row."))
        header_row, header = raw_rows[0]
        headers = [_norm_header(h) for h in header]
        rows = []
        for number, values in raw_rows[1:]:
            record = {}
            for i, h in enumerate(headers):
                if h:
                    record[h] = _cell_text(values[i]) if i < len(values) else ""
            rows.append((number, record))
        if len(rows) > MAX_ROWS:
            raise UserError(_("The file has more than %s rows; split it.", MAX_ROWS))
        return headers, rows

    def _leaders_count_rows(self):
        try:
            return len(self._leaders_read_rows()[1])
        except UserError:
            return 0

    # ------------------------------------------------------------------
    # Row parsing (no database)
    # ------------------------------------------------------------------
    @staticmethod
    def _to_number(text):
        text = (text or "").strip().replace(" ", "")
        if not text:
            return None
        if "," in text and "." not in text:
            text = text.replace(",", ".")
        return float(text)

    def _parse_date(self, text):
        text = (text or "").strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                local = datetime.strptime(text, fmt)
            except ValueError:
                continue
            utc = self._user_tz().localize(local).astimezone(pytz.UTC).replace(tzinfo=None)
            return utc.strftime("%Y-%m-%d %H:%M:%S")
        raise ValueError(text)

    def _leaders_normalize_row(self, kind, raw):
        """Parse the text cells of one row. Returns ``(payload, errors)``; errors are ``(column, message)``."""
        errors = []
        payload = {}

        def number(column, minimum=0.0, required=False):
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
            if minimum is not None and value < minimum:
                errors.append((column, _("%s cannot be negative.", column)))
                return None
            return value

        payload["sku"] = raw.get("sku", "")
        if not payload["sku"]:
            errors.append(("sku", _("sku is required.")))
        if kind in ("new_products", "catalog_offers"):
            payload["price"] = number("price", required=True)
            fulfillment = (raw.get("fulfillment") or "fbm").lower()
            if fulfillment not in ("fbm", "fbl"):
                errors.append(("fulfillment", _("fulfillment must be fbm or fbl.")))
            payload["fulfillment"] = fulfillment
            payload["initial_qty"] = number("initial_qty") or 0.0
            payload["markets"] = [m.strip() for m in raw.get("markets", "").split(";") if m.strip()]
        if kind == "new_products":
            payload["name"] = raw.get("name", "")
            if not payload["name"]:
                errors.append(("name", _("name is required.")))
            payload["description"] = raw.get("description", "")
            payload["category"] = raw.get("category", "")
            url = raw.get("image_url", "")
            payload["image_url"] = url
            if url:
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    errors.append(("image_url", _("image_url must be an http or https address.")))
        elif kind == "catalog_offers":
            payload["barcode"] = raw.get("barcode", "")
            payload["internal_reference"] = raw.get("internal_reference", "")
            if not payload["barcode"] and not payload["internal_reference"]:
                errors.append(("barcode", _("Give a barcode or an internal reference.")))
        else:
            payload["price"] = number("price")
            payload["stock"] = number("stock")
            clear = (raw.get("clear_discount") or "").lower()
            if clear not in ("", "yes", "no", "y", "true", "1", "0", "false"):
                errors.append(("clear_discount", _("clear_discount must be yes or no.")))
            payload["clear_discount"] = clear in ("yes", "y", "true", "1")
        # discount
        percent = number("discount_percent", minimum=None)
        sale = number("discount_price", minimum=None)
        payload["discount_percent"], payload["discount_price"] = percent, sale
        for column in ("discount_start", "discount_end"):
            text = raw.get(column, "")
            payload[column] = False
            if text:
                try:
                    payload[column] = self._parse_date(text)
                except ValueError:
                    errors.append((column, _("%(column)s is not a valid date: '%(value)s'.", column=column, value=text)))
        return payload, errors

    # ------------------------------------------------------------------
    # Rules that read the database (validation AND apply)
    # ------------------------------------------------------------------
    def _leaders_find_categories(self, name):
        Category = self.env["product.public.category"].sudo()
        found = Category.search([("name", "=ilike", name)])
        if len(found) > 1:
            exact = found.filtered(lambda c: (c.display_name or "").lower() == name.lower())
            found = exact or found
        return found

    def _leaders_find_websites(self, names):
        Website = self.env["website"].sudo()
        sites, missing = Website, []
        for name in names:
            site = Website.search([("name", "=ilike", name)], limit=2)
            if len(site) == 1:
                sites |= site
            else:
                missing.append(name)
        return sites, missing

    def _leaders_check_discount(self, payload, price, errors):
        """Static discount checks; the service re-validates at apply time."""
        percent, sale = payload.get("discount_percent"), payload.get("discount_price")
        start, end = payload.get("discount_start"), payload.get("discount_end")
        if percent is None and sale is None:
            if start or end:
                errors.append(("discount_percent", _("Discount dates need a discount_percent or discount_price.")))
            return False
        if not self.env["leaders.marketplace.offer"]._leaders_discounts_enabled():
            errors.append(("discount_percent", _("Vendor discounts are not enabled on this marketplace.")))
            return False
        if percent is not None and sale is not None:
            errors.append(("discount_price", _("Use either discount_percent or discount_price, not both.")))
        elif percent is not None and not 0.0 < percent < 100.0:
            errors.append(("discount_percent", _("A percentage discount must be above 0 and below 100.")))
        elif sale is not None and (sale <= 0.0 or (price is not None and sale >= price)):
            errors.append(("discount_price", _("The sale price must be above 0 and below the regular price.")))
        if start and end and end <= start:
            errors.append(("discount_end", _("The discount must end after it starts.")))
        if end and end <= fields.Datetime.to_string(fields.Datetime.now()):
            errors.append(("discount_end", _("The discount end date is already in the past.")))
        return True

    def _leaders_discount_values(self, payload):
        kind = "percent" if payload.get("discount_percent") is not None else "price"
        return {"type": kind, "percent": payload.get("discount_percent"),
                "price": payload.get("discount_price"),
                "start": payload.get("discount_start") or False, "end": payload.get("discount_end") or False}

    def _leaders_check_rules(self, payload, ctx):
        """Every business rule of one row against the current database.

        ``ctx`` carries what the file itself already claimed while validating
        (``extra`` offers, seen SKUs / product keys); at apply time it is fresh
        because the earlier rows are already in the database.
        :return: ``(errors, warnings, resolved)``
        """
        self.ensure_one()
        kind, vendor = self.kind, self.vendor_id.commercial_partner_id
        Offer = self.env["leaders.marketplace.offer"].sudo()
        errors, warnings, resolved = [], [], {}
        sku = payload.get("sku")
        if vendor.vendor_state != "approved":
            return [("sku", _("Your vendor account is not active."))], [], resolved
        seen = ctx.setdefault("skus", set())
        if sku:
            if sku in seen:
                errors.append(("sku", _("Duplicate sku %s in the file.", sku)))
            seen.add(sku)
        if kind == "update":
            offer = Offer.search([("vendor_id", "=", vendor.id), ("seller_sku", "=", sku)], limit=1) if sku else Offer
            if sku and not offer:
                errors.append(("sku", _("No active offer of yours has the sku %s.", sku)))
            price, stock = payload.get("price"), payload.get("stock")
            if price is None and stock is None and payload.get("discount_percent") is None \
                    and payload.get("discount_price") is None and not payload.get("clear_discount"):
                errors.append(("price", _("Nothing to update: fill price, stock or a discount.")))
            if stock is not None and offer:
                try:
                    if offer.fulfillment_type != "fbm":
                        raise UserError(_("FBL stock is controlled by warehouse inbound receipts, not direct vendor adjustments."))
                    offer._leaders_set_fbm_stock(stock, check_only=True)
                except UserError as exc:
                    errors.append(("stock", str(exc)))
            if payload.get("clear_discount") and (payload.get("discount_percent") is not None
                                                  or payload.get("discount_price") is not None):
                errors.append(("clear_discount", _("Do not give a discount and clear it in the same row.")))
            elif self._leaders_check_discount(payload, price if price is not None else (offer.price if offer else None), errors):
                resolved["discount"] = True
            resolved["offer"] = offer
            return errors, warnings, resolved

        # --- new products / catalog offers ---
        fulfillment = payload.get("fulfillment") or "fbm"
        try:
            Offer._leaders_check_fulfillment_type(fulfillment)
        except UserError as exc:
            errors.append(("fulfillment", str(exc)))
        if sku and Offer.with_context(active_test=False).search_count([
                ("vendor_id", "=", vendor.id), ("seller_sku", "=", sku)]):
            errors.append(("sku", _("Seller SKU %s is already used by another of your offers.", sku)))
        tier = vendor.vendor_tier_id
        if tier and tier.product_limit:
            current = Offer.search_count([("vendor_id", "=", vendor.id), ("active", "=", True)])
            if current + ctx.get("extra", 0) >= tier.product_limit:
                errors.append(("sku", _("Your vendor tier product limit has been reached.")))
        # markets
        sites, missing = self._leaders_find_websites(payload.get("markets") or [])
        if missing:
            errors.append(("markets", _("Unknown market(s): %s.", ", ".join(missing))))
        allowed = vendor.vendor_market_ids
        if allowed and sites - allowed:
            errors.append(("markets", _("Market(s) not allowed for your account: %s.",
                                        ", ".join((sites - allowed).mapped("name")))))
        resolved["markets"] = sites
        categories = self.env["product.public.category"]
        if kind == "new_products":
            if payload.get("category"):
                categories = self._leaders_find_categories(payload["category"])
                if not categories:
                    errors.append(("category", _("Category '%s' does not exist.", payload["category"])))
                elif len(categories) > 1:
                    errors.append(("category", _("Category '%s' is ambiguous.", payload["category"])))
            resolved["category"] = categories
        else:
            if not self.env["res.partner"]._marketplace_catalog_enabled():
                errors.append(("barcode", _("Selling existing catalog products is not enabled.")))
            product = self._leaders_find_catalog_product(payload, errors)
            resolved["product"] = product
            if product:
                categories = product.product_tmpl_id.public_categ_ids
                key = (product.id, fulfillment)
                keys = ctx.setdefault("keys", set())
                if key in keys or Offer.with_context(active_test=False).search_count([
                        ("vendor_id", "=", vendor.id), ("product_id", "=", product.id),
                        ("fulfillment_type", "=", fulfillment)]):
                    errors.append(("barcode", _("You already have an offer for this product with the same fulfillment.")))
                keys.add(key)
        # policy: the offer create forces the type; stock only exists for FBM
        forced = self.env["leaders.fulfillment.policy"].sudo()._policy_for(vendor, categories).forced_fulfillment_type
        effective = forced or fulfillment
        resolved["effective_fulfillment"] = effective
        qty = payload.get("initial_qty") or 0.0
        if qty > 0 and effective != "fbm":
            if fulfillment == "fbl":
                errors.append(("initial_qty", _("Initial stock applies to FBM only; FBL stock comes from inbound receipts.")))
            else:
                warnings.append(("initial_qty", _("A fulfillment policy forces FBL, so the initial stock was not added.")))
        if self._leaders_check_discount(payload, payload.get("price"), errors):
            resolved["discount"] = True
        if not errors:
            ctx["extra"] = ctx.get("extra", 0) + 1
        return errors, warnings, resolved

    def _leaders_find_catalog_product(self, payload, errors):
        Product = self.env["product.product"].sudo()
        base = [("sale_ok", "=", True), ("product_tmpl_id.marketplace_state", "=", "published")]
        matches = {}
        for column, field in (("barcode", "barcode"), ("internal_reference", "default_code")):
            value = payload.get(column)
            if not value:
                continue
            found = Product.search(base + [(field, "=", value)], limit=3)
            if not found:
                errors.append((column, _("No published catalog product matches %(column)s '%(value)s'.",
                                         column=column, value=value)))
                return Product
            if len(found) > 1:
                errors.append((column, _("%(column)s '%(value)s' matches several products; the row is ambiguous.",
                                         column=column, value=value)))
                return Product
            matches[column] = found
        if len(matches) == 2 and matches["barcode"] != matches["internal_reference"]:
            errors.append(("internal_reference", _("The barcode and the internal reference point to different products.")))
            return Product
        return next(iter(matches.values()), Product)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def action_validate(self):
        for job in self.filtered(lambda j: j.state in ("uploaded", "validating")):
            try:
                with self.env.cr.savepoint():
                    job._leaders_validate()
            except (UserError, ValidationError, AccessError) as exc:
                job._leaders_fail_file(str(exc))
            except Exception as exc:  # never leave a job stuck in "validating"
                _logger.exception("Vendor import %s: validation crashed", job.id)
                job._leaders_fail_file(_("Unexpected error while reading the file: %s", exc))
        return True

    def _leaders_fail_file(self, message):
        self.ensure_one()
        self.line_ids.unlink()
        self.env["leaders.vendor.import.line"].create({
            "import_id": self.id, "row_number": 1, "status": "invalid", "payload": "{}",
            "messages": json.dumps([{"column": "", "message": message, "level": "error"}])})
        self.write({"state": "failed", "total_rows": 0, "valid_rows": 0, "invalid_rows": 1,
                    "log": message})
        self._leaders_store_error_file()

    def _leaders_validate(self):
        self.ensure_one()
        self.state = "validating"
        self.line_ids.unlink()
        headers, rows = self._leaders_read_rows()
        columns = COLUMNS[self.kind]
        missing = [c for c, req in columns if req and c not in headers]
        if self.kind == "catalog_offers" and "barcode" not in headers and "internal_reference" not in headers:
            missing.append("barcode / internal_reference")
        if missing:
            raise UserError(_("Missing column(s): %s. Download the template for the right headers.",
                              ", ".join(missing)))
        known = {c for c, _r in columns}
        unknown = [h for h in headers if h and h not in known]
        rows = [(n, r) for n, r in rows if not (r.get("sku") or "").upper().startswith("EXAMPLE-")]
        Line = self.env["leaders.vendor.import.line"]
        ctx, vals_list, valid, invalid = {}, [], 0, 0
        for number, raw in rows:
            payload, errors = self._leaders_normalize_row(self.kind, raw)
            warnings = []
            if not errors:
                errors, warnings, _resolved = self._leaders_check_rules(payload, ctx)
            messages = [{"column": c, "message": m, "level": "error"} for c, m in errors] + \
                       [{"column": c, "message": m, "level": "warning"} for c, m in warnings]
            if errors:
                invalid += 1
            else:
                valid += 1
            vals_list.append({
                "import_id": self.id, "row_number": number,
                "status": "invalid" if errors else "valid",
                "payload": json.dumps(payload), "messages": json.dumps(messages)})
        Line.create(vals_list)
        log = [_("Validated %(total)s row(s): %(valid)s valid, %(invalid)s with errors.",
                 total=len(rows), valid=valid, invalid=invalid)]
        if unknown:
            log.append(_("Ignored unknown column(s): %s.", ", ".join(unknown)))
        self.write({"state": "validated", "total_rows": len(rows), "valid_rows": valid,
                    "invalid_rows": invalid, "applied_rows": 0, "failed_rows": 0,
                    "log": "\n".join(log)})
        self._leaders_store_error_file()

    # ------------------------------------------------------------------
    # Error file
    # ------------------------------------------------------------------
    def _leaders_error_rows(self):
        rows = []
        for line in self.line_ids.sorted("row_number"):
            for message in json.loads(line.messages or "[]"):
                rows.append((line.row_number, message.get("column") or "",
                             message.get("level") or "error", message.get("message") or ""))
        return rows

    def _leaders_build_error_file(self, fmt):
        self.ensure_one()
        header = ["row", "column", "level", "message"]
        rows = self._leaders_error_rows()
        if fmt == "csv":
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(header)
            writer.writerows(rows)
            return out.getvalue().encode("utf-8-sig"), "text/csv", "import_%s_errors.csv" % self.id
        import openpyxl
        from openpyxl.styles import Font
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Errors"
        ws.append(header)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in rows:
            ws.append(list(row))
        buf = io.BytesIO()
        wb.save(buf)
        return (buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "import_%s_errors.xlsx" % self.id)

    def _leaders_store_error_file(self):
        for job in self:
            content, _mime, name = job._leaders_build_error_file("csv")
            job.write({"error_file": base64.b64encode(content), "error_filename": name})

    # ------------------------------------------------------------------
    # Confirm and apply
    # ------------------------------------------------------------------
    def action_confirm(self):
        self.ensure_one()
        if self.state != "validated":
            raise UserError(_("Only a validated import can be confirmed."))
        if not self.valid_rows:
            raise UserError(_("There is no valid row to import."))
        if self.vendor_id.vendor_state != "approved":
            raise UserError(_("Your vendor account is not active."))
        self.state = "confirmed"
        self._leaders_trigger_cron()
        return True

    @api.model
    def _leaders_lock(self, domain, limit=None):
        """Jobs matching ``domain`` that no other worker is processing right now."""
        ids = self.search(domain, limit=limit).ids
        if not ids:
            return self.browse()
        self.env.cr.execute(
            "SELECT id FROM leaders_vendor_import WHERE id IN %s ORDER BY id FOR UPDATE SKIP LOCKED",
            [tuple(ids)])
        return self.browse([r[0] for r in self.env.cr.fetchall()])

    @api.model
    def cron_process_imports(self):
        """Validate uploaded files, then apply confirmed imports one batch at a time."""
        for job in self._leaders_lock([("state", "=", "uploaded")], limit=5):
            job.action_validate()
        remaining = self._leaders_batch_size()
        jobs = self._leaders_lock([("state", "in", ("confirmed", "processing"))])
        for job in jobs:
            if remaining <= 0:
                break
            remaining -= job._leaders_process_batch(remaining)
        if self.search_count([("state", "in", ("uploaded", "confirmed", "processing"))]):
            self._leaders_trigger_cron()
        return True

    def _leaders_process_batch(self, limit):
        self.ensure_one()
        if self.vendor_id.vendor_state != "approved":
            self._log(_("The vendor account is not active any more; the import was stopped."))
            self.state = "failed"
            return 0
        self.state = "processing"
        Line = self.env["leaders.vendor.import.line"]
        lines = Line.search([("import_id", "=", self.id), ("status", "=", "valid")],
                            order="row_number", limit=limit)
        for line in lines:
            line._leaders_apply()
        pending = Line.search_count([("import_id", "=", self.id), ("status", "=", "valid")])
        applied = Line.search_count([("import_id", "=", self.id), ("status", "=", "applied")])
        failed = Line.search_count([("import_id", "=", self.id), ("status", "=", "failed")])
        vals = {"applied_rows": applied, "failed_rows": failed}
        if not pending:
            vals["state"] = "done"
        self.write(vals)
        if not pending:
            self._log(_("Import finished: %(applied)s applied, %(failed)s failed.", applied=applied, failed=failed))
            self._leaders_store_error_file()
        return len(lines)

    # ------------------------------------------------------------------
    # Image download (SSRF-guarded)
    # ------------------------------------------------------------------
    @api.model
    def _leaders_check_image_url(self, url):
        """An error message, or False when the URL is safe to fetch."""
        parsed = urlparse(url or "")
        if parsed.scheme not in ("http", "https") or not parsed.hostname or len(url) > 2048:
            return _("Only http and https image addresses are allowed.")
        try:
            infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError):
            return _("The image host %s cannot be resolved.", parsed.hostname)
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0].split("%")[0])
            except ValueError:
                return _("The image host %s has an invalid address.", parsed.hostname)
            if not address.is_global:
                return _("The image address points to a private or local network and was not fetched.")
        return False

    @api.model
    def _leaders_fetch_image(self, url):
        """``(image_b64, warning)``: a failure is a warning, never an exception."""
        max_bytes = self._leaders_image_max_bytes()
        current = url
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                problem = self._leaders_check_image_url(current)
                if problem:
                    return False, problem
                response = requests.get(current, timeout=IMAGE_TIMEOUT, stream=True, allow_redirects=False,
                                        headers={"User-Agent": "LeadersMarketplaceImport/1.0"})
                try:
                    if response.status_code in (301, 302, 303, 307, 308):
                        current = urljoin(current, response.headers.get("Location", ""))
                        continue
                    if response.status_code != 200:
                        return False, _("The image server answered HTTP %s.", response.status_code)
                    length = str(response.headers.get("Content-Length") or "")
                    too_large = _("The image is too large (max %(size)s MB).", size=max_bytes // (1024 * 1024))
                    if length.isdigit() and int(length) > max_bytes:
                        return False, too_large
                    data = b""
                    for chunk in response.iter_content(65536):
                        data += chunk
                        if len(data) > max_bytes:
                            return False, too_large
                finally:
                    response.close()
                if not data:
                    return False, _("The downloaded image is empty.")
                if not guess_mimetype(data, default="").startswith("image/"):
                    return False, _("The address did not return an image (PNG, JPEG, GIF, WebP).")
                return base64.b64encode(data), False
            return False, _("The image address redirects too many times.")
        except requests.exceptions.Timeout:
            return False, _("The image download timed out after %s seconds.", IMAGE_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            return False, _("The image could not be downloaded: %s", exc.__class__.__name__)


class VendorImportLine(models.Model):
    _name = "leaders.vendor.import.line"
    _description = "Vendor Bulk Import Row"
    _order = "import_id, row_number"

    import_id = fields.Many2one("leaders.vendor.import", required=True, ondelete="cascade", index=True)
    row_number = fields.Integer(required=True)
    status = fields.Selection([
        ("valid", "Valid"), ("invalid", "Invalid"), ("applied", "Applied"), ("failed", "Failed"),
    ], required=True, default="valid", index=True)
    payload = fields.Text(help="The validated row as JSON: the apply step uses exactly this.")
    messages = fields.Text(help="JSON list of {column, message, level}.")
    offer_id = fields.Many2one("leaders.marketplace.offer", ondelete="set null")

    def _leaders_add_message(self, column, message, level):
        self.ensure_one()
        messages = json.loads(self.messages or "[]")
        messages.append({"column": column, "message": message, "level": level})
        self.messages = json.dumps(messages)

    def _leaders_apply(self):
        """Apply one validated row in a savepoint; the rules are re-checked first."""
        self.ensure_one()
        job = self.import_id
        try:
            with self.env.cr.savepoint():
                payload = json.loads(self.payload or "{}")
                errors, warnings, resolved = job._leaders_check_rules(payload, {})
                if errors:
                    raise UserError("; ".join("%s: %s" % (c, m) for c, m in errors))
                offer, extra = job._leaders_apply_payload(payload, resolved)
            self.write({"status": "applied", "offer_id": offer.id if offer else False})
            for column, message in warnings + extra:
                self._leaders_add_message(column, message, "warning")
        except (UserError, ValidationError, AccessError) as exc:
            self._leaders_mark_failed(str(exc))
        except Exception as exc:
            _logger.exception("Vendor import %s row %s crashed", job.id, self.row_number)
            self._leaders_mark_failed(_("Unexpected error: %s", exc))

    def _leaders_mark_failed(self, message):
        self.write({"status": "failed"})
        self._leaders_add_message("", message, "error")
        self.import_id._log(_("Row %(row)s failed: %(message)s", row=self.row_number, message=message))


class VendorImportApply(models.Model):
    """Apply step of the import, kept next to the job so the rules stay in one place."""
    _inherit = "leaders.vendor.import"

    def _leaders_apply_payload(self, payload, resolved):
        """Create/update through the same models the portal forms use.

        :return: ``(offer, extra_warnings)``
        """
        self.ensure_one()
        vendor = self.vendor_id.commercial_partner_id
        Offer = self.env["leaders.marketplace.offer"].sudo()
        warnings = []
        if self.kind == "update":
            offer = resolved["offer"]
            if payload.get("price") is not None:
                offer.price = payload["price"]
            if payload.get("stock") is not None:
                offer._leaders_set_fbm_stock(payload["stock"])
            if payload.get("clear_discount"):
                offer._leaders_clear_discount()
            if resolved.get("discount"):
                state = offer._leaders_set_discount(self._leaders_discount_values(payload))
                if state == "pending_approval":
                    warnings.append(("discount_percent", _("The discount is above the limit and waits for approval.")))
            return offer, warnings

        fulfillment = payload.get("fulfillment") or "fbm"
        if self.kind == "new_products":
            # Same creation as the portal "Add Product" form.
            template_vals = {
                "name": payload["name"],
                "description_sale": payload.get("description") or False,
                "type": "consu", "is_storable": True, "sale_ok": True, "purchase_ok": False,
                "marketplace_state": "draft",
                "marketplace_owner_vendor_id": vendor.id,
            }
            if payload.get("image_url"):
                image, problem = self._leaders_fetch_image(payload["image_url"])
                if image:
                    template_vals["image_1920"] = image
                else:
                    warnings.append(("image_url", problem))
            if resolved.get("category"):
                template_vals["public_categ_ids"] = [(6, 0, resolved["category"].ids)]
            template = self.env["product.template"].sudo().create(template_vals)
            product = template.product_variant_id
        else:
            product = resolved["product"]
            template = product.product_tmpl_id
        offer = Offer.create({
            "product_tmpl_id": template.id, "product_id": product.id, "vendor_id": vendor.id,
            "seller_sku": payload["sku"], "price": payload["price"],
            "currency_id": self.env.company.currency_id.id,
            "fulfillment_type": fulfillment,
            "stock_location_id": vendor._marketplace_stock_location(fulfillment).id,
            "market_ids": [(6, 0, resolved["markets"].ids)],
        })
        qty = payload.get("initial_qty") or 0.0
        if qty > 0 and offer.fulfillment_type == "fbm":
            location = vendor._marketplace_stock_location("fbm")
            self.env["stock.quant"].sudo()._leaders_adjust_vendor_stock(
                offer.product_id, location, vendor, qty,
                reason=_("Vendor bulk import stock (%s)", offer.seller_sku))
        if resolved.get("discount"):
            state = offer._leaders_set_discount(self._leaders_discount_values(payload))
            if state == "pending_approval":
                warnings.append(("discount_percent", _("The discount is above the limit and waits for approval.")))
        if self.submit_after_import:
            offer._leaders_vendor_submit(vendor)
        return offer, warnings


class MarketplaceOfferImportHelpers(models.Model):
    """Shared vendor actions used by the portal routes and the bulk import."""
    _inherit = "leaders.marketplace.offer"

    def _leaders_set_fbm_stock(self, qty, check_only=False):
        """Set the FBM quantity (portal 'set' mode: correction by delta, never below reserved).

        Raises UserError with the portal's messages.
        """
        self.ensure_one()
        vendor = self.vendor_id.commercial_partner_id
        if self.fulfillment_type != "fbm":
            raise UserError(_("FBL stock is controlled by warehouse inbound receipts, not direct vendor adjustments."))
        if not self.product_id.is_storable:
            raise UserError(_("Marketplace stock updates require a goods product, not a service."))
        if qty < 0:
            raise UserError(_("The stock quantity cannot be negative."))
        location = vendor._marketplace_stock_location("fbm")
        rounding = self.product_id.uom_id.rounding
        quants = self.env["stock.quant"].sudo().search([
            ("product_id", "=", self.product_id.id), ("location_id", "=", location.id),
            ("owner_id", "=", vendor.id), ("lot_id", "=", False), ("package_id", "=", False),
        ])
        reserved = sum(quants.mapped("reserved_quantity"))
        if float_compare(qty, reserved, precision_rounding=rounding) < 0:
            raise UserError(_(
                "You cannot set the stock of %(product)s to %(qty)s: %(reserved)s are already reserved for open orders.",
                product=self.product_id.display_name, qty=qty, reserved=reserved))
        if check_only:
            return True
        delta = qty - sum(quants.mapped("quantity"))
        if not float_is_zero(delta, precision_rounding=rounding):
            if self.stock_location_id != location:
                self.sudo().stock_location_id = location
            self.env["stock.quant"].sudo()._leaders_adjust_vendor_stock(
                self.product_id, location, vendor, delta,
                reason=_("Vendor portal stock correction (%s)", self.seller_sku))
        return True

    def _leaders_vendor_submit(self, vendor):
        """Send the offer (and its product, when the vendor owns the content) for review."""
        self.ensure_one()
        if self.product_tmpl_id.sudo()._leaders_vendor_owns_content(vendor):
            self.product_tmpl_id.sudo().write({"marketplace_state": "pending", "is_published": False})
        self.sudo().action_submit()
        return True
