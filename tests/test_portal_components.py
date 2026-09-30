"""Portal design system + redesigned customer pages (TASK 29 / C2a)."""
import re

from lxml import html as lxml_html

from odoo import Command
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import HttpCase
from odoo.tools import file_path

from odoo.addons.leaders_marketplace.models.portal_nav import STATE_TONES, TIMELINE_KEYS

from .test_customer_cancel import CustomerCancelCommon
from .test_security_rpc import PORTAL_PASSWORD

TONES = {"success", "warning", "info", "primary", "danger", "dark", "secondary"}
STATE_FIELD = re.compile(r"^(state|status|.+_state|.+_status)$")
# Fields of other apps' mixins that share the naming pattern.
NOT_A_STATE = {"activity_state"}
EXTRA_FIELDS = (("product.template", "marketplace_state"), ("res.partner", "vendor_state"),
                ("res.partner", "vendor_kyc_state"))


def module_state_fields(env):
    """(model, field) of every state-like selection of the leaders.* models."""
    found = []
    for model_name in sorted(env.registry.models):
        if not model_name.startswith("leaders."):
            continue
        for name, field in env[model_name]._fields.items():
            if field.type == "selection" and STATE_FIELD.match(name) and name not in NOT_A_STATE:
                found.append((model_name, name))
    found.extend(EXTRA_FIELDS)
    return found


