"""A2: the customer cancels a whole checkout or one seller's part."""
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD
from .test_vendor_split import demo_provider


class CustomerCancelCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._clean_slate()
        env = cls.env
        cls.vendor_b = env["res.partner"].create({
            "name": "Cancel Vendor B", "is_marketplace_vendor": True, "vendor_state": "approved"})
        cls.vendor_b._marketplace_ensure_vendor_stock()
        cls.b_product = env["product.product"].create({
            "name": "Cancel B Product", "type": "consu", "is_storable": True,
            "list_price": 1.0, "taxes_id": [Command.clear()]})
        cls.b_offer = env["leaders.marketplace.offer"].create({
            "product_tmpl_id": cls.b_product.product_tmpl_id.id, "product_id": cls.b_product.id,
            "vendor_id": cls.vendor_b.id, "seller_sku": "CXL-B", "price": 80.0,
            "fulfillment_type": "fbm",
            "stock_location_id": cls.vendor_b._marketplace_stock_location("fbm").id})
        env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.b_product, cls.vendor_b.vendor_location_id, cls.vendor_b, 5)
        cls.b_offer.action_approve()
        cls.customer.email = "lm.cancel.buyer@example.com"
        cls.customer_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Cancelling Carol", "login": "lm.cancel.buyer@example.com",
            "email": "lm.cancel.buyer@example.com", "password": PORTAL_PASSWORD,
            "partner_id": cls.customer.id,
            "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
        })
        cls.reason = cls.env.ref("leaders_marketplace.cancel_reason_changed_mind")
        set_param = cls.env["ir.config_parameter"].sudo().set_param
        set_param("leaders_marketplace.customer_can_cancel_orders", "True")
        set_param("leaders_marketplace.cancel_window_hours", "0")
        set_param("leaders_marketplace.cod_enabled", "True")
        for key in ("customer_order_cancelled_by_customer", "vendor_order_cancelled_by_customer",
                    "customer_order_cancelled"):
            set_param("leaders_marketplace.notify_%s" % key, "True")

    def _sale(self, lines, cod=False):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": p.id, "product_uom_qty": q})
                           for p, q in lines],
        })
        if cod:
            order.leaders_payment_method = "cod"
        return order

    def _pay_online(self, order):
        """Confirm through a done payment transaction, with an automatic invoice."""
        provider = demo_provider(self)
        provider.state = "test"
        self.env["ir.config_parameter"].sudo().set_param("sale.automatic_invoice", True)
        tx = self.env["payment.transaction"].create({
            "provider_id": provider.id, "operation": "online_redirect",
            "payment_method_id": provider.payment_method_ids[:1].id,
            "reference": "LM-CXL-%s" % order.id, "amount": order.amount_total,
            "currency_id": order.currency_id.id, "partner_id": self.customer.id,
            "sale_order_ids": [Command.set(order.ids)],
        })
        tx._set_done()
        tx._post_process()
        return tx

    def _ship(self, vendor_order):
        group = vendor_order.fulfillment_group_ids[:1]
        group.tracking_ref = "CXL-TRK"
        group.action_ship()

    def _mails(self, since):
        return self.env["mail.mail"].sudo().search([("id", ">", since)])

    def _last_mail(self):
        return self.env["mail.mail"].sudo().search([], order="id desc", limit=1).id or 0


