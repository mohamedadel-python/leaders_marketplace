"""B2: vendor bulk import."""
import base64
import csv
import io
from unittest.mock import patch

import requests

from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD
from .test_vendor_split import MarketplaceVendorCommon

PARAM = "leaders_marketplace.vendor_bulk_import_enabled"
BATCH = "leaders_marketplace.vendor_import_batch_size"
FETCH = "odoo.addons.leaders_marketplace.models.vendor_import.requests.get"
# 1x1 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
NEW_HEADERS = ["name", "description", "category", "sku", "price", "fulfillment", "initial_qty",
               "image_url", "markets", "discount_percent", "discount_price", "discount_start", "discount_end"]


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def close(self):
        pass


def make_csv(headers, rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    writer.writerows(rows)
    return out.getvalue().encode()


def new_row(**kw):
    row = dict.fromkeys(NEW_HEADERS, "")
    row.update(kw)
    return [row[h] for h in NEW_HEADERS]


class ImportCommon(MarketplaceVendorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Import = cls.env["leaders.vendor.import"]
        cls.Offer = cls.env["leaders.marketplace.offer"]
        cls.param = cls.env["ir.config_parameter"].sudo()
        cls.param.set_param(PARAM, "True")
        cls.param.set_param("leaders_marketplace.vendor_discounts_enabled", "True")
        cls.param.set_param("leaders_marketplace.catalog_offers_enabled", "True")
        cls.category = cls.env["product.public.category"].create({"name": "Import Kitchen"})
        cls.tier = cls.env["leaders.vendor.tier"].create({"name": "Imp Tier", "code": "imp-tier", "product_limit": 0})
        cls.vendor.vendor_tier_id = cls.tier
        # a published catalog product with a barcode
        cls.catalog = cls.own_product
        cls.catalog.write({"barcode": "6223001111111", "default_code": "CAT-REF-1"})
        cls.catalog.product_tmpl_id.write({"marketplace_state": "published", "is_published": True, "sale_ok": True})

    def _job(self, headers, rows, kind="new_products", filename="data.csv", submit=False, vendor=None):
        job = self.Import.create({
            "vendor_id": (vendor or self.vendor).id, "user_id": self.env.user.id, "kind": kind,
            "file": base64.b64encode(make_csv(headers, rows)), "filename": filename,
            "submit_after_import": submit})
        job.action_validate()
        return job

    def _new_job(self, rows, **kw):
        return self._job(NEW_HEADERS, rows, **kw)

    def _errors(self, job):
        return job._leaders_error_rows()

    def _run(self, job):
        job.action_confirm()
        while job.state in ("confirmed", "processing"):
            self.Import.cron_process_imports()
        return job


@tagged("post_install", "-at_install")
class TestVendorImportValidation(ImportCommon):

    def test_new_products_good_and_bad_rows_report_row_column_message(self):
        job = self._new_job([
            new_row(name="Mug", sku="IMP-1", price="9.5", category="Import Kitchen"),
            new_row(name="", sku="IMP-2", price="3"),
            new_row(name="Cup", sku="IMP-3", price="-4"),
            new_row(name="Bowl", sku="IMP-1", price="4"),
            new_row(name="Fork", sku="FBM-1", price="4"),
            new_row(name="Spoon", sku="IMP-6", price="4", category="Nope"),
            new_row(name="Knife", sku="IMP-7", price="abc"),
        ])
        self.assertEqual(job.state, "validated")
        self.assertEqual((job.total_rows, job.valid_rows, job.invalid_rows), (7, 1, 6))
        errors = {(r, c): m for r, c, lvl, m in self._errors(job) if lvl == "error"}
        self.assertIn("name", {c for (r, c) in errors if r == 3})
        self.assertIn("cannot be negative", errors[(4, "price")])
        self.assertIn("Duplicate sku", errors[(5, "sku")])
        self.assertIn("already used", errors[(6, "sku")])
        self.assertIn("does not exist", errors[(7, "category")])
        self.assertIn("must be a number", errors[(8, "price")])

    def test_error_file_is_csv_and_xlsx(self):
        job = self._new_job([new_row(name="", sku="E-1", price="1")])
        content, mime, name = job._leaders_build_error_file("csv")
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["row", "column", "level", "message"])
        self.assertEqual(rows[1][:3], ["2", "name", "error"])
        self.assertTrue(job.error_file)
        import openpyxl
        content, mime, name = job._leaders_build_error_file("xlsx")
        ws = openpyxl.load_workbook(io.BytesIO(content)).active
        self.assertEqual([c.value for c in ws[2]][:3], [2, "name", "error"])

    def test_missing_required_column_fails_the_file(self):
        job = self._job(["name", "price"], [["A", "1"]])
        self.assertEqual(job.state, "failed")
        self.assertIn("Missing column", job.log)
        self.assertEqual(len(self._errors(job)), 1)

    def test_example_row_is_skipped_and_templates_load(self):
        for kind in ("new_products", "catalog_offers", "update"):
            for fmt in ("csv", "xlsx"):
                content, mime, name = self.Import._leaders_build_template(kind, fmt)
                self.assertTrue(content)
        content, _mime, _n = self.Import._leaders_build_template("new_products", "csv")
        job = self.Import.create({"vendor_id": self.vendor.id, "kind": "new_products",
                                  "file": base64.b64encode(content), "filename": "t.csv"})
        job.action_validate()
        self.assertEqual(job.total_rows, 0)
        content, _mime, _n = self.Import._leaders_build_template("update", "xlsx")
        job = self.Import.create({"vendor_id": self.vendor.id, "kind": "update",
                                  "file": base64.b64encode(content), "filename": "t.xlsx"})
        job.action_validate()
        self.assertEqual((job.state, job.total_rows), ("validated", 0))

    def test_fbl_row_refused_when_fbl_is_off(self):
        self.param.set_param("leaders_marketplace.fbl_enabled", "False")
        job = self._new_job([new_row(name="A", sku="F-1", price="1", fulfillment="fbl"),
                             new_row(name="B", sku="F-2", price="1", fulfillment="fbm")])
        self.assertEqual((job.valid_rows, job.invalid_rows), (1, 1))
        self.assertIn("not available", self._errors(job)[0][3])

    def test_fbl_with_initial_qty_and_bad_fulfillment_refused(self):
        job = self._new_job([new_row(name="A", sku="F-1", price="1", fulfillment="fbl", initial_qty="5"),
                             new_row(name="B", sku="F-2", price="1", fulfillment="xyz"),
                             new_row(name="C", sku="F-3", price="1", fulfillment="fbl")])
        self.assertEqual((job.valid_rows, job.invalid_rows), (1, 2))

    def test_forced_policy_warns_when_stock_cannot_be_added(self):
        self.env["leaders.fulfillment.policy"].create({
            "name": "Force FBL", "vendor_id": self.vendor.id, "forced_fulfillment_type": "fbl"})
        job = self._new_job([new_row(name="A", sku="P-1", price="1", initial_qty="5")])
        self.assertEqual(job.valid_rows, 1)
        self._run(job)
        offer = self.Offer.search([("vendor_id", "=", self.vendor.id), ("seller_sku", "=", "P-1")])
        self.assertEqual(offer.fulfillment_type, "fbl")
        levels = [(lvl, c) for r, c, lvl, m in self._errors(job)]
        self.assertIn(("warning", "initial_qty"), levels)

    def test_product_limit_counts_the_rows_of_the_file(self):
        self.tier.product_limit = 4  # the vendor already has 2 offers
        job = self._new_job([new_row(name=str(i), sku="L-%s" % i, price="1") for i in range(4)])
        self.assertEqual((job.valid_rows, job.invalid_rows), (1 + 1, 2))
        self.assertIn("tier product limit", self._errors(job)[0][3])

    def test_limit_is_rechecked_at_apply(self):
        job = self._new_job([new_row(name=str(i), sku="L-%s" % i, price="1") for i in range(3)])
        self.assertEqual(job.valid_rows, 3)
        self.tier.product_limit = 3
        self._run(job)
        self.assertEqual((job.applied_rows, job.failed_rows), (1, 2))
        self.assertEqual(job.state, "done")

    def test_markets_must_be_known_and_allowed(self):
        site_a = self.env["website"].search([], limit=1)
        site_b = self.env["website"].create({"name": "Import Market B"})
        self.vendor.vendor_market_ids = site_a
        job = self._new_job([
            new_row(name="A", sku="M-1", price="1", markets=site_a.name),
            new_row(name="B", sku="M-2", price="1", markets="Import Market B"),
            new_row(name="C", sku="M-3", price="1", markets="Ghost Market")])
        self.assertEqual((job.valid_rows, job.invalid_rows), (1, 2))
        self.assertTrue(site_b)

    def test_discount_columns(self):
        job = self._new_job([
            new_row(name="A", sku="D-1", price="100", discount_percent="20"),
            new_row(name="B", sku="D-2", price="100", discount_percent="120"),
            new_row(name="C", sku="D-3", price="100", discount_price="150"),
            new_row(name="D", sku="D-4", price="100", discount_price="80", discount_start="2999-01-01"),
            new_row(name="E", sku="D-5", price="100", discount_percent="10", discount_price="50")])
        self.assertEqual((job.valid_rows, job.invalid_rows), (2, 3))
        self._run(job)
        get = lambda sku: self.Offer.search([("seller_sku", "=", sku), ("vendor_id", "=", self.vendor.id)])
        self.assertEqual(get("D-1").discount_state, "active")
        self.assertEqual(get("D-4").discount_state, "scheduled")
        self.assertEqual(get("D-1").effective_price, 80.0)

    def test_discount_refused_when_the_master_switch_is_off(self):
        self.param.set_param("leaders_marketplace.vendor_discounts_enabled", "False")
        job = self._new_job([new_row(name="A", sku="D-1", price="100", discount_percent="20")])
        self.assertEqual(job.invalid_rows, 1)
        self.assertIn("not enabled", self._errors(job)[0][3])


@tagged("post_install", "-at_install")
class TestVendorImportApply(ImportCommon):

    def _get(self, sku):
        return self.Offer.search([("seller_sku", "=", sku), ("vendor_id", "=", self.vendor.id)])

    def test_confirm_applies_only_valid_rows_as_drafts(self):
        job = self._new_job([
            new_row(name="Mug", description="Nice", sku="A-1", price="9.5", category="Import Kitchen", initial_qty="7"),
            new_row(name="", sku="A-2", price="1"),
        ])
        self.assertEqual(self.Offer.search_count([("seller_sku", "=", "A-1")]), 0)
        self._run(job)
        self.assertEqual((job.state, job.applied_rows, job.failed_rows), ("done", 1, 0))
        offer = self._get("A-1")
        self.assertEqual(offer.state, "draft")
        self.assertEqual(offer.price, 9.5)
        tmpl = offer.product_tmpl_id
        self.assertEqual(tmpl.marketplace_state, "draft")
        self.assertEqual(tmpl.marketplace_owner_vendor_id, self.vendor)
        self.assertTrue(tmpl.is_storable)
        self.assertEqual(tmpl.public_categ_ids, self.category)
        quant = self.env["stock.quant"].search([
            ("product_id", "=", offer.product_id.id), ("owner_id", "=", self.vendor.id),
            ("location_id", "=", self.vendor.vendor_location_id.id)])
        self.assertEqual(sum(quant.mapped("quantity")), 7.0)
        self.assertFalse(self._get("A-2"))

    def test_batches_are_resumable(self):
        self.param.set_param(BATCH, "2")
        job = self._new_job([new_row(name="P%s" % i, sku="B-%s" % i, price="1") for i in range(5)])
        job.action_confirm()
        self.Import.cron_process_imports()
        self.assertEqual((job.state, job.applied_rows), ("processing", 2))
        self.Import.cron_process_imports()
        self.assertEqual((job.state, job.applied_rows), ("processing", 4))
        self.Import.cron_process_imports()
        self.assertEqual((job.state, job.applied_rows), ("done", 5))
        self.Import.cron_process_imports()
        self.assertEqual(self.Offer.search_count([
            ("vendor_id", "=", self.vendor.id), ("seller_sku", "in", ["B-%s" % i for i in range(5)])]), 5)

    def test_rows_are_rechecked_at_apply(self):
        job = self._new_job([new_row(name="A", sku="R-1", price="1"), new_row(name="B", sku="R-2", price="1")])
        self.assertEqual(job.valid_rows, 2)
        other = self.env["product.product"].create({"name": "Other", "type": "consu", "is_storable": True})
        self.Offer.create({
            "product_tmpl_id": other.product_tmpl_id.id, "product_id": other.id, "vendor_id": self.vendor.id,
            "seller_sku": "R-1", "price": 1.0, "fulfillment_type": "fbm",
            "stock_location_id": self.vendor._marketplace_stock_location("fbm").id})
        self._run(job)
        self.assertEqual((job.applied_rows, job.failed_rows), (1, 1))
        self.assertIn("Row 2 failed", job.log)
        self.assertTrue(self._get("R-2"))

    def test_only_confirmed_valid_jobs_apply(self):
        job = self._new_job([new_row(name="", sku="X-1", price="1")])
        with self.assertRaises(Exception):
            job.action_confirm()
        ok = self._new_job([new_row(name="A", sku="X-2", price="1")])
        self.Import.cron_process_imports()
        self.assertFalse(self._get("X-2"))
        ok.action_confirm()
        self.Import.cron_process_imports()
        self.assertTrue(self._get("X-2"))

    def test_frozen_vendor_import_stops(self):
        job = self._new_job([new_row(name="A", sku="Z-1", price="1")])
        job.action_confirm()
        self.vendor.vendor_state = "frozen"
        self.Import.cron_process_imports()
        self.assertEqual(job.state, "failed")
        self.assertFalse(self._get("Z-1"))

    def test_submit_after_import(self):
        job = self._new_job([new_row(name="A", sku="S-1", price="5")], submit=True)
        self._run(job)
        offer = self._get("S-1")
        self.assertEqual(offer.state, "pending")
        self.assertEqual(offer.product_tmpl_id.marketplace_state, "pending")

    def test_image_failure_is_a_warning_and_the_product_is_created(self):
        job = self._new_job([new_row(name="A", sku="I-1", price="5", image_url="http://93.184.216.34/a.png")])
        with patch(FETCH, side_effect=requests.exceptions.Timeout()):
            self._run(job)
        self.assertEqual(job.applied_rows, 1)
        offer = self._get("I-1")
        self.assertFalse(offer.product_tmpl_id.image_1920)
        self.assertIn(("warning", "image_url"), [(lvl, c) for r, c, lvl, m in self._errors(job)])

    def test_image_is_downloaded(self):
        job = self._new_job([new_row(name="A", sku="I-2", price="5", image_url="http://93.184.216.34/a.png")])
        with patch(FETCH, return_value=FakeResponse(PNG, headers={"Content-Length": str(len(PNG))})) as get:
            self._run(job)
        self.assertEqual(get.call_args.kwargs["timeout"], 10)
        self.assertTrue(self._get("I-2").product_tmpl_id.image_1920)

    def test_image_fetch_guards(self):
        fetch = self.Import._leaders_fetch_image
        with patch(FETCH, side_effect=requests.exceptions.Timeout()):
            self.assertIn("timed out", fetch("http://93.184.216.34/a.png")[1])
        with patch(FETCH, return_value=FakeResponse(b"<html>nope</html>")):
            self.assertIn("did not return an image", fetch("http://93.184.216.34/a.png")[1])
        self.param.set_param("leaders_marketplace.vendor_image_max_mb", "1")
        big = FakeResponse(b"\x89PNG" + b"0" * (1024 * 1024 + 10))
        with patch(FETCH, return_value=big):
            self.assertIn("too large", fetch("http://93.184.216.34/a.png")[1])
        with patch(FETCH, return_value=FakeResponse(b"", headers={"Content-Length": str(5 * 1024 * 1024)})):
            self.assertIn("too large", fetch("http://93.184.216.34/a.png")[1])
        with patch(FETCH, return_value=FakeResponse(b"", status=404)):
            self.assertIn("HTTP 404", fetch("http://93.184.216.34/a.png")[1])
        for url in ("http://127.0.0.1/a.png", "http://10.0.0.5/a.png", "http://192.168.1.1/a.png",
                    "http://[::1]/a.png", "http://169.254.169.254/latest"):
            with patch(FETCH) as get:
                self.assertIn("private", fetch(url)[1], url)
                get.assert_not_called()
        with patch(FETCH) as get:
            self.assertIn("http", fetch("ftp://93.184.216.34/a.png")[1])
            get.assert_not_called()
        redirect = FakeResponse(b"", status=302, headers={"Location": "http://127.0.0.1/secret.png"})
        with patch(FETCH, return_value=redirect) as get:
            self.assertIn("private", fetch("http://93.184.216.34/a.png")[1])
            self.assertEqual(get.call_count, 1)

    def test_catalog_offers(self):
        headers = ["barcode", "internal_reference", "sku", "price", "fulfillment", "initial_qty"]
        dup1 = self.env["product.product"].create({"name": "Dup 1", "default_code": "DUP-REF", "type": "consu", "sale_ok": True})
        dup2 = self.env["product.product"].create({"name": "Dup 2", "default_code": "DUP-REF", "type": "consu", "sale_ok": True})
        (dup1 | dup2).product_tmpl_id.write({"marketplace_state": "published"})
        job = self._job(headers, [
            ["6223001111111", "", "C-1", "20", "fbm", "4"],
            ["", "NOPE", "C-2", "20", "", ""],
            ["", "DUP-REF", "C-3", "20", "", ""],
            ["6223001111111", "", "C-4", "20", "fbm", ""],
            ["", "CAT-REF-1", "C-5", "20", "fbl", ""],
            ["", "", "C-6", "20", "", ""],
        ], kind="catalog_offers")
        self.assertEqual((job.valid_rows, job.invalid_rows), (2, 4))
        msgs = {r: m for r, c, l, m in self._errors(job) if l == "error"}
        self.assertIn("No published catalog product", msgs[3])
        self.assertIn("ambiguous", msgs[4])
        self.assertIn("already have an offer", msgs[5])
        self._run(job)
        offer = self._get("C-1")
        self.assertEqual((offer.product_id, offer.state), (self.catalog, "draft"))
        self.assertTrue(self._get("C-5"))

    def test_catalog_offers_need_the_catalog_setting(self):
        self.param.set_param("leaders_marketplace.catalog_offers_enabled", "False")
        job = self._job(["barcode", "sku", "price"], [["6223001111111", "C-1", "20"]], kind="catalog_offers")
        self.assertEqual(job.valid_rows, 0)

    def test_update_by_seller_sku(self):
        headers = ["sku", "price", "stock", "discount_percent", "discount_price", "discount_start",
                   "discount_end", "clear_discount"]
        job = self._job(headers, [
            ["FBM-1", "120", "9", "25", "", "", "", ""],
            ["FBL-1", "", "5", "", "", "", "", ""],
            ["NOPE", "1", "", "", "", "", "", ""],
            ["FBM-1", "1", "", "", "", "", "", ""],
        ], kind="update")
        self.assertEqual((job.valid_rows, job.invalid_rows), (1, 3))
        self._run(job)
        offer = self.fbm_offer
        self.assertEqual(offer.price, 120.0)
        self.assertEqual(offer.discount_state, "active")
        quant = self.env["stock.quant"].search([
            ("product_id", "=", self.fbm_product.id), ("owner_id", "=", self.vendor.id),
            ("location_id", "=", self.vendor.vendor_location_id.id)])
        self.assertEqual(sum(quant.mapped("quantity")), 9.0)
        job = self._job(headers, [["FBM-1", "", "", "", "", "", "", "yes"]], kind="update")
        self._run(job)
        self.assertEqual(offer.discount_state, "none")

    def test_update_stock_below_reserved_is_refused(self):
        order = self._order([(self.fbm_product, 3)])
        order.action_confirm()
        job = self._job(["sku", "stock"], [["FBM-1", "1"], ["FBM-1", "10"]], kind="update")
        self.assertEqual(job.invalid_rows, 2)  # duplicate, and below the reserved quantity
        self.assertIn("reserved", " ".join(m for _r, _c, _l, m in self._errors(job)))

    def test_update_negative_and_empty_rows(self):
        job = self._job(["sku", "price", "stock"], [["FBM-1", "-1", ""], ["FBM-1", "", "-2"], ["FBM-1", "", ""]],
                        kind="update")
        self.assertEqual(job.valid_rows, 0)


@tagged("post_install", "-at_install")
class TestVendorImportPortal(HttpCase, MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.param = cls.env["ir.config_parameter"].sudo()
        cls.param.set_param(PARAM, "True")
        cls.other = cls.env["res.partner"].create({
            "name": "Import Rival", "is_marketplace_vendor": True, "vendor_state": "approved"})
        cls.Import = cls.env["leaders.vendor.import"]
        cls.rival_job = cls.Import.create({
            "vendor_id": cls.other.id, "kind": "new_products", "filename": "r.csv",
            "file": base64.b64encode(make_csv(NEW_HEADERS, [new_row(name="R", sku="RV-1", price="1")]))})
        cls.rival_job.action_validate()

    def setUp(self):
        super().setUp()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)

    def _upload(self, rows, name="up.csv"):
        token = self._csrf("/my/vendor/import")
        response = self.url_open("/my/vendor/import/upload", data={"csrf_token": token, "kind": "new_products"},
                                 files={"file": (name, make_csv(NEW_HEADERS, rows))})
        self.assertEqual(response.status_code, 200)
        return self.Import.search([("vendor_id", "=", self.vendor.id)], order="id desc", limit=1)

    def test_feature_off_is_404_everywhere(self):
        self.param.set_param(PARAM, "False")
        for url in ("/my/vendor/import", "/my/vendor/import/template/update.csv",
                    "/my/vendor/import/%s" % self.rival_job.id):
            self.assertEqual(self.url_open(url).status_code, 404, url)
        self.assertNotIn("/my/vendor/import", self.url_open("/my/vendor/products").text)

    def test_link_visible_when_on(self):
        self.assertIn("/my/vendor/import", self.url_open("/my/vendor/products").text)
        page = self.url_open("/my/vendor/import")
        self.assertEqual(page.status_code, 200)
        self.assertIn("template/new_products.xlsx", page.text)

    def test_upload_confirm_and_download(self):
        job = self._upload([new_row(name="Web A", sku="W-1", price="3"), new_row(name="", sku="W-2", price="3")])
        self.assertEqual((job.state, job.valid_rows, job.invalid_rows), ("validated", 1, 1))
        page = self.url_open("/my/vendor/import/%s" % job.id)
        self.assertIn("Confirm import of", page.text)
        for fmt in ("csv", "xlsx"):
            response = self.url_open("/my/vendor/import/%s/errors.%s" % (job.id, fmt))
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn(b"name", self.url_open("/my/vendor/import/%s/errors.csv" % job.id).content)
        token = self._csrf("/my/vendor/import/%s" % job.id)
        self.url_open("/my/vendor/import/%s/confirm" % job.id, data={"csrf_token": token})
        self.assertEqual(job.state, "confirmed")
        self.Import.cron_process_imports()
        self.assertEqual(job.state, "done")
        self.assertTrue(self.env["leaders.marketplace.offer"].search([("seller_sku", "=", "W-1")]))

    def test_template_download(self):
        for fmt in ("csv", "xlsx"):
            response = self.url_open("/my/vendor/import/template/new_products.%s" % fmt)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content)
        self.assertEqual(self.url_open("/my/vendor/import/template/bogus.csv").status_code, 404)

    def test_bad_upload_shows_an_error(self):
        token = self._csrf("/my/vendor/import")
        response = self.url_open("/my/vendor/import/upload", data={"csrf_token": token, "kind": "new_products"},
                                 files={"file": ("x.txt", b"hello")})
        self.assertIn("Upload an .xlsx or .csv file", response.text)

    def test_other_vendors_job_is_404(self):
        job = self.rival_job
        self.assertEqual(self.url_open("/my/vendor/import/%s" % job.id).status_code, 404)
        self.assertEqual(self.url_open("/my/vendor/import/%s/errors.csv" % job.id).status_code, 404)
        token = self._csrf("/my/vendor/import")
        response = self.url_open("/my/vendor/import/%s/confirm" % job.id, data={"csrf_token": token})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(job.state, "validated")

    def test_frozen_vendor_cannot_import(self):
        self.vendor.vendor_state = "frozen"
        self.assertIn(self.url_open("/my/vendor/import").status_code, (401, 403))