@tagged("post_install", "-at_install")
class TestPortalComponents(TransactionCase):

    def _render(self, template, values):
        return lxml_html.fromstring(
            "<div>%s</div>" % self.env["ir.qweb"]._render(template, values))

    def test_every_state_of_every_model_has_a_color(self):
        pairs = module_state_fields(self.env)
        self.assertGreater(len(pairs), 20)
        self.assertIn(("leaders.vendor.order", "state"), pairs)
        self.assertIn(("leaders.fulfillment.group", "state"), pairs)
        self.assertIn(("leaders.vendor.return", "state"), pairs)
        self.assertIn(("leaders.vendor.dispute", "state"), pairs)
        self.assertIn(("leaders.settlement", "state"), pairs)
        self.assertIn(("leaders.marketplace.offer", "state"), pairs)
        self.assertIn(("leaders.vendor.application", "state"), pairs)
        self.assertIn(("leaders.marketplace.offer", "discount_state"), pairs)
        self.assertIn(("leaders.vendor.import", "state"), pairs)
        missing = []
        for model_name, field_name in pairs:
            for value, _label in self.env[model_name]._fields[field_name]._description_selection(self.env):
                if STATE_TONES.get(value) not in TONES:
                    missing.append("%s.%s=%s" % (model_name, field_name, value))
        self.assertFalse(missing, "states without a colour: %s" % missing)
        self.assertEqual(STATE_TONES["late"], "danger")

    def test_badge_template_renders_tone_and_label_for_every_state(self):
        for model_name, field_name in module_state_fields(self.env):
            field = self.env[model_name]._fields[field_name]
            for value, label in field._description_selection(self.env):
                record = self.env[model_name].new({field_name: value})
                dom = self._render("leaders_marketplace.lm_state_badge", {
                    "lm_record": record, "lm_field": field_name})
                badge = dom.xpath("//span[contains(@class, 'lm-badge')]")
                self.assertEqual(len(badge), 1, (model_name, field_name, value))
                self.assertIn("lm-badge--%s" % STATE_TONES[value], badge[0].get("class"))
                self.assertEqual(badge[0].text_content().strip(), label)

    def test_legacy_badge_templates_delegate(self):
        order = self.env["leaders.vendor.order"].new({"state": "cancelled"})
        dom = self._render("leaders_marketplace.portal_vendor_state_badge", {"record": order})
        self.assertTrue(dom.xpath("//span[contains(@class, 'lm-badge--danger')]"))
        ret = self.env["leaders.vendor.return"].new({"state": "refunded"})
        dom = self._render("leaders_marketplace.portal_return_state_badge", {"ret": ret})
        self.assertTrue(dom.xpath("//span[contains(@class, 'lm-badge--success')]"))

    def test_empty_state_template(self):
        dom = self._render("leaders_marketplace.lm_empty_state", {
            "lm_icon": "fa-inbox", "lm_message": "Nothing here",
            "lm_action_url": "/shop", "lm_action_label": "Go"})
        self.assertTrue(dom.xpath("//div[contains(@class, 'lm-empty')]//a[@href='/shop']"))
        self.assertIn("Nothing here", dom.text_content())
        dom = self._render("leaders_marketplace.lm_empty_state", {"lm_message": "Just text"})
        self.assertFalse(dom.xpath("//a"))

    def test_timeline_steps_for_orders_and_groups(self):
        nav = self.env["leaders.portal.nav"]
        order = self.env["leaders.vendor.order"].new({"state": "pending"})
        line = nav._timeline(order)
        self.assertEqual([s["key"] for s in line["steps"]], list(TIMELINE_KEYS))
        self.assertEqual([s["status"] for s in line["steps"]], ["current", "todo", "todo", "todo"])
        self.assertFalse(line["terminal"])
        order.state = "fulfilled"
        self.assertEqual([s["status"] for s in nav._timeline(order)["steps"]],
                         ["done", "done", "current", "todo"])
        order.state = "completed"
        self.assertEqual([s["status"] for s in nav._timeline(order)["steps"]],
                         ["done", "done", "done", "done"])
        order.state = "cancelled"
        line = nav._timeline(order)
        self.assertEqual(line["terminal"]["key"], "cancelled")
        self.assertEqual([s["status"] for s in line["steps"]], ["done", "todo", "todo", "todo"])
        group = self.env["leaders.fulfillment.group"].new({"state": "delivery_reported"})
        self.assertEqual([s["status"] for s in nav._timeline(group)["steps"]],
                         ["done", "done", "current", "todo"])
        group.state = "delivered"
        self.assertEqual(nav._timeline(group)["steps"][3]["status"], "done")
        group = self.env["leaders.fulfillment.group"].new({"state": "shipped", "shipping_status": "returned"})
        line = nav._timeline(group)
        self.assertEqual(line["terminal"]["key"], "returned")
        self.assertEqual([s["status"] for s in line["steps"]], ["done", "done", "done", "done"])

    def test_timeline_template_markup(self):
        order = self.env["leaders.vendor.order"].new({"state": "cancelled"})
        dom = self._render("leaders_marketplace.lm_timeline", {"lm_record": order})
        self.assertEqual(len(dom.xpath("//ol[contains(@class, 'lm-timeline')]/li")), 5)
        self.assertTrue(dom.xpath("//li[@data-step='cancelled' and contains(@class, 'is-terminal')]"))

    def test_assets_are_registered_and_present(self):
        import ast
        with open(file_path("leaders_marketplace/__manifest__.py"), encoding="utf-8") as handle:
            manifest = ast.literal_eval(handle.read())
        bundle = manifest["assets"]["web.assets_frontend"]
        for path in ("scss/portal/_tokens.scss", "scss/portal/_components.scss", "scss/portal.scss",
                     "js/portal_actions.js"):
            full = "leaders_marketplace/static/src/" + path
            self.assertIn(full, bundle)
            self.assertTrue(file_path(full))
        # tokens come before the components that use them
        self.assertLess(bundle.index("leaders_marketplace/static/src/scss/portal/_tokens.scss"),
                        bundle.index("leaders_marketplace/static/src/scss/portal/_components.scss"))
        with open(file_path("leaders_marketplace/static/src/js/portal_actions.js"), encoding="utf-8") as handle:
            script = handle.read()
        for needle in ("data-lm-confirm", "data-lm-loading", "lm-validate"):
            self.assertIn(needle, script)

    def test_frontend_bundle_compiles_with_the_design_system(self):
        bundle = self.env["ir.qweb"]._get_asset_bundle("web.assets_frontend", css=True, js=False)
        content = bundle.css().raw.decode()  # raises when the SCSS does not compile
        for needle in (".lm-page-header", ".lm-stat-cards", ".lm-badge--danger", ".lm-table-responsive",
                       ".lm-timeline", ".lm-empty", ".lm-form-field", "--lm-radius"):
            self.assertIn(needle, content)