@tagged("post_install", "-at_install")
class TestCustomerCancel(CustomerCancelCommon):

    def test_off_by_default_semantics(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.customer_can_cancel_orders", False)
        order = self._sale([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        self.assertFalse(vendor_order._customer_cancel_info()["enabled"])
        with self.assertRaises(UserError):
            vendor_order.action_customer_cancel(self.reason.id, scope="vendor")
        self.assertEqual(vendor_order.state, "processing")

    def test_whole_checkout_paid_online_cancels_everything_and_prepares_refunds(self):
        self._configure_accounts()
        order = self._sale([(self.own_product, 1), (self.fbm_product, 1), (self.b_product, 1)])
        self._pay_online(order)
        vendor_orders = order.vendor_order_ids
        self.assertEqual(len(vendor_orders), 2)
        self.assertEqual(order.state, "sale")
        picking_before = (order.picking_ids | vendor_orders.picking_ids)
        self.assertTrue(picking_before)

        vendor_orders[:1].action_customer_cancel(self.reason.id, "not needed", scope="all")

        for vendor_order in vendor_orders:
            self.assertEqual(vendor_order.state, "cancelled")
            self.assertEqual(vendor_order.cancelled_by_type, "customer")
            self.assertEqual(vendor_order.cancel_reason_id, self.reason)
            self.assertEqual(vendor_order.cancel_note, "not needed")
            self.assertTrue(vendor_order.cancel_date)
            refund = vendor_order.refund_payment_id
            self.assertTrue(refund, "no refund for %s" % vendor_order.name)
            self.assertEqual(refund.state, "draft")
            self.assertEqual(refund.payment_type, "outbound")
            self.assertEqual(refund.amount, vendor_order.amount_total)
        self.assertEqual(order.state, "cancel")
        self.assertEqual(order.leaders_cancelled_by_type, "customer")
        self.assertEqual(order.leaders_cancel_reason_id, self.reason)
        refund = order.leaders_refund_payment_id
        self.assertTrue(refund)
        self.assertEqual(refund.state, "draft")
        self.assertEqual(refund.amount, 50.0)
        self.assertEqual(refund.leaders_cancelled_sale_id, order)
        credit_note = self.env["account.move"].search([
            ("reversed_entry_id", "in", order.invoice_ids.ids)])
        self.assertTrue(credit_note)
        self.assertEqual(credit_note.state, "draft")
        self.assertTrue(all(p.state in ("cancel", "done") for p in picking_before))
        self.assertFalse(picking_before.filtered(lambda p: p.state == "done"))

    def test_company_part_paid_by_transaction_without_invoice_refunds_its_amount(self):
        self._configure_accounts()
        self.env["ir.config_parameter"].sudo().set_param("sale.automatic_invoice", False)
        order = self._sale([(self.own_product, 1), (self.fbm_product, 1)])
        provider = demo_provider(self)
        provider.state = "test"
        tx = self.env["payment.transaction"].create({
            "provider_id": provider.id, "operation": "online_redirect",
            "payment_method_id": provider.payment_method_ids[:1].id,
            "reference": "LM-CXL2-%s" % order.id, "amount": order.amount_total,
            "currency_id": order.currency_id.id, "partner_id": self.customer.id,
            "sale_order_ids": [Command.set(order.ids)],
        })
        tx._set_done()
        tx._post_process()
        self.assertFalse(order.invoice_ids.filtered(lambda m: m.state == "posted"))
        order.vendor_order_ids.action_customer_cancel(self.reason.id, scope="all")
        self.assertEqual(order.state, "cancel")
        self.assertEqual(order.leaders_refund_payment_id.amount, 50.0)
        self.assertEqual(order.leaders_refund_payment_id.state, "draft")
        # Posting it matches the customer's remaining credit; nothing is left open.
        order.leaders_refund_payment_id.action_post()
        for vendor_order in order.vendor_order_ids:
            vendor_order.refund_payment_id.action_post()
        lines = self.env["account.move.line"].search([
            ("partner_id", "=", self.customer.commercial_partner_id.id),
            ("account_id.account_type", "=", "asset_receivable"),
            ("parent_state", "=", "posted"), ("reconciled", "=", False)])
        self.assertEqual(self.company.currency_id.round(sum(lines.mapped("amount_residual"))), 0.0)

    def test_one_seller_part_leaves_the_rest(self):
        self._configure_accounts()
        order = self._sale([(self.own_product, 1), (self.fbm_product, 1), (self.b_product, 1)])
        self._pay_online(order)
        fbm_order = order.vendor_order_ids.filtered(lambda v: v.vendor_id == self.vendor)
        other = order.vendor_order_ids - fbm_order
        self.assertEqual(len(fbm_order), 1)
        self.assertTrue(fbm_order._customer_cancel_info()["has_other_parts"])

        fbm_order.action_customer_cancel(self.reason.id, scope="vendor")

        self.assertEqual(fbm_order.state, "cancelled")
        self.assertEqual(fbm_order.cancelled_by_type, "customer")
        self.assertTrue(fbm_order.refund_payment_id)
        self.assertNotEqual(other.state, "cancelled")
        self.assertFalse(other.cancelled_by_type)
        self.assertEqual(order.state, "sale")
        self.assertFalse(order.leaders_cancelled_by_type)
        self.assertFalse(order.leaders_refund_payment_id)

    def test_cod_cancel_moves_no_money_and_is_typed_customer(self):
        self._configure_accounts()
        order = self._sale([(self.own_product, 1), (self.fbm_product, 1)], cod=True)
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        payments_before = self.env["account.payment"].search_count([])

        vendor_order.action_customer_cancel(self.reason.id, scope="all")

        self.assertEqual(vendor_order.state, "cancelled")
        self.assertEqual(vendor_order.payment_method, "cod")
        self.assertEqual(vendor_order.cancelled_by_type, "customer")
        self.assertFalse(vendor_order.refund_payment_id)
        self.assertFalse(order.leaders_refund_payment_id)
        self.assertEqual(order.state, "cancel")
        self.assertEqual(self.env["account.payment"].search_count([]), payments_before)

    def test_shipped_scope_is_refused_and_reported(self):
        order = self._sale([(self.fbm_product, 1), (self.b_product, 1)])
        order.action_confirm()
        fbm_order = order.vendor_order_ids.filtered(lambda v: v.vendor_id == self.vendor)
        fbl_order = order.vendor_order_ids - fbm_order
        self._ship(fbm_order)
        info = fbl_order._customer_cancel_info()
        self.assertEqual(info["all"], "shipped")     # a group of another vendor order shipped
        self.assertIsNone(info["vendor"])            # this seller's own part is untouched
        with self.assertRaises(UserError):
            fbl_order.action_customer_cancel(self.reason.id, scope="all")
        self.assertEqual(fbl_order.state, "processing")
        self.assertEqual(fbm_order._customer_cancel_info()["vendor"], "shipped")
        with self.assertRaises(UserError):
            fbm_order.action_customer_cancel(self.reason.id, scope="vendor")
        fbl_order.action_customer_cancel(self.reason.id, scope="vendor")
        self.assertEqual(fbl_order.state, "cancelled")

    def test_company_part_shipped_blocks_the_whole_checkout(self):
        order = self._sale([(self.own_product, 1), (self.fbm_product, 1)])
        order.action_confirm()
        picking = order.picking_ids[:1]
        self.assertTrue(picking)
        picking.move_ids.quantity = picking.move_ids.product_uom_qty
        picking.move_ids.picked = True
        picking.with_context(skip_backorder=True)._action_done()
        self.assertEqual(picking.state, "done")
        with self.assertRaises(UserError):
            order.vendor_order_ids.action_customer_cancel(self.reason.id, scope="all")
        self.assertEqual(order.vendor_order_ids.state, "processing")

    def test_cancel_window(self):
        order = self._sale([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.cancel_window_hours", "2")
        order.date_order = fields.Datetime.now() - timedelta(hours=3)
        vendor_order.date_order = fields.Datetime.now() - timedelta(hours=3)
        self.assertEqual(vendor_order._customer_cancel_info()["vendor"], "window")
        with self.assertRaises(UserError):
            vendor_order.action_customer_cancel(self.reason.id, scope="vendor")
        vendor_order.date_order = fields.Datetime.now() - timedelta(hours=1)
        vendor_order.action_customer_cancel(self.reason.id, scope="vendor")
        self.assertEqual(vendor_order.state, "cancelled")

    def test_zero_window_means_no_limit(self):
        order = self._sale([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        order.date_order = fields.Datetime.now() - timedelta(days=400)
        vendor_order.date_order = fields.Datetime.now() - timedelta(days=400)
        vendor_order.action_customer_cancel(self.reason.id, scope="all")
        self.assertEqual(vendor_order.state, "cancelled")

    def test_a_reason_is_mandatory_and_must_apply_to_customers(self):
        order = self._sale([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        for bad in (False, "", 99999999):
            with self.assertRaises(UserError):
                vendor_order.action_customer_cancel(bad, scope="vendor")
        vendor_only = self.env["leaders.cancel.reason"].create({
            "name": "Vendor only", "applies_to": "vendor"})
        archived = self.env["leaders.cancel.reason"].create({
            "name": "Old", "applies_to": "customer", "active": False})
        for bad in (vendor_only.id, archived.id):
            with self.assertRaises(UserError):
                vendor_order.action_customer_cancel(bad, scope="vendor")
        self.assertEqual(vendor_order.state, "processing")

    def test_default_reasons_are_seeded(self):
        reasons = self.env["leaders.cancel.reason"].search([("applies_to", "in", ("customer", "both"))])
        self.assertGreaterEqual(len(reasons), 5)

    def test_reservations_are_released_and_buybox_refreshed(self):
        quants = self.env["stock.quant"].search([
            ("product_id", "=", self.fbm_product.id),
            ("location_id", "=", self.fbm_offer.stock_location_id.id)])
        order = self._sale([(self.fbm_product, 2)])
        order.action_confirm()
        self.assertGreater(sum(quants.mapped("reserved_quantity")), 0)
        vendor_order = order.vendor_order_ids
        calls = []
        Offer = type(self.env["leaders.marketplace.offer"])
        original = Offer._recompute_buybox_winner

        def spy(records, product_ids=None):
            calls.append(product_ids)
            return original(records, product_ids=product_ids)

        Offer._recompute_buybox_winner = spy
        try:
            vendor_order.action_customer_cancel(self.reason.id, scope="vendor")
        finally:
            Offer._recompute_buybox_winner = original
        self.assertEqual(sum(quants.mapped("reserved_quantity")), 0)
        self.assertTrue(any(c and self.fbm_product.id in c for c in calls))

    def test_emails_go_to_the_customer_and_the_vendor(self):
        order = self._sale([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        last = self._last_mail()
        vendor_order.action_customer_cancel(self.reason.id, "no thanks", scope="vendor")
        mails = self._mails(last)
        customer_mail = mails.filtered(lambda m: "your cancellation" in (m.subject or "").lower())
        vendor_mail = mails.filtered(lambda m: "do not ship" in (m.subject or "").lower())
        self.assertEqual(len(customer_mail), 1)
        self.assertEqual(customer_mail.recipient_ids, self.customer)
        self.assertEqual(len(vendor_mail), 1)
        self.assertIn(self.owner_user.partner_id, vendor_mail.recipient_ids)
        self.assertIn(self.reason.name, vendor_mail.body_html)
        # The generic seller-cancel mail is not sent on top.
        self.assertFalse(mails.filtered(
            lambda m: (m.subject or "").startswith("Your order") and "was cancelled" in m.subject))

    def test_emails_respect_the_notification_settings(self):
        set_param = self.env["ir.config_parameter"].sudo().set_param
        set_param("leaders_marketplace.notify_customer_order_cancelled_by_customer", False)
        set_param("leaders_marketplace.notify_vendor_order_cancelled_by_customer", False)
        order = self._sale([(self.fbm_product, 1)])
        order.action_confirm()
        last = self._last_mail()
        order.vendor_order_ids.action_customer_cancel(self.reason.id, scope="vendor")
        self.assertFalse(self._mails(last))

    def test_backend_and_vendor_cancels_record_their_own_type(self):
        order = self._sale([(self.fbm_product, 1)])
        order.action_confirm()
        order.vendor_order_ids.action_cancel()
        self.assertEqual(order.vendor_order_ids.cancelled_by_type, "platform")
        self.assertTrue(order.vendor_order_ids.cancel_date)
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.vendor_can_cancel_orders", "True")
        order2 = self._sale([(self.fbm_product, 1)])
        order2.action_confirm()
        order2.vendor_order_ids.action_vendor_cancel("Out of stock", self.owner_user)
        self.assertEqual(order2.vendor_order_ids.cancelled_by_type, "vendor")

    def test_customer_cancellations_do_not_count_in_vendor_kpis(self):
        KPI = self.env["leaders.vendor.kpi"]
        month = fields.Date.context_today(self.env["res.users"]).replace(day=1)
        cancelled_by_customer = self._sale([(self.fbm_product, 1)])
        cancelled_by_customer.action_confirm()
        cancelled_by_platform = self._sale([(self.fbm_product, 1)])
        cancelled_by_platform.action_confirm()
        before = KPI._compute_for_period(self.vendor, month)
        cancelled_by_customer.vendor_order_ids.action_customer_cancel(
            self.reason.id, scope="vendor")
        after_customer = KPI._compute_for_period(self.vendor, month)
        # The customer's cancellation disappears from the counts altogether.
        self.assertEqual(after_customer["order_count"], before["order_count"] - 1)
        self.assertEqual(after_customer["cancellation_rate"], before["cancellation_rate"])
        cancelled_by_platform.vendor_order_ids.action_cancel()
        after_platform = KPI._compute_for_period(self.vendor, month)
        self.assertGreater(after_platform["cancellation_rate"], after_customer["cancellation_rate"])

    def test_a_typeless_cancelled_order_still_counts(self):
        KPI = self.env["leaders.vendor.kpi"]
        month = fields.Date.context_today(self.env["res.users"]).replace(day=1)
        order = self._sale([(self.fbm_product, 1)])
        order.action_confirm()
        before = KPI._compute_for_period(self.vendor, month)
        order.vendor_order_ids.action_cancel()
        order.vendor_order_ids.cancelled_by_type = False    # an order cancelled before A2
        after = KPI._compute_for_period(self.vendor, month)
        self.assertEqual(after["order_count"], before["order_count"])
        self.assertGreater(after["cancellation_rate"], before["cancellation_rate"])

    def test_cancelling_twice_is_refused(self):
        order = self._sale([(self.fbm_product, 1)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        vendor_order.action_customer_cancel(self.reason.id, scope="vendor")
        with self.assertRaises(UserError):
            vendor_order.action_customer_cancel(self.reason.id, scope="vendor")


@tagged("post_install", "-at_install")
class TestCustomerCancelPortal(HttpCase, CustomerCancelCommon):

    def _confirmed(self, lines=None, cod=False):
        order = self._sale(lines or [(self.fbm_product, 1), (self.b_product, 1)], cod=cod)
        order.action_confirm()
        return order

    def _stranger(self):
        partner = self.env["res.partner"].create({"name": "Cancel Stranger"})
        return self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Cancel Stranger", "login": "lm.cancel.stranger@example.com",
            "password": PORTAL_PASSWORD, "partner_id": partner.id,
            "groups_id": [Command.set([self.env.ref("base.group_portal").id])],
        })

    def test_the_page_shows_both_buttons(self):
        order = self._confirmed()
        vendor_order = order.vendor_order_ids[:1]
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/marketplace/orders/%s" % vendor_order.id)
        self.assertIn("Cancel order", page.text)
        self.assertIn("Cancel this seller", page.text)
        self.assertIn("Changed my mind" if False else self.reason.name, page.text)

    def test_the_page_hides_buttons_when_off_or_shipped(self):
        order = self._confirmed()
        vendor_order = order.vendor_order_ids[:1]
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.customer_can_cancel_orders", False)
        page = self.url_open("/my/marketplace/orders/%s" % vendor_order.id)
        self.assertNotIn("lm_cancel_card", page.text)
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.customer_can_cancel_orders", "True")
        self._ship(vendor_order)
        page = self.url_open("/my/marketplace/orders/%s" % vendor_order.id)
        self.assertNotIn("lm_cancel_all_form", page.text)
        self.assertNotIn("lm_cancel_part_form", page.text)
        self.assertIn("Request a return after delivery", page.text)

    def test_post_cancels_one_part_then_the_whole(self):
        order = self._confirmed()
        first, second = order.vendor_order_ids[0], order.vendor_order_ids[1]
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        url = "/my/marketplace/orders/%s" % first.id
        response = self._post(url + "/cancel", url, scope="vendor",
                              reason_id=self.reason.id, note="oops")
        self.assertIn("This seller", response.text)
        self.assertEqual(first.state, "cancelled")
        self.assertEqual(first.cancelled_by_type, "customer")
        self.assertNotEqual(second.state, "cancelled")
        url2 = "/my/marketplace/orders/%s" % second.id
        response = self._post(url2 + "/cancel", url2, scope="all", reason_id=self.reason.id)
        self.assertEqual(second.state, "cancelled")
        page = self.url_open(url2)
        self.assertIn("You cancelled this order", page.text)

    def test_the_page_says_the_refund_is_processing(self):
        self._configure_accounts()
        order = self._sale([(self.fbm_product, 1)])
        self._pay_online(order)
        vendor_order = order.vendor_order_ids
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        url = "/my/marketplace/orders/%s" % vendor_order.id
        self._post(url + "/cancel", url, scope="all", reason_id=self.reason.id)
        self.assertTrue(vendor_order.refund_payment_id)
        page = self.url_open(url)
        self.assertIn("Your refund is being processed", page.text)

    def test_post_without_a_reason_shows_an_error_and_changes_nothing(self):
        order = self._confirmed([(self.fbm_product, 1)])
        vendor_order = order.vendor_order_ids
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        url = "/my/marketplace/orders/%s" % vendor_order.id
        response = self._post(url + "/cancel", url, scope="vendor")
        self.assertIn("Please choose a reason", response.text)
        self.assertEqual(vendor_order.state, "processing")

    def test_another_customer_cannot_cancel(self):
        order = self._confirmed([(self.fbm_product, 1)])
        vendor_order = order.vendor_order_ids
        stranger = self._stranger()
        self.authenticate(stranger.login, PORTAL_PASSWORD)
        token = self._csrf("/my/account")
        response = self.url_open(
            "/my/marketplace/orders/%s/cancel" % vendor_order.id,
            data={"csrf_token": token, "scope": "all", "reason_id": self.reason.id})
        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(vendor_order.state, "processing")

    def test_post_needs_csrf_and_the_setting(self):
        order = self._confirmed([(self.fbm_product, 1)])
        vendor_order = order.vendor_order_ids
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        url = "/my/marketplace/orders/%s/cancel" % vendor_order.id
        response = self.url_open(url, data={"scope": "all", "reason_id": self.reason.id})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(vendor_order.state, "processing")
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.customer_can_cancel_orders", False)
        token = self._csrf("/my/marketplace/orders/%s" % vendor_order.id)
        response = self.url_open(url, data={
            "csrf_token": token, "scope": "all", "reason_id": self.reason.id})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(vendor_order.state, "processing")

    def test_shipped_post_is_refused_with_a_message(self):
        order = self._confirmed([(self.fbm_product, 1)])
        vendor_order = order.vendor_order_ids
        self._ship(vendor_order)
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        url = "/my/marketplace/orders/%s" % vendor_order.id
        response = self._post(url + "/cancel", url, scope="all", reason_id=self.reason.id)
        self.assertIn("already shipped", response.text)
        self.assertNotEqual(vendor_order.state, "cancelled")
