"""Redesigned vendor portal pages, English and Arabic (TASK 29 / C2b)."""
import base64
import datetime

from lxml import html as lxml_html

from odoo import Command, fields
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import PORTAL_PASSWORD, MarketplacePortalUserCommon
from .test_vendor_import import NEW_HEADERS, make_csv

PARAMS = {
    "vendor_bulk_import_enabled": "True", "vendor_api_write_enabled": "True",
    "vendor_discounts_enabled": "True", "vendor_variants_enabled": "True",
    "shipping_providers_enabled": "True", "vendor_can_cancel_orders": "True",
    "catalog_offers_enabled": "True", "vendor_apply_enabled": "True",
}


@tagged("post_install", "-at_install")
class TestVendorPortalPages(HttpCase, MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        param = cls.env["ir.config_parameter"].sudo()
        for key, value in PARAMS.items():
            param.set_param("leaders_marketplace." + key, value)
        cls.env["leaders.vendor.terms"].with_context(active_test=False).search([]).write({"active": False})
        website = cls.env["website"].search([], limit=1)
        lang = cls.env["res.lang"]._activate_lang("ar_001")
        website.language_ids = [Command.link(lang.id)]
        cls.ar_prefix = "/" + lang.url_code
        # data for every page
        order = cls._confirmed_sale_order(cls.fbm_product, 2)
        cls.vendor_order = order.vendor_order_ids[:1]
        # an owned template with a variant offer: the product form shows every section
        cls.tmpl = cls.env["product.template"].create({
            "name": "C2b Shirt", "type": "consu", "is_storable": True, "sale_ok": True,
            "marketplace_state": "draft", "marketplace_owner_vendor_id": cls.vendor.id})
        cls.offer = cls.env["leaders.marketplace.offer"]._leaders_create_variant_offers(
            cls.vendor, cls.tmpl, [{"product": cls.tmpl.product_variant_ids[:1], "sku": "C2B-1", "price": 10.0,
                                    "stock": 0.0}], "fbm", cls.env.company.currency_id, cls.env["website"])[:1]
        shipped = cls._confirmed_sale_order(cls.fbm_product, 1).vendor_order_ids[:1]
        group = shipped.fulfillment_group_ids[:1]
        group.tracking_ref = "TRK-C2B-0"
        group.action_ship()
        cls.return_rec = cls.env["leaders.vendor.return"].create({
            "vendor_order_id": shipped.id, "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": shipped.line_ids[:1].id, "quantity": 1})],
        })
        cls.dispute = cls.env["leaders.vendor.dispute"].create({
            "vendor_order_id": shipped.id, "reason": "Where is it"})
        period = fields.Date.today().replace(day=1)
        KPI = cls.env["leaders.vendor.kpi"]
        cls.kpi = KPI.search([("vendor_id", "=", cls.vendor.id), ("period", "=", period)]) or KPI.create({
            "vendor_id": cls.vendor.id, "period": period, "order_count": 4, "late_shipment_rate": 25.0})
        cls.env["leaders.vendor.penalty"].create({
            "vendor_id": cls.vendor.id, "level": "warning", "reason": "Late shipping"})
        cls.env["leaders.vendor.api.key"]._leaders_generate(cls.vendor, cls.owner_user, "ERP", "read")
        cls.job = cls.env["leaders.vendor.import"].create({
            "vendor_id": cls.vendor.id, "user_id": cls.env.user.id, "kind": "new_products",
            "file": base64.b64encode(make_csv(NEW_HEADERS, [])), "filename": "data.csv"})
        cls.job.action_validate()
        cls.application = cls.env["leaders.vendor.application"].create({
            "business_name": "Vis Co", "store_name": "Vis", "legal_type": "company",
            "legal_name": "Vis LLC", "commercial_registration_number": "1", "tax_number": "3",
            "business_activity": "x", "owner_name": "O", "owner_email": "vis.owner@example.com",
            "representative_name": "O", "representative_position": "GM", "representative_id_number": "1",
            "bank_name": "B", "bank_account_holder": "H", "bank_account_number": "SA0380000000608010167519",
            "terms_accepted": True, "seller_agreement_accepted": True,
            "document_ids": [Command.create({
                "name": "id_passport", "document_type": "id_passport", "number": "N",
                "file": base64.b64encode(b"scan"), "file_name": "id.pdf"})],
        })
        cls.application._portal_ensure_token()
        cls.env.cr.flush()

    # -- helpers ------------------------------------------------------------
    def _page(self, path, lang, status=200):
        # The session keeps the last language: visit every page in one language, then the other.
        prefix = self.ar_prefix if lang == "ar" else ""
        response = self.url_open(prefix + path)
        self.assertEqual(response.status_code, status, prefix + path)
        dom = lxml_html.fromstring(response.content)
        rtl = dom.xpath("//div[@id='wrapwrap' and contains(@class, 'o_rtl')]")
        self.assertEqual(bool(rtl), lang == "ar", "wrong text direction on %s (%s)" % (path, lang))
        return dom

    def _assert_vendor_shell(self, dom, path):
        main = dom.xpath("//main[contains(@class, 'lm-vendor-main')]")
        self.assertTrue(main, "no vendor layout on %s" % path)
        self.assertTrue(main[0].xpath(".//h1[contains(@class, 'lm-page-title')]"), "no page header on %s" % path)
        self.assertTrue(dom.xpath("//ol[@id='lm_vendor_breadcrumbs']//li"), "breadcrumbs lost on %s" % path)
        self.assertTrue(dom.xpath("//nav[contains(@class, 'lm-vendor-nav')]"))
        self.assertFalse(main[0].xpath("./div[contains(@class, 'container')]"), "old wrapper on %s" % path)
        self.assertFalse(main[0].xpath(".//div[contains(@class, 'my-4') and contains(@class, 'lm-portal')]"))
        for cell in main[0].xpath(".//table[contains(@class, 'lm-table')]//tbody/tr/td[normalize-space(.) != '']"):
            classes = (cell.get("class") or "").split()
            self.assertTrue(
                cell.get("data-label") or "lm-cell-title" in classes or "lm-cell-actions" in classes,
                "%s: unlabelled cell %s" % (path, lxml_html.tostring(cell)[:140]))
        for table in main[0].xpath(".//table[contains(@class, 'lm-table')]"):
            self.assertTrue(table.xpath("ancestor::div[contains(@class, 'lm-table-responsive')]"))
        for icon in main[0].xpath(".//i[contains(@class, 'fa ')]"):
            self.assertEqual(icon.get("aria-hidden"), "true", "%s: icon %s" % (path, icon.get("class")))

    def _paths(self):
        vo, tmpl = self.vendor_order.id, self.tmpl.id
        return [
            "/my/vendor", "/my/vendor/products", "/my/vendor/products/new",
            "/my/vendor/products/%s/edit" % self.offer.id, "/my/vendor/templates/%s/variants" % tmpl,
            "/my/vendor/orders", "/my/vendor/orders/%s" % vo,
            "/my/vendor/returns", "/my/vendor/returns/%s" % self.return_rec.id,
            "/my/vendor/disputes", "/my/vendor/disputes/%s" % self.dispute.id,
            "/my/vendor/performance", "/my/vendor/finance", "/my/vendor/finance/statement",
            "/my/vendor/delivery-zones", "/my/vendor/staff", "/my/vendor/api",
            "/my/vendor/profile", "/my/vendor/import", "/my/vendor/import/%s" % self.job.id,
            "/my/vendor/terms",
        ]

    # -- tests ----------------------------------------------------------------
    def test_every_owner_page_renders_en_and_ar(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        for lang in ("en", "ar"):
            for path in self._paths():
                self._assert_vendor_shell(self._page(path, lang), path)

    def test_staff_sees_their_pages_but_not_owner_ones(self):
        self.authenticate(self.staff_user.login, PORTAL_PASSWORD)
        for lang in ("en", "ar"):
            for path in ("/my/vendor", "/my/vendor/products", "/my/vendor/orders", "/my/vendor/orders/%s" % self.vendor_order.id):
                self._assert_vendor_shell(self._page(path, lang), path)
        for path in ("/my/vendor/finance", "/my/vendor/staff", "/my/vendor/profile"):
            self.assertEqual(self.url_open(path).status_code, 403, path)
        self.assertFalse(self._page("/my/vendor", "ar").xpath("//main//a[contains(@href, '/my/vendor/staff')]"))


    def test_dashboard_alerts_cards_and_quick_actions(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self.vendor_order.write({"ship_by_date": fields.Datetime.now() - datetime.timedelta(days=2)})
        self.vendor_order._compute_is_late()
        self.env.cr.flush()
        for lang in ("en", "ar"):
            dom = self._page("/my/vendor", lang)
            self.assertEqual(len(dom.xpath("//div[contains(@class, 'lm-stat-cards')]/a[contains(@class, 'lm-stat-card')]")), 4)
            self.assertTrue(dom.xpath("//*[@id='lm_alert_late']"))
            self.assertTrue(dom.xpath("//*[@id='lm_alert_returns']"))
            self.assertTrue(dom.xpath("//*[@id='lm_dashboard_attention']"))
            self.assertTrue(dom.xpath("//a[contains(@class, 'lm-quick-link')][contains(@href, '/my/vendor/products/new')]"))

    def test_orders_list_keeps_ship_by_and_late_and_filters(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self.vendor_order.write({"ship_by_date": fields.Datetime.now() - datetime.timedelta(days=2)})
        self.vendor_order._compute_is_late()
        self.env.cr.flush()
        dom = self._page("/my/vendor/orders?filter=cancelled", "en")
        self.assertTrue(dom.xpath("//div[contains(@class, 'lm-empty')]//a[contains(@class, 'btn')]"))
        dom = self._page("/my/vendor/orders?filter=late", "en")
        self.assertTrue(dom.xpath("//table[@id='lm_orders_table']"))
        for lang in ("en", "ar"):
            dom = self._page("/my/vendor/orders", lang)
            self.assertEqual(len(dom.xpath("//*[@id='lm_order_filters']/a")), 5)
            headers = [th.text_content().strip() for th in dom.xpath("//table[@id='lm_orders_table']//th")]
            self.assertGreaterEqual(len(headers), 9)
            self.assertTrue(dom.xpath("//table[@id='lm_orders_table']//span[contains(@class, 'lm-badge--danger')]"))

    def test_order_detail_timeline_forms_and_confirmations(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        for lang in ("en", "ar"):
            dom = self._page("/my/vendor/orders/%s" % self.vendor_order.id, lang)
            self.assertTrue(dom.xpath("//*[@id='lm_order_timeline']//ol[contains(@class, 'lm-timeline')]"))
            groups = self.vendor_order.fulfillment_group_ids
            self.assertGreaterEqual(len(dom.xpath("//div[contains(@class, 'lm-panel')]//ol[contains(@class, 'lm-timeline')]")), len(groups))
            self.assertTrue(dom.xpath("//form[@id='lm_ship_form']//input[starts-with(@name, 'qty_')]"))
            self.assertTrue(dom.xpath("//form[@id='lm_ship_form']//input[@name='tracking_ref' and @required]"))
            cancel = dom.xpath("//*[@id='lm_vendor_cancel_card']//form")
            self.assertTrue(cancel)
            self.assertIn("lm-validate", cancel[0].get("class"))
            ref = cancel[0].get("data-lm-confirm-ref")
            self.assertTrue(ref and dom.xpath("//*[@id='%s']" % ref.lstrip("#")))
            self.assertTrue(cancel[0].xpath(".//button[@data-lm-loading]"))
            self.assertTrue(dom.xpath("//*[@id='lm_order_totals']"))

    def test_refuse_action_asks_for_confirmation(self):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id, "leaders_payment_method": "cod",
            "order_line": [Command.create({"product_id": self.fbm_product.id, "product_uom_qty": 1})],
        })
        order.action_confirm()
        vendor_order = order.vendor_order_ids[:1]
        group = vendor_order.fulfillment_group_ids[:1]
        group.tracking_ref = "TRK-C2B"
        group.action_ship()
        self.env.cr.flush()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        dom = self._page("/my/vendor/orders/%s" % vendor_order.id, "en")
        form = dom.xpath("//form[contains(@action, '/refused')]")
        self.assertTrue(form)
        self.assertTrue(form[0].get("data-lm-confirm-ref"))
        self.assertTrue(form[0].xpath(".//button[@data-lm-loading]"))

    def test_return_decision_reject_confirms(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        dom = self._page("/my/vendor/returns/%s" % self.return_rec.id, "ar")
        forms = dom.xpath("//*[@id='lm_return_decision']//form[contains(@action, '/reject')]")
        self.assertTrue(forms and forms[0].get("data-lm-confirm-ref"))
        self.assertTrue(forms[0].xpath(".//textarea[@required]/following-sibling::div[contains(@class, 'invalid-feedback')]"))

    def test_dispute_thread_and_reply_form(self):
        self.dispute.message_post(body="Hello from the customer", message_type="comment")
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        dom = self._page("/my/vendor/disputes/%s" % self.dispute.id, "en")
        self.assertTrue(dom.xpath("//div[contains(@class, 'lm-thread')]//div[contains(@class, 'lm-message')]"))
        self.assertTrue(dom.xpath("//textarea[@name='message' and @required]"))
        self.assertIn("lm-badge", dom.xpath("//h1/following-sibling::span | //span[contains(@class, 'lm-badge')]")[0].get("class"))

    def test_performance_shows_late_shipment_rate(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        dom = self._page("/my/vendor/performance", "en")
        card = dom.xpath("//*[@id='lm_kpi_late_rate']")
        self.assertTrue(card)
        self.assertIn("lm-tone-danger", card[0].get("class"))
        self.assertIn("25.00%", card[0].text_content())
        self.assertTrue(dom.xpath("//span[contains(@class, 'lm-badge--warning')]"))  # the penalty level

    def test_finance_sections(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        for lang in ("en", "ar"):
            dom = self._page("/my/vendor/finance", lang)
            self.assertTrue(dom.xpath("//*[@id='lm_finance_cards']/div[contains(@class, 'lm-stat-card')]"))
            self.assertTrue(dom.xpath("//*[@id='lm_settlements_panel']//table | //*[@id='lm_settlements_panel']//div[contains(@class, 'lm-empty')]"))
            self.assertTrue(dom.xpath("//*[@id='lm_wallet_panel']"))
            self.assertTrue(dom.xpath("//a[contains(@href, '/my/vendor/finance/statement')]"))
            self.assertTrue(dom.xpath("//*[@id='lm_settlement_request']//form[contains(@class, 'lm-validate')]"))

    def test_products_page_actions_and_empty_state(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        dom = self._page("/my/vendor/products", "en")
        self.assertTrue(dom.xpath("//*[@id='lm_bulk_discount_form']"))
        clear = dom.xpath("//*[@id='lm_bulk_discount_form']//button[@value='clear']")
        self.assertTrue(clear and clear[0].get("data-lm-confirm-ref"))
        self.assertTrue(dom.xpath("//form[contains(@action, '/stock')]"))
        dom = self._page("/my/vendor/products?state=suspended", "en")
        self.assertTrue(dom.xpath("//div[contains(@class, 'lm-empty')]//a[contains(@class, 'btn')]"))

    def test_product_form_sections_and_inline_validation(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        for lang in ("en", "ar"):
            dom = self._page("/my/vendor/products/%s/edit" % self.offer.id, lang)
            form = dom.xpath("//form[@id='lm_product_form']")[0]
            self.assertIn("lm-validate", form.get("class"))
            for section in ("lm_details_card", "lm_offer_card", "lm_main_image_card", "lm_variants_card",
                            "lm_extra_images_card", "lm_specs_card"):
                self.assertTrue(form.xpath(".//*[@id='%s']" % section), section)
            self.assertTrue(dom.xpath("//*[@id='lm_discount_card']"))
            self.assertTrue(form.xpath(".//input[@name='name' and @required]/following-sibling::div[contains(@class, 'invalid-feedback')]"))

    def test_destructive_actions_of_the_other_pages_confirm(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        dom = self._page("/my/vendor/api", "en")
        revoke = dom.xpath("//form[contains(@action, '/revoke')]")
        self.assertTrue(revoke and revoke[0].get("data-lm-confirm-ref"))
        self.assertTrue(dom.xpath("//pre[contains(@class, 'lm-code')]"))
        dom = self._page("/my/vendor/staff", "en")
        deactivate = dom.xpath("//form[contains(@action, '/deactivate')]")
        self.assertTrue(deactivate and deactivate[0].get("data-lm-confirm-ref"))
        self.env["leaders.vendor.delivery.zone"].create({
            "vendor_id": self.vendor.id, "state_id": self.env["res.country.state"].search([], limit=1).id, "price": 5.0})
        self.env.cr.flush()
        dom = self._page("/my/vendor/delivery-zones", "en")
        remove = dom.xpath("//form[contains(@action, '/delete')]")
        self.assertTrue(remove and remove[0].get("data-lm-confirm-ref"))
        self.assertNotIn("platform", dom.xpath("string(//main)").lower())

    def test_import_job_shows_counts(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        dom = self._page("/my/vendor/import/%s" % self.job.id, "en")
        self.assertEqual(len(dom.xpath("//*[@id='lm_vendor_import_job']//div[contains(concat(' ', normalize-space(@class), ' '), ' lm-stat-card ')]")), 4)

    def test_terms_staff_notice_page(self):
        self.env["leaders.vendor.terms"].create({"name": "Seller Agreement", "version": 1, "body": "<p>Body</p>"})
        self.env.cr.flush()
        self.authenticate(self.staff_user.login, PORTAL_PASSWORD)
        for lang in ("en", "ar"):
            dom = self._page("/my/vendor", lang, status=403)
            self.assertTrue(dom.xpath("//*[@id='lm_vendor_terms_notice']//h1[contains(@class, 'lm-page-title')]"))
            self.assertTrue(dom.xpath("//*[@id='lm_vendor_terms_notice']//div[contains(@class, 'alert-warning')]"))

    def test_public_application_pages(self):
        customer = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Shopper", "login": "lm.shopper@example.com", "password": PORTAL_PASSWORD,
            "groups_id": [Command.set([self.env.ref("base.group_portal").id])]})
        self.authenticate(customer.login, PORTAL_PASSWORD)  # the form needs a signed-in customer
        for lang in ("en", "ar"):
            dom = self._page("/vendor/apply", lang)
            self.assertTrue(dom.xpath("//*[@id='lm_apply_page']//h1[contains(@class, 'lm-af-title')]"))
            self.assertEqual(len(dom.xpath("//form[@id='lm_apply_form']//section[contains(@class, 'lm-af-step')]")), 3)
            self.assertTrue(dom.xpath("//form[@id='lm_apply_form']//button[@data-lm-loading]"))
            dom = self._page("/vendor/apply/status/%s" % self.application.access_token, lang)
            self.assertTrue(dom.xpath("//*[@id='lm_apply_status_page']//span[contains(@class, 'lm-badge')]"))
            self.assertTrue(dom.xpath("//div[contains(@class, 'lm-table-responsive')]//td[@data-label]"))

    def test_new_penalty_levels_have_tones(self):
        from odoo.addons.leaders_marketplace.models.portal_nav import STATE_TONES
        for level in ("warning", "freeze", "ban"):
            self.assertIn(level, STATE_TONES)
        dom = lxml_html.fromstring("<div>%s</div>" % self.env["ir.qweb"]._render("leaders_marketplace.lm_state_badge", {
            "lm_record": self.env["leaders.vendor.penalty"].new({"level": "ban"}), "lm_field": "level"}))
        self.assertTrue(dom.xpath("//span[contains(@class, 'lm-badge--danger')]"))