@tagged("post_install", "-at_install")
class TestCustomerPortalPages(HttpCase, CustomerCancelCommon):
    """Every redesigned customer page renders in English and Arabic with its components."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        website = cls.env["website"].search([], limit=1)
        lang = cls.env["res.lang"]._activate_lang("ar_001")
        website.language_ids = [Command.link(lang.id)]
        cls.ar_prefix = "/" + lang.url_code
        cls.stranger_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Empty Eve", "login": "lm.empty.eve@example.com", "password": PORTAL_PASSWORD,
            "partner_id": cls.env["res.partner"].create({"name": "Empty Eve"}).id,
            "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
        })

    def _page(self, path, lang):
        prefix = self.ar_prefix if lang == "ar" else ""
        response = self.url_open(prefix + path)
        self.assertEqual(response.status_code, 200, prefix + path)
        self.assertGreater(len(response.content), 500, (response.url, response.text[:200]))
        dom = lxml_html.fromstring(response.content)
        rtl = dom.xpath("//div[@id='wrapwrap' and contains(@class, 'o_rtl')]")
        self.assertEqual(bool(rtl), lang == "ar", "wrong text direction on %s (%s)" % (path, lang))
        return dom

    def _confirmed(self):
        order = self._sale([(self.fbm_product, 1), (self.b_product, 1)])
        order.action_confirm()
        return order

    def _assert_cells_are_labelled(self, dom):
        tables = dom.xpath("//div[contains(@class, 'lm-table-responsive')]//table[contains(@class, 'lm-table')]")
        self.assertTrue(tables)
        for cell in dom.xpath("//table[contains(@class, 'lm-table')]//tbody/tr/td[normalize-space(.) != '']"):
            self.assertTrue(cell.get("data-label"), lxml_html.tostring(cell)[:120])

    def test_orders_list_en_and_ar(self):
        order = self._confirmed()
        vendor_order = order.vendor_order_ids[:1]
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        for lang in ("en", "ar"):
            dom = self._page("/my/marketplace/orders", lang)
            self.assertTrue(dom.xpath("//div[contains(@class, 'lm-page-header')]//h1"))
            self.assertTrue(dom.xpath("//div[contains(@class, 'lm-stat-cards')]"))
            self.assertTrue(dom.xpath("//span[contains(@class, 'lm-badge')]"))
            self.assertTrue(dom.xpath("//a[contains(@href, '/my/marketplace/orders/%s')]" % vendor_order.id))
            self._assert_cells_are_labelled(dom)
            self.assertFalse(dom.xpath("//div[contains(@class, 'lm-empty')]"))

    def test_orders_list_empty_state_has_a_next_action(self):
        self.authenticate(self.stranger_user.login, PORTAL_PASSWORD)
        for lang in ("en", "ar"):
            dom = self._page("/my/marketplace/orders", lang)
            self.assertTrue(dom.xpath("//div[contains(@class, 'lm-empty')]//a[contains(@class, 'btn')]"))

    def test_order_detail_en_and_ar(self):
        order = self._confirmed()
        vendor_order = order.vendor_order_ids[:1]
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        for lang in ("en", "ar"):
            dom = self._page("/my/marketplace/orders/%s" % vendor_order.id, lang)
            self.assertTrue(dom.xpath("//ol[contains(@class, 'lm-timeline')]"))
            self.assertTrue(dom.xpath("//h1[contains(@class, 'lm-page-title')]"))
            self.assertTrue(dom.xpath("//span[contains(@class, 'lm-badge')]"))
            self.assertTrue(dom.xpath("//*[@id='lm_order_items']"))
            self.assertTrue(dom.xpath("//*[@id='lm_order_totals']"))
            self._assert_cells_are_labelled(dom)
            # destructive forms confirm, show a spinner on submit and validate inline
            form = dom.xpath("//form[@id='lm_cancel_all_form']")[0]
            self.assertIn("lm-validate", form.get("class"))
            self.assertTrue(form.get("data-lm-confirm-ref"))
            self.assertTrue(dom.xpath(".//*[@id='%s']" % form.get("data-lm-confirm-ref").lstrip("#")))
            self.assertTrue(form.xpath(".//button[@data-lm-loading]"))
            self.assertTrue(form.xpath(".//select[@required]/following-sibling::div[contains(@class, 'invalid-feedback')]"))
            self.assertTrue(dom.xpath("//form[@id='lm_cancel_part_form']"))
            self.assertTrue(dom.xpath("//*[@id='lm_report_problem']//textarea[@required]"))
            for icon in dom.xpath("//*[@id='lm_customer_order']//i[contains(@class, 'fa')]"):
                self.assertEqual(icon.get("aria-hidden"), "true")

    def test_order_detail_states_cancelled_shipped_and_returns(self):
        order = self._confirmed()
        first = order.vendor_order_ids[0]
        self._ship(first)
        first.invalidate_recordset()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._page("/my/marketplace/orders/%s" % first.id, "en")
        self.assertTrue(dom.xpath("//li[@data-step='shipped']"))
        self.assertTrue(dom.xpath("//*[@id='lm_cancel_shipped']"))
        cancelled = order.vendor_order_ids[1]
        cancelled.action_cancel()
        dom = self._page("/my/marketplace/orders/%s" % cancelled.id, "ar")
        self.assertTrue(dom.xpath("//li[@data-step='cancelled']"))
        self.assertTrue(dom.xpath("//span[contains(@class, 'lm-badge--danger')]"))

    def test_returns_and_disputes_page_en_and_ar(self):
        order = self._confirmed()
        vendor_order = order.vendor_order_ids[:1]
        self._ship(vendor_order)
        self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": vendor_order.line_ids[:1].id, "quantity": 1})],
        })
        self.env["leaders.vendor.dispute"].create({"vendor_order_id": vendor_order.id, "reason": "Late"})
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        for lang in ("en", "ar"):
            dom = self._page("/my/marketplace/returns", lang)
            self.assertTrue(dom.xpath("//*[@id='lm_customer_returns']//div[contains(@class, 'lm-stat-cards')]"))
            self.assertEqual(len(dom.xpath("//table[contains(@class, 'lm-table')]")), 2)
            self.assertTrue(dom.xpath("//span[contains(@class, 'lm-badge--warning')]"))
            self._assert_cells_are_labelled(dom)
        self.authenticate(self.stranger_user.login, PORTAL_PASSWORD)
        dom = self._page("/my/marketplace/returns", "en")
        self.assertEqual(len(dom.xpath("//div[contains(@class, 'lm-empty')]")), 2)

    def test_seller_store_page_en_and_ar(self):
        self.vendor.vendor_store_name = "Redesign Store"
        self.vendor._marketplace_ensure_store_slug()
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.catalog_offers_enabled", "True")
        path = "/seller/%s" % self.vendor.vendor_store_slug
        self.authenticate(None, None)
        for lang in ("en", "ar"):
            dom = self._page(path, lang)
            self.assertTrue(dom.xpath("//*[@id='lm_seller_store']//h1[contains(@class, 'lm-page-title')]"))
            self.assertTrue(dom.xpath("//*[contains(@class, 'lm-empty')] | //a[contains(@class, 'lm-store-card')]"))
