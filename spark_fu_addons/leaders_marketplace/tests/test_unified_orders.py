"""Unified "My orders" (TASK 29 / D1): overall status machine, list, detail, buy again."""
from lxml import html as lxml_html

from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.leaders_marketplace.models.unified_orders import (
    CHECKOUT_STATUSES, aggregate_status, status_labels)
from .test_customer_cancel import CustomerCancelCommon
from .test_security_rpc import PORTAL_PASSWORD

PARAM = "leaders_marketplace.unified_customer_orders"


class UnifiedOrdersCommon(CustomerCancelCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param(PARAM, "True")

    def _confirmed(self, lines, cod=False):
        order = self._sale(lines, cod=cod)
        order.action_confirm()
        return order

    def _deliver_group(self, vendor_order):
        for group in vendor_order.fulfillment_group_ids:
            group.tracking_ref = "D1-%s" % group.id
            group.action_ship()
            group.action_delivered()

    def _deliver_company(self, sale):
        for picking in sale.picking_ids:
            picking.action_assign()
            for move in picking.move_ids:
                move.quantity = move.product_uom_qty
                move.picked = True
            picking.with_context(skip_backorder=True)._action_done()

    def _status(self, sale):
        return sale._leaders_overall_status()[0]


@tagged("post_install", "-at_install")
class TestOverallStatus(UnifiedOrdersCommon):

    def test_aggregate_state_machine_table(self):
        table = [
            ([], "placed"),
            (["placed"], "placed"),
            (["confirmed"], "confirmed"),
            (["placed", "confirmed"], "confirmed"),
            (["processing"], "processing"),
            (["confirmed", "processing"], "processing"),
            (["partially_shipped"], "partially_shipped"),
            (["shipped"], "shipped"),
            (["shipped", "shipped"], "shipped"),
            (["shipped", "delivered"], "shipped"),
            (["shipped", "processing"], "partially_shipped"),
            (["delivered", "processing"], "partially_shipped"),
            (["delivered", "confirmed"], "partially_shipped"),
            (["delivered"], "delivered"),
            (["delivered", "delivered"], "delivered"),
            (["delivered", "returned"], "delivered"),
            (["returned"], "returned"),
            (["returned", "returned"], "returned"),
            (["cancelled"], "cancelled"),
            (["cancelled", "cancelled"], "cancelled"),
            (["cancelled", "processing"], "partially_cancelled"),
            (["cancelled", "delivered"], "partially_cancelled"),
            (["cancelled", "returned"], "partially_cancelled"),
            (["cancelled", "shipped", "confirmed"], "partially_cancelled"),
        ]
        for codes, expected in table:
            self.assertEqual(aggregate_status(codes)[0], expected, codes)
        # The progress of the living parts survives a partial cancel.
        self.assertEqual(aggregate_status(["cancelled", "shipped"]), ("partially_cancelled", "shipped"))
        self.assertEqual(aggregate_status(["cancelled"]), ("cancelled", "cancelled"))
        # Every code has a label and a badge tone.
        from odoo.addons.leaders_marketplace.models.portal_nav import STATE_TONES
        labels = status_labels()
        for code in CHECKOUT_STATUSES:
            self.assertTrue(labels[code])
            self.assertIn(code, STATE_TONES)

    def test_company_only_checkout(self):
        sale = self._confirmed([(self.own_product, 1)])
        self.assertFalse(sale.vendor_order_ids)
        self.assertEqual(sale._leaders_overall_status(), ("processing", "Processing"))
        self._deliver_company(sale)
        self.assertEqual(self._status(sale), "delivered")
        self.assertEqual(sale._leaders_checkout_amount(), sale.amount_total)

    def test_vendor_only_checkout_walks_through_the_states(self):
        sale = self._confirmed([(self.fbm_product, 2)])
        self.assertTrue(sale.marketplace_vendor_only)
        self.assertEqual(sale.state, "cancel")
        self.assertEqual(self._status(sale), "processing")
        vorder = sale.vendor_order_ids
        group = vorder.fulfillment_group_ids[:1]
        group.tracking_ref = "D1-A"
        group.action_ship()
        self.assertEqual(self._status(sale), "shipped")
        group.action_delivered()
        self.assertEqual(self._status(sale), "delivered")
        self.assertEqual(sale._leaders_checkout_amount(), 200.0)

    def test_mixed_checkout_uses_every_part(self):
        sale = self._confirmed([(self.own_product, 1), (self.fbm_product, 1), (self.b_product, 1)])
        self.assertEqual(sale.state, "sale")
        self.assertEqual(len(sale.vendor_order_ids), 2)
        self.assertEqual(self._status(sale), "processing")
        va, vb = sale.vendor_order_ids.sorted("id")
        self._deliver_group(va)
        # one seller delivered, the others still processing
        self.assertEqual(self._status(sale), "partially_shipped")
        self._deliver_group(vb)
        self.assertEqual(self._status(sale), "partially_shipped")   # company part not shipped yet
        self._deliver_company(sale)
        self.assertEqual(self._status(sale), "delivered")

    def test_cancelled_and_partially_cancelled(self):
        sale = self._confirmed([(self.own_product, 1), (self.fbm_product, 1), (self.b_product, 1)])
        va, vb = sale.vendor_order_ids.sorted("id")
        vb.sudo().action_cancel()
        self.assertEqual(self._status(sale), "partially_cancelled")
        self.assertEqual(sale._leaders_status_info()["progress"], "processing")
        va.sudo().action_cancel()
        sale.sudo()._action_cancel()
        self.assertEqual(self._status(sale), "cancelled")

    def test_customer_cancel_all_is_cancelled(self):
        sale = self._confirmed([(self.own_product, 1), (self.fbm_product, 1)])
        sale.vendor_order_ids.action_customer_cancel(self.reason.id, scope="all")
        self.assertEqual(self._status(sale), "cancelled")
        self.assertTrue(sale._leaders_is_checkout_of(self.customer))

    def test_returned_when_every_line_came_back(self):
        sale = self._confirmed([(self.fbm_product, 1)])
        vorder = sale.vendor_order_ids
        self._deliver_group(vorder)
        ret = self.env["leaders.vendor.return"].create({
            "vendor_order_id": vorder.id, "reason": "Broken",
            "line_ids": [Command.create({"vendor_line_id": vorder.line_ids[:1].id, "quantity": 1})],
        })
        self.assertEqual(self._status(sale), "delivered")
        ret.sudo().write({"state": "received"})
        vorder.line_ids.invalidate_recordset()
        self.assertEqual(self._status(sale), "returned")

    def test_checkout_scope_excludes_drafts_and_other_customers(self):
        draft = self._sale([(self.own_product, 1)])
        sale = self._confirmed([(self.own_product, 1)])
        found = self.env["sale.order"].search(self.env["sale.order"]._leaders_checkout_domain(self.customer))
        self.assertIn(sale, found)
        self.assertNotIn(draft, found)
        other = self.env["res.partner"].create({"name": "Somebody Else"})
        self.assertFalse(self.env["sale.order"].search(self.env["sale.order"]._leaders_checkout_domain(other)))


@tagged("post_install", "-at_install")
class TestUnifiedOrdersPortal(HttpCase, UnifiedOrdersCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for product in (cls.own_product, cls.fbm_product, cls.fbl_product, cls.b_product):
            product.product_tmpl_id.write({"is_published": True, "sale_ok": True})
        cls.other_customer = cls.env["res.partner"].create({"name": "Other Buyer", "email": "lm.other.buyer@example.com"})
        cls.other_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Other Buyer", "login": "lm.other.buyer@example.com", "email": "lm.other.buyer@example.com",
            "password": PORTAL_PASSWORD, "partner_id": cls.other_customer.id,
            "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
        })

    # -- helpers -----------------------------------------------------------
    def _set(self, value):
        self.env["ir.config_parameter"].sudo().set_param(PARAM, value)
        self.env.cr.flush()

    def _dom(self, path, code=200):
        response = self.url_open(path)
        self.assertEqual(response.status_code, code, path)
        return lxml_html.fromstring(response.content) if code == 200 else None

    def _rows(self, dom):
        return dom.xpath("//table[contains(@class, 'lm-table')]//tbody/tr/@data-checkout")

    def _card_href(self, dom):
        return dom.xpath("string(//div[contains(@class, 'o_portal_index_card')]//a[@title='My orders']/@href)")

    def _crumbs(self, dom):
        return " ".join(dom.xpath("string(//ol[contains(@class, 'o_portal_submenu')])").split())

    # -- switch off keeps today's behaviour ------------------------------------
    def test_switch_off_404_and_card_unchanged(self):
        sale = self._confirmed([(self.own_product, 1), (self.fbm_product, 1)])
        vorder = sale.vendor_order_ids
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        self._set("True")
        self.assertEqual(self._card_href(self._dom("/my/home")), "/my/marketplace/checkouts")
        self._set(False)
        self.assertEqual(self._card_href(self._dom("/my/home")), "/my/marketplace/orders")
        self._dom("/my/marketplace/checkouts", 404)
        self._dom("/my/marketplace/checkouts/%s" % sale.id, 404)
        token = self._csrf("/my/marketplace/orders/%s" % vorder.id)
        response = self.url_open("/my/marketplace/checkouts/%s/buy-again" % sale.id, data={"csrf_token": token})
        self.assertEqual(response.status_code, 404)
        # the old pages still work, breadcrumbs point to the old list
        dom = self._dom("/my/marketplace/orders/%s" % vorder.id)
        self.assertTrue(dom.xpath("//ol[contains(@class, 'o_portal_submenu')]//a[@href='/my/marketplace/orders']"))
        self._dom("/my/marketplace/orders")

    # -- list ---------------------------------------------------------------
    def test_list_has_one_row_per_checkout(self):
        company = self._confirmed([(self.own_product, 1)])
        vendor_only = self._confirmed([(self.fbm_product, 1), (self.b_product, 1)])   # 2 vendor orders, ONE row
        mixed = self._confirmed([(self.own_product, 1), (self.fbm_product, 1)])
        cod = self._confirmed([(self.fbm_product, 1)], cod=True)
        draft = self._sale([(self.own_product, 1)])
        other = self.env["sale.order"].create({
            "partner_id": self.other_customer.id,
            "order_line": [Command.create({"product_id": self.own_product.id, "product_uom_qty": 1})]})
        other.action_confirm()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/marketplace/checkouts")
        rows = self._rows(dom)
        self.assertCountEqual(rows, [str(s.id) for s in (company, vendor_only, mixed, cod)])
        self.assertNotIn(str(draft.id), rows)
        self.assertNotIn(str(other.id), rows)
        text = self.url_open("/my/marketplace/checkouts").text
        self.assertIn(vendor_only.name, text)
        self.assertIn("Cash on Delivery", text)
        self.assertIn("Online Payment", text)
        # the total of a split checkout is the checkout total (both sellers)
        row = dom.xpath("//tr[@data-checkout='%s']" % vendor_only.id)[0]
        self.assertIn("180.00", row.text_content())
        for cell in row.xpath("./td"):
            self.assertTrue(cell.get("data-label"))
        self.assertIn(self.vendor.name, row.text_content())
        # breadcrumbs
        self.assertIn("My orders", self._crumbs(dom))

    def test_filters_search_sort(self):
        company = self._confirmed([(self.own_product, 1)])
        vendor_only = self._confirmed([(self.fbm_product, 1)])
        cancelled = self._confirmed([(self.b_product, 1)])
        cancelled.vendor_order_ids.action_customer_cancel(self.reason.id, scope="all")
        old = self._confirmed([(self.own_product, 1)])
        old.date_order = "2020-03-15 10:00:00"
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        ids = lambda **kw: self._rows(self._dom("/my/marketplace/checkouts" + (
            "?" + "&".join("%s=%s" % i for i in kw.items()) if kw else "")))
        self.assertEqual(len(ids()), 4)
        self.assertEqual(ids(status="cancelled"), [str(cancelled.id)])
        self.assertCountEqual(ids(status="processing"), [str(company.id), str(vendor_only.id), str(old.id)])
        self.assertEqual(ids(status="delivered"), [])
        self.assertEqual(ids(status="bogus").__len__(), 4)            # unknown status = no filter
        self.assertEqual(ids(search=vendor_only.name), [str(vendor_only.id)])
        self.assertIn(str(vendor_only.id), ids(search="Split Vendor"))   # by seller
        self.assertEqual(ids(search="Cancel B Product"), [str(cancelled.id)])   # by product
        self.assertEqual(ids(search="Company Product").__len__(), 2)
        self.assertEqual(ids(search="zzz-nothing"), [])
        self.assertEqual(ids(date_end="2020-12-31"), [str(old.id)])
        self.assertEqual(ids(date_begin="2021-01-01").__len__(), 3)
        self.assertEqual(ids(date_begin="2020-03-15", date_end="2020-03-15"), [str(old.id)])
        self.assertEqual(ids(date_begin="not-a-date").__len__(), 4)
        # sort: date desc (default), asc, total
        self.assertEqual(ids()[-1], str(old.id))
        self.assertEqual(ids(sortby="date_asc")[0], str(old.id))
        totals = ids(sortby="total")
        self.assertEqual(totals[0], str(vendor_only.id))              # 100.00 is the biggest
        # empty result has an empty state with a way back
        dom = self._dom("/my/marketplace/checkouts?search=zzz-nothing")
        self.assertTrue(dom.xpath("//div[contains(@class, 'lm-empty')]//a[@href='/my/marketplace/checkouts']"))

    def test_pager(self):
        for _i in range(11):
            self._confirmed([(self.own_product, 1)])
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        first = self._rows(self._dom("/my/marketplace/checkouts"))
        self.assertEqual(len(first), 10)
        dom2 = self._dom("/my/marketplace/checkouts/page/2")
        second = self._rows(dom2)
        self.assertEqual(len(second), 1)
        self.assertFalse(set(first) & set(second))
        # filters survive in the pager links
        dom = self._dom("/my/marketplace/checkouts?search=Company")
        self.assertTrue(dom.xpath("//ul[contains(@class, 'pagination')]//a[contains(@href, 'search=Company')]"))

    def test_empty_list_has_a_next_action(self):
        self.authenticate(self.other_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/marketplace/checkouts")
        self.assertTrue(dom.xpath("//div[contains(@class, 'lm-empty')]//a[@href='/shop']"))

    # -- detail ---------------------------------------------------------------
    def test_detail_ownership(self):
        sale = self._confirmed([(self.own_product, 1), (self.fbm_product, 1)])
        self.authenticate(self.other_user.login, PORTAL_PASSWORD)
        self._dom("/my/marketplace/checkouts/%s" % sale.id, 403)
        self._dom("/my/marketplace/checkouts/999999", 404)
        token = self._csrf("/my/account")
        response = self.url_open("/my/marketplace/checkouts/%s/buy-again" % sale.id, data={"csrf_token": token})
        self.assertEqual(response.status_code, 403)
        # a draft quotation of the customer is not a checkout
        draft = self._sale([(self.own_product, 1)])
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        self._dom("/my/marketplace/checkouts/%s" % draft.id, 404)
        self._dom("/my/marketplace/checkouts/%s" % sale.id)

    def test_detail_sections_and_action_chips(self):
        sale = self._confirmed([(self.own_product, 1), (self.fbm_product, 1), (self.b_product, 2)])
        va, vb = sale.vendor_order_ids.sorted("id")
        va.sudo().fulfillment_group_ids[:1].tracking_ref = "TRK-A"
        va.sudo().fulfillment_group_ids[:1].tracking_url = "https://track.example/TRK-A"
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/marketplace/checkouts/%s" % sale.id)
        self.assertTrue(dom.xpath("//div[@id='lm_checkout_timeline']//ol[contains(@class, 'lm-timeline')]"))
        self.assertTrue(dom.xpath("//h1/../span[contains(@class, 'lm-badge')]"))
        self.assertTrue(dom.xpath("//div[@id='lm_checkout_company']"))
        for vorder in (va, vb):
            self.assertTrue(dom.xpath("//div[@id='lm_checkout_vendor_%s']" % vorder.id))
            self.assertTrue(dom.xpath("//div[@id='lm_checkout_vendor_%s']//ol[contains(@class, 'lm-timeline')]" % vorder.id))
        self.assertTrue(dom.xpath("//a[@href='https://track.example/TRK-A']"))
        self.assertIn("Company Product", dom.xpath("string(//div[@id='lm_checkout_company'])"))
        # chips come from the same predicates as the order page (cancel on, nothing shipped)
        chips = dom.xpath("//div[@id='lm_checkout_chips_%s']//a/@data-action" % va.id)
        self.assertIn("cancel", chips)
        self.assertIn("report", chips)
        self.assertNotIn("return", chips)
        self.assertTrue(dom.xpath("//div[@id='lm_checkout_actions']//a[@data-action='cancel_all']"))
        self.assertTrue(dom.xpath("//div[@id='lm_checkout_chips_%s']//a[@href='/my/marketplace/orders/%s']" % (va.id, va.id)))
        self.assertTrue(dom.xpath("//*[@id='lm_checkout_totals']"))
        self.assertIn(str(int(sale._marketplace_checkout_total())), dom.xpath("string(//*[@id='lm_checkout_totals'])"))
        # ship + deliver seller A: cancel chips disappear, return appears
        self._deliver_group(va)
        self.env.cr.flush()
        dom = self._dom("/my/marketplace/checkouts/%s" % sale.id)
        chips = dom.xpath("//div[@id='lm_checkout_chips_%s']//a/@data-action" % va.id)
        self.assertNotIn("cancel", chips)
        self.assertIn("return", chips)
        self.assertFalse(dom.xpath("//div[@id='lm_checkout_actions']//a[@data-action='cancel_all']"))
        # switching cancellation off removes the chips
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.customer_can_cancel_orders", False)
        dom = self._dom("/my/marketplace/checkouts/%s" % sale.id)
        self.assertNotIn("cancel", dom.xpath("//div[@id='lm_checkout_chips_%s']//a/@data-action" % vb.id))
        self.assertIn("My orders", self._crumbs(dom))
        self.assertIn(sale.name, self._crumbs(dom))
        self.assertTrue(dom.xpath("//ol[contains(@class, 'o_portal_submenu')]//a[@href='/my/marketplace/checkouts']"))

    def test_detail_shows_seller_invoice_pdf_link(self):
        sale = self._confirmed([(self.fbm_product, 1)])
        vorder = sale.vendor_order_ids
        self.assertEqual(vorder.invoice_id.state, "posted")
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/marketplace/checkouts/%s" % sale.id)
        href = "/my/marketplace/vendor-invoices/%s" % vorder.invoice_id.id
        self.assertTrue(dom.xpath("//a[@href='%s']" % href))
        self.assertEqual(self.url_open(href).status_code, 200)

    def test_customer_orders_card_and_breadcrumb_on_vendor_order_page(self):
        sale = self._confirmed([(self.fbm_product, 1)])
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/marketplace/orders/%s" % sale.vendor_order_ids.id)
        self.assertTrue(dom.xpath("//ol[contains(@class, 'o_portal_submenu')]//a[@href='/my/marketplace/checkouts']"))

    def test_pages_render_in_english_and_arabic(self):
        website = self.env["website"].search([], limit=1)
        lang = self.env["res.lang"]._activate_lang("ar_001")
        website.language_ids = [Command.link(lang.id)]
        prefix = "/" + lang.url_code
        sale = self._confirmed([(self.own_product, 1), (self.fbm_product, 1)])
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        for pre in ("", prefix):
            for path in ("/my/marketplace/checkouts", "/my/marketplace/checkouts/%s" % sale.id):
                response = self.url_open(pre + path)
                self.assertEqual(response.status_code, 200, pre + path)
                dom = lxml_html.fromstring(response.content)
                rtl = bool(dom.xpath("//div[@id='wrapwrap' and contains(@class, 'o_rtl')]"))
                self.assertEqual(rtl, pre == prefix, pre + path)
                self.assertTrue(dom.xpath("//span[contains(@class, 'lm-badge')]"))

    # -- buy again --------------------------------------------------------------
    def _cart(self):
        return self.env["sale.order"].sudo().search([
            ("partner_id", "=", self.customer.id), ("state", "=", "draft"),
            ("website_id", "!=", False)], order="id desc", limit=1)

    def test_buy_again_uses_current_offers_and_skips_unavailable(self):
        sale = self._confirmed([(self.own_product, 1), (self.fbm_product, 2), (self.fbl_product, 1), (self.b_product, 1)])
        # the world moved on: new price, one seller gone, one product unpublished
        self.fbm_offer.sudo().write({"price": 120.0})
        self.b_offer.sudo().write({"active": False})
        self.fbl_product.product_tmpl_id.is_published = False
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        before = self.env["sale.order"].sudo().search_count([("state", "=", "draft")])
        token = self._csrf("/my/marketplace/checkouts/%s" % sale.id)
        response = self.url_open("/my/marketplace/checkouts/%s/buy-again" % sale.id, data={"csrf_token": token})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.url.endswith("/shop/cart"), response.url)
        cart = self._cart()
        self.assertTrue(cart, "no cart was created")
        lines = cart.order_line.filtered(lambda l: not l.is_delivery)
        by_product = {l.product_id: l for l in lines}
        self.assertEqual(by_product[self.own_product].product_uom_qty, 1)
        fbm_line = by_product[self.fbm_product]
        self.assertEqual(fbm_line.product_uom_qty, 2)
        self.assertEqual(fbm_line.marketplace_offer_id, self.fbm_offer)
        self.assertAlmostEqual(fbm_line.price_unit, 120.0)               # the CURRENT offer price
        self.assertNotIn(self.fbl_product, by_product)                   # unpublished
        self.assertNotIn(self.b_product, by_product)                     # no live offer
        # the customer is told what was skipped
        self.assertIn("could not be added again", response.text)
        self.assertIn("Cancel B Product", response.text)
        self.assertIn("Vendor FBL Product", response.text)
        self.assertGreaterEqual(self.env["sale.order"].sudo().search_count([("state", "=", "draft")]), before)

    def test_buy_again_of_a_vendor_only_checkout_and_button_is_shown(self):
        sale = self._confirmed([(self.fbm_product, 1)])
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/marketplace/checkouts/%s" % sale.id)
        self.assertTrue(dom.xpath("//form[@id='lm_buy_again_form']"))
        token = self._csrf("/my/marketplace/checkouts/%s" % sale.id)
        self.url_open("/my/marketplace/checkouts/%s/buy-again" % sale.id, data={"csrf_token": token})
        cart = self._cart()
        line = cart.order_line.filtered(lambda l: l.product_id == self.fbm_product)
        self.assertEqual(line.product_uom_qty, 1)
        self.assertEqual(line.marketplace_offer_id, self.fbm_offer)
        # repeating adds the quantity again (standard cart behaviour)
        self.url_open("/my/marketplace/checkouts/%s/buy-again" % sale.id, data={"csrf_token": token})
        self.assertEqual(cart.order_line.filtered(lambda l: l.product_id == self.fbm_product).product_uom_qty, 2)

    def test_buy_again_needs_csrf(self):
        sale = self._confirmed([(self.fbm_product, 1)])
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        response = self.url_open("/my/marketplace/checkouts/%s/buy-again" % sale.id, data={"x": "1"})
        self.assertIn(response.status_code, (400, 403))
        self.assertFalse(self._cart().order_line.filtered(lambda l: l.product_id == self.fbm_product))

    def test_vendor_order_helpers_reused_by_the_order_page(self):
        sale = self._confirmed([(self.fbm_product, 2)])
        vorder = sale.vendor_order_ids
        self.assertEqual(vorder._customer_returnable_lines(), [])
        self._deliver_group(vorder)
        self.assertEqual([(l, q) for l, q in vorder._customer_returnable_lines()], [(vorder.line_ids[:1], 2.0)])
        can_rate, rating = vorder._customer_rating_info()
        self.assertFalse(rating)
        self.assertFalse(can_rate)    # catalog offers switch is off
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/marketplace/orders/%s" % vorder.id)
        self.assertTrue(dom.xpath("//div[@id='lm_return_card']"))
