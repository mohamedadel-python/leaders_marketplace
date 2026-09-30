"""TASK 29 / D2: returns & disputes center (reason codes, photos, exchange, shipping fee, pickup)."""
import base64
from datetime import timedelta
from unittest.mock import patch

from lxml import html as lxml_html

from odoo import Command, fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PARAM = "leaders_marketplace.returns_center_enabled"


class ReturnsCenterCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Param = cls.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM, "True")
        Param.set_param("leaders_marketplace.return_shipping_fee", 10.0)
        Param.set_param("leaders_marketplace.return_pickup_max_days", 7)
        cls.Return = cls.env["leaders.return.reason"]
        cls.r_damaged = cls.env.ref("leaders_marketplace.return_reason_damaged")
        cls.r_mind = cls.env.ref("leaders_marketplace.return_reason_changed_mind")
        cls.r_desc = cls.env.ref("leaders_marketplace.return_reason_not_as_described")

    def _shipped(self, product, qty):
        order = self._order([(product, qty)])
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        for group in vendor_order.fulfillment_group_ids:
            group.tracking_ref = "TRK"
            group.action_ship()
        return vendor_order

    @staticmethod
    def _photo(name="p.png", raw=PNG):
        return Command.create({"name": name, "datas": base64.b64encode(raw)})

    def _request(self, vendor_order, qty=1, **vals):
        vals.setdefault("reason", "Because")
        return self.env["leaders.vendor.return"].create(dict(vals, **{
            "vendor_order_id": vendor_order.id,
            "line_ids": [Command.create({"vendor_line_id": vendor_order.line_ids[:1].id, "quantity": qty})],
        }))

    def _vendor_stock(self, product, location):
        return sum(self.env["stock.quant"].search([
            ("product_id", "=", product.id), ("location_id", "=", location.id),
            ("owner_id", "=", self.vendor.id)]).mapped("quantity"))


@tagged("post_install", "-at_install")
class TestReturnReasons(ReturnsCenterCommon):

    def test_default_reasons_are_seeded(self):
        reasons = self.env["leaders.return.reason"].search([])
        self.assertGreaterEqual(len(reasons), 5)
        self.assertTrue(self.r_damaged.requires_photo)
        self.assertTrue(self.env.ref("leaders_marketplace.return_reason_wrong_item").requires_photo)
        self.assertFalse(self.r_desc.requires_photo)
        self.assertTrue(self.r_mind.customer_pays_shipping)
        self.assertFalse(self.r_damaged.customer_pays_shipping)

    def test_photo_required_by_the_reason(self):
        vendor_order = self._shipped(self.fbm_product, 2)
        with self.assertRaises(ValidationError):
            self._request(vendor_order, reason_id=self.r_damaged.id)
        ret = self._request(vendor_order, reason_id=self.r_damaged.id, photo_ids=[self._photo()])
        self.assertEqual(len(ret.photo_ids), 1)
        self.assertEqual((ret.photo_ids.res_model, ret.photo_ids.res_id), ("leaders.vendor.return", ret.id))
        # a reason without the flag needs no photo
        self._request(vendor_order, reason_id=self.r_desc.id)

    def test_photo_limit_and_validation(self):
        vendor_order = self._shipped(self.fbm_product, 2)
        five = [self._photo("p%s.png" % i) for i in range(5)]
        self.assertEqual(len(self._request(vendor_order, reason_id=self.r_desc.id, photo_ids=five).photo_ids), 5)
        vendor_order2 = self._shipped(self.fbm_product, 1)
        with self.assertRaises(ValidationError):
            self._request(vendor_order2, reason_id=self.r_desc.id, photo_ids=five + [self._photo("p6.png")])
        with self.assertRaises(ValidationError):
            self._request(vendor_order2, reason_id=self.r_desc.id,
                          photo_ids=[self._photo("fake.png", b"not an image at all")])
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        with self.assertRaises(ValidationError):
            self._request(vendor_order2, reason_id=self.r_desc.id, photo_ids=[self._photo("x.svg", svg)])
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.vendor_image_max_mb", 1)
        big = PNG + b"\0" * (1024 * 1024 + 10)
        with self.assertRaises(ValidationError):
            self._request(vendor_order2, reason_id=self.r_desc.id, photo_ids=[self._photo("big.png", big)])

    def test_fee_flag_is_copied_from_the_reason(self):
        vendor_order = self._shipped(self.fbm_product, 2)
        self.assertTrue(self._request(vendor_order, reason_id=self.r_mind.id).customer_pays_shipping)
        self.assertFalse(self._request(vendor_order, reason_id=self.r_desc.id).customer_pays_shipping)


@tagged("post_install", "-at_install")
class TestReturnExchange(ReturnsCenterCommon):

    def test_exchange_creates_a_zero_charge_replacement(self):
        escrow, income, payable, receivable = self._configure_accounts()
        Accounting = self.env["leaders.marketplace.accounting"]
        vendor_order = self._shipped(self.fbm_product, 2)
        line = vendor_order.line_ids
        self.assertEqual(line.qty_delivered, 2)
        ledger_before = Accounting._leaders_check_ledger(self.vendor)
        wallet_before = self.env["leaders.wallet.transaction"].search_count([("vendor_id", "=", self.vendor.id)])
        state_before = vendor_order.state
        stock_before = self._vendor_stock(self.fbm_product, self.vendor.vendor_location_id)

        ret = self._request(vendor_order, 1, resolution="exchange", reason_id=self.r_desc.id)
        ret.action_vendor_approve()
        ret.action_receive()

        self.assertEqual(ret.state, "exchanged")
        group = ret.replacement_group_ids
        self.assertEqual(len(group), 1)
        self.assertTrue(group.is_replacement)
        self.assertEqual(group.vendor_order_id, vendor_order)
        picking = group.picking_ids
        self.assertEqual(picking.location_id, self.vendor.vendor_location_id)
        self.assertEqual(picking.location_dest_id, self.env.ref("stock.stock_location_customers"))
        self.assertEqual(picking.owner_id, self.vendor)
        self.assertEqual(picking.move_ids.product_uom_qty, 1)
        self.assertFalse(picking.move_ids.leaders_vendor_line_id)
        # goods came back, the replacement is reserved from the same stock
        self.assertEqual(self._vendor_stock(self.fbm_product, self.vendor.vendor_location_id), stock_before + 1)
        self.assertEqual(picking.state, "assigned")
        # no money moved
        self.assertFalse(ret.refund_move_id or ret.refund_payment_id or ret.credit_note_id or ret.wallet_transaction_id)
        self.assertEqual(vendor_order.refunded_amount, 0.0)
        self.assertEqual(Accounting._leaders_check_ledger(self.vendor), ledger_before)
        self.assertEqual(self.env["leaders.wallet.transaction"].search_count(
            [("vendor_id", "=", self.vendor.id)]), wallet_before)
        # the order itself is untouched and the replaced quantity is not lost nor doubled
        self.assertEqual(vendor_order.state, state_before)
        self.assertEqual(line.qty_returned, 0)
        self.assertEqual(line._qty_returnable(), 2)

        # the vendor ships the replacement through the usual flow; the order state does not move
        group.tracking_ref = "TRK-REPL"
        group.action_ship()
        self.assertEqual(group.state, "shipped")
        self.assertEqual(vendor_order.state, state_before)
        self.assertEqual(picking.state, "done")
        self.assertEqual(line.qty_delivered, 2)
        # and delivering the original groups still completes the order
        vendor_order.fulfillment_group_ids.filtered(lambda g: not g.is_replacement).action_delivered()
        self.assertEqual(vendor_order.state, "completed")
        self.assertEqual(Accounting._leaders_check_ledger(self.vendor)["escrow"], 0.0)

    def test_exchange_needs_stock_at_request_time(self):
        vendor_order = self._shipped(self.fbm_product, 2)
        stock = self._vendor_stock(self.fbm_product, self.vendor.vendor_location_id)
        self.assertEqual(stock, 3)
        self.env["stock.quant"]._leaders_adjust_vendor_stock(
            self.fbm_product, self.vendor.vendor_location_id, self.vendor, -stock)
        with self.assertRaises(ValidationError):
            self._request(vendor_order, 1, resolution="exchange", reason_id=self.r_desc.id)
        # a refund is still possible
        self.assertEqual(self._request(vendor_order, 1, reason_id=self.r_desc.id).resolution, "refund")

    def test_exchange_falls_back_to_refund_when_stock_vanished(self):
        vendor_order = self._shipped(self.fbm_product, 2)
        ret = self._request(vendor_order, 1, resolution="exchange", reason_id=self.r_desc.id)
        ret.action_vendor_approve()
        with patch.object(type(ret), "_exchange_shortfall", return_value=["Vendor FBM Product"]):
            ret.action_receive()
        self.assertEqual((ret.state, ret.resolution), ("refunded", "refund"))
        self.assertFalse(ret.replacement_group_ids)
        self.assertEqual(vendor_order.refunded_amount, 100.0)
        self.assertTrue(any("no longer has enough stock" in (m.body or "") for m in ret.message_ids))

    def test_exchange_from_fbl_stock(self):
        vendor_order = self._shipped(self.fbl_product, 2)
        ret = self._request(vendor_order, 1, resolution="exchange", reason_id=self.r_desc.id)
        ret.action_vendor_approve()
        receipt = ret.picking_ids
        for move in receipt.move_ids:
            move.quantity = move.product_uom_qty
        receipt.move_ids.picked = True
        receipt._action_done()
        self.assertEqual(ret.state, "exchanged")
        group = ret.replacement_group_ids
        self.assertEqual(group.fulfillment_type, "fbl")
        self.assertEqual(group.picking_ids.location_id, self.vendor.vendor_fbl_location_id)

    def test_cannot_cancel_an_exchanged_return(self):
        vendor_order = self._shipped(self.fbm_product, 2)
        ret = self._request(vendor_order, 1, resolution="exchange")
        ret.action_vendor_approve()
        ret.action_receive()
        with self.assertRaises(Exception):
            ret.action_cancel()


@tagged("post_install", "-at_install")
class TestReturnShippingFee(ReturnsCenterCommon):

    def _pay(self, vendor_order, receivable, amount):
        bank = self.env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", self.company.id)], limit=1)
        payment = self.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": self.customer.id,
            "amount": amount, "journal_id": bank.id,
        })
        payment.action_post()
        (payment.move_id.line_ids.filtered(lambda l: l.account_id == receivable)
         + vendor_order.collection_move_id.line_ids.filtered(lambda l: l.account_id == receivable)).reconcile()

    def _receivable_balance(self, receivable):
        lines = self.env["account.move.line"].search([
            ("account_id", "=", receivable.id), ("partner_id", "=", self.customer.id),
            ("parent_state", "=", "posted")])
        return round(sum(lines.mapped("balance")), 2)

    def test_fee_is_deducted_from_the_refund_after_escrow_release(self):
        escrow, income, payable, receivable = self._configure_accounts()
        Accounting = self.env["leaders.marketplace.accounting"]
        vendor_order = self._shipped(self.fbm_product, 2)
        self._pay(vendor_order, receivable, 200.0)
        vendor_order.fulfillment_group_ids.action_delivered()

        ret = self._request(vendor_order, 1, reason_id=self.r_mind.id)
        self.assertEqual((ret.refund_amount, ret.shipping_fee_deducted, ret.net_refund_amount), (100.0, 10.0, 90.0))
        ret.action_vendor_approve()
        ret.action_receive()
        self.assertEqual(ret.state, "refunded")
        move = ret.refund_move_id
        # the vendor bears the goods value, the customer gets refund minus fee, the marketplace keeps the fee
        self.assertEqual(move.line_ids.filtered(lambda l: l.account_id == payable).debit, 100.0)
        self.assertEqual(move.line_ids.filtered(lambda l: l.account_id == receivable).credit, 90.0)
        self.assertEqual(move.line_ids.filtered(lambda l: l.account_id == income).credit, 10.0)
        self.assertEqual(ret.vendor_deduction, 100.0)
        self.assertEqual(ret.wallet_transaction_id.amount, -100.0)
        payment = ret.refund_payment_id
        self.assertEqual(payment.amount, 90.0)
        payment.action_post()
        self.assertEqual(self._receivable_balance(receivable), 0.0)
        ledger = Accounting._leaders_check_ledger(self.vendor)
        self.assertEqual(ledger["escrow"], 0.0)
        self.assertEqual(ledger["payable"], ledger["wallet"]["unsettled"])
        self.assertEqual(ledger["payable"], 100.0)
        self.assertEqual(ret.credit_note_id.payment_state, "paid")

    def test_fee_before_escrow_release_and_customer_had_not_paid(self):
        escrow, income, payable, receivable = self._configure_accounts()
        Accounting = self.env["leaders.marketplace.accounting"]
        vendor_order = self._shipped(self.fbm_product, 2)
        ret = self._request(vendor_order, 1, reason_id=self.r_mind.id)
        ret.action_vendor_approve()
        ret.action_receive()
        move = ret.refund_move_id
        self.assertEqual(move.line_ids.filtered(lambda l: l.account_id == escrow).debit, 100.0)
        self.assertEqual(move.line_ids.filtered(lambda l: l.account_id == income).credit, 10.0)
        self.assertFalse(ret.refund_payment_id)
        # the customer's debt fell by the net refund: what is left is the other unit + the fee
        self.assertEqual(self._receivable_balance(receivable), 110.0)
        vendor_order.fulfillment_group_ids.action_delivered()
        ledger = Accounting._leaders_check_ledger(self.vendor)
        self.assertEqual(ledger["escrow"], 0.0)
        self.assertEqual(ledger["payable"], ledger["wallet"]["unsettled"])

    def test_fee_uses_the_fee_income_account_when_set(self):
        escrow, income, payable, receivable = self._configure_accounts()
        fee_account = self.env["account.account"].create(
            {"name": "Fee income", "code": "LMFEE1", "account_type": "income"})
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.fee_income_account_id", fee_account.id)
        vendor_order = self._shipped(self.fbm_product, 2)
        ret = self._request(vendor_order, 1, reason_id=self.r_mind.id)
        ret.action_vendor_approve()
        ret.action_receive()
        self.assertEqual(ret.refund_move_id.line_ids.filtered(lambda l: l.account_id == fee_account).credit, 10.0)
        self.assertFalse(ret.refund_move_id.line_ids.filtered(lambda l: l.account_id == income))

    def test_fee_is_capped_by_the_refund_and_ignored_for_other_cases(self):
        vendor_order = self._shipped(self.fbm_product, 3)
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.return_shipping_fee", 500.0)
        self.assertEqual(self._request(vendor_order, 1, reason_id=self.r_mind.id).shipping_fee_deducted, 100.0)
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.return_shipping_fee", 10.0)
        # reason without the flag, fee 0 and exchange never pay
        self.assertEqual(self._request(vendor_order, 1, reason_id=self.r_desc.id).shipping_fee_deducted, 0.0)
        self.assertEqual(self._request(vendor_order, 1, reason_id=self.r_mind.id,
                                       resolution="exchange").shipping_fee_deducted, 0.0)
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.return_shipping_fee", 0.0)
        vendor_order2 = self._shipped(self.fbm_product, 1)
        self.assertEqual(self._request(vendor_order2, 1, reason_id=self.r_mind.id).shipping_fee_deducted, 0.0)

    def test_switch_off_never_deducts(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM, False)
        vendor_order = self._shipped(self.fbm_product, 2)
        ret = self._request(vendor_order, 1, reason_id=self.r_mind.id)
        self.assertEqual((ret.shipping_fee_deducted, ret.net_refund_amount), (0.0, 100.0))


@tagged("post_install", "-at_install")
class TestReturnPickupDate(ReturnsCenterCommon):

    def test_pickup_date_schedules_the_fbl_receipt(self):
        vendor_order = self._shipped(self.fbl_product, 2)
        day = fields.Date.context_today(self.env["leaders.vendor.return"]) + timedelta(days=3)
        ret = self._request(vendor_order, 1, pickup_date=day)
        ret.action_vendor_approve()
        self.assertEqual(ret.picking_ids.scheduled_date.date(), day)
        self.assertIn(day.strftime("%Y"), ret._leaders_next_step())

    def test_pickup_date_window_and_fbm_refusal(self):
        today = fields.Date.context_today(self.env["leaders.vendor.return"])
        vendor_order = self._shipped(self.fbl_product, 3)
        with self.assertRaises(ValidationError):
            self._request(vendor_order, 1, pickup_date=today + timedelta(days=8))
        with self.assertRaises(ValidationError):
            self._request(vendor_order, 1, pickup_date=today - timedelta(days=1))
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.return_pickup_max_days", 10)
        self.assertTrue(self._request(vendor_order, 1, pickup_date=today + timedelta(days=8)))
        fbm_order = self._shipped(self.fbm_product, 1)
        with self.assertRaises(ValidationError):
            self._request(fbm_order, 1, pickup_date=today + timedelta(days=1))

    def test_no_pickup_date_keeps_the_default_schedule(self):
        vendor_order = self._shipped(self.fbl_product, 2)
        ret = self._request(vendor_order, 1)
        ret.action_vendor_approve()
        self.assertTrue(ret.picking_ids.scheduled_date)
        self.assertFalse(ret.pickup_date)


@tagged("post_install", "-at_install")
class TestNextStepTexts(ReturnsCenterCommon):

    def test_every_return_state_has_a_next_step(self):
        vendor_order = self._shipped(self.fbm_product, 5)
        ret = self._request(vendor_order, 1)
        seen = {}
        for state in ("requested", "approved", "rejected", "escalated", "received", "refunded",
                      "closed", "cancelled", "exchanged"):
            ret.state = state
            text = ret._leaders_next_step()
            self.assertTrue(text and isinstance(text, str), state)
            seen[state] = text
        self.assertEqual(len(set(seen.values())), len(seen))
        ret.state = "requested"
        self.assertIn("before", ret._leaders_next_step())
        self.assertTrue(ret._leaders_deadline())
        ret.state = "approved"
        self.assertIn("seller", ret._leaders_next_step())
        self.assertFalse(ret._leaders_deadline())
        ret.state = "received"
        ret.resolution = "exchange"
        self.assertIn("replacement", ret._leaders_next_step())
        ret.state = "refunded"
        ret.shipping_fee_deducted = 10.0
        self.assertIn("fee", ret._leaders_next_step())

    def test_fbl_approved_next_step(self):
        ret = self._request(self._shipped(self.fbl_product, 2), 1)
        ret.state = "approved"
        self.assertIn("warehouse", ret._leaders_next_step())

    def test_every_dispute_state_has_a_next_step(self):
        vendor_order = self._shipped(self.fbm_product, 1)
        dispute = self.env["leaders.vendor.dispute"].create({"vendor_order_id": vendor_order.id, "reason": "x"})
        texts = set()
        for state in ("open", "vendor_reply", "escalated", "resolved", "closed"):
            dispute.state = state
            text = dispute._leaders_next_step()
            self.assertTrue(text, state)
            texts.add(text)
        self.assertEqual(len(texts), 5)
        dispute.state = "open"
        self.assertTrue(dispute._leaders_deadline())
        dispute.state = "vendor_reply"
        self.assertFalse(dispute._leaders_deadline())

    def test_timeline_steps_cover_every_state(self):
        ret = self._request(self._shipped(self.fbm_product, 2), 1)
        for state in ("requested", "approved", "rejected", "escalated", "received", "refunded",
                      "closed", "cancelled", "exchanged"):
            ret.state = state
            steps = ret._leaders_timeline_steps()
            self.assertTrue(steps)
            self.assertTrue(all(s["status"] in ("done", "current", "todo") for s in steps), state)


@tagged("post_install", "-at_install")
class TestReturnsCenterPortal(HttpCase, ReturnsCenterCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Center Buyer", "login": "lm.center.buyer@example.com",
            "email": "lm.center.buyer@example.com", "password": PORTAL_PASSWORD,
            "partner_id": cls.customer.id,
            "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
        })
        cls.other = cls.env["res.partner"].create({"name": "Other Center Buyer", "email": "lm.center.other@example.com"})
        cls.other_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Other Center Buyer", "login": "lm.center.other@example.com",
            "email": "lm.center.other@example.com", "password": PORTAL_PASSWORD,
            "partner_id": cls.other.id,
            "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
        })

    def _dom(self, path, code=200):
        response = self.url_open(path)
        self.assertEqual(response.status_code, code, path)
        return lxml_html.fromstring(response.content) if code == 200 else None

    def test_center_page_lists_returns_and_disputes_with_next_step(self):
        vendor_order = self._shipped(self.fbm_product, 3)
        ret = self._request(vendor_order, 1, reason_id=self.r_desc.id)
        dispute = self.env["leaders.vendor.dispute"].create({"vendor_order_id": vendor_order.id, "reason": "late"})
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/marketplace/returns")
        self.assertTrue(dom.xpath("//div[@id='lm_customer_returns']"))
        self.assertTrue(dom.xpath("//div[@id='lm_returns_filters']//a[contains(@class, 'lm-chip')]"))
        self.assertTrue(dom.xpath("//a[@href='/my/marketplace/returns/%s']" % ret.id))
        text = " ".join(dom.xpath("string(//div[@id='lm_customer_returns'])").split())
        self.assertIn(ret._leaders_next_step(), text)
        self.assertIn(dispute._leaders_next_step(), text)
        self.assertTrue(dom.xpath("//span[contains(@class, 'lm-badge')]"))
        # filters
        dom = self._dom("/my/marketplace/returns?filter=completed")
        self.assertFalse(dom.xpath("//a[@href='/my/marketplace/returns/%s']" % ret.id))
        dom = self._dom("/my/marketplace/returns?filter=active")
        self.assertTrue(dom.xpath("//a[@href='/my/marketplace/returns/%s']" % ret.id))
        dom = self._dom("/my/marketplace/returns?filter=bogus")
        self.assertTrue(dom.xpath("//a[@href='/my/marketplace/returns/%s']" % ret.id))

    def test_detail_page_ownership_and_content(self):
        vendor_order = self._shipped(self.fbm_product, 3)
        ret = self._request(vendor_order, 1, reason_id=self.r_damaged.id, photo_ids=[self._photo()])
        photo = ret.photo_ids
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/marketplace/returns/%s" % ret.id)
        self.assertTrue(dom.xpath("//div[@id='lm_return_next_step']"))
        self.assertTrue(dom.xpath("//ol[@id='lm_return_timeline']//li"))
        self.assertTrue(dom.xpath("//div[@id='lm_return_photos_card']//img"))
        self.assertTrue(dom.xpath("//form[contains(@action, '/escalate')]"))
        response = self.url_open("/my/marketplace/returns/%s/photo/%s" % (ret.id, photo.id))
        self.assertEqual((response.status_code, response.headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(response.content, PNG)
        self.assertEqual(self.url_open("/my/marketplace/returns/%s/photo/%s" % (ret.id, photo.id + 9999)).status_code, 404)
        # the vendor sees the photo on its own detail page
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/vendor/returns/%s" % ret.id)
        self.assertTrue(dom.xpath("//div[@id='lm_vendor_return_photos']//img"))
        self.assertEqual(self.url_open("/my/vendor/returns/%s/photo/%s" % (ret.id, photo.id)).status_code, 200)
        # somebody else cannot read either
        self.authenticate(self.other_user.login, PORTAL_PASSWORD)
        self.assertEqual(self.url_open("/my/marketplace/returns/%s" % ret.id).status_code, 403)
        self.assertEqual(self.url_open("/my/marketplace/returns/%s/photo/%s" % (ret.id, photo.id)).status_code, 403)
        self.assertNotEqual(self.url_open("/my/vendor/returns/%s/photo/%s" % (ret.id, photo.id)).status_code, 200)

    def test_escalate_from_the_detail_page_returns_to_it(self):
        vendor_order = self._shipped(self.fbm_product, 3)
        ret = self._request(vendor_order, 1, reason_id=self.r_desc.id)
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        detail = "/my/marketplace/returns/%s" % ret.id
        response = self._post("%s/escalate" % detail, detail, back="detail")
        self.assertEqual(ret.state, "escalated")
        self.assertTrue(response.url.endswith(detail), response.url)

    def test_return_form_with_reason_photo_resolution_and_pickup(self):
        vendor_order = self._shipped(self.fbl_product, 2)
        vendor_order.fulfillment_group_ids.action_delivered()
        line = vendor_order.line_ids
        url = "/my/marketplace/orders/%s" % vendor_order.id
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom(url)
        self.assertTrue(dom.xpath("//select[@name='reason_id']"))
        self.assertTrue(dom.xpath("//input[@type='file' and @name='photos']"))
        self.assertTrue(dom.xpath("//input[@name='resolution' and @value='exchange']"))
        self.assertTrue(dom.xpath("//input[@name='pickup_date']"))
        day = fields.Date.context_today(self.env["leaders.vendor.return"]) + timedelta(days=2)
        # missing photo for a photo-required reason: refused with a message, nothing created
        self._post("%s/return" % url, url, **{"qty_%s" % line.id: "1", "reason_id": str(self.r_damaged.id)})
        self.assertFalse(vendor_order.return_ids)
        response = self._post(
            "%s/return" % url, url, files=[("photos", ("a.png", PNG, "image/png"))],
            **{"qty_%s" % line.id: "1", "reason_id": str(self.r_damaged.id), "reason": "Cracked",
               "resolution": "exchange", "pickup_date": day.isoformat()})
        ret = vendor_order.return_ids
        self.assertEqual(len(ret), 1, response.url)
        self.assertEqual((ret.reason_id, ret.resolution, ret.pickup_date), (self.r_damaged, "exchange", day))
        self.assertEqual(len(ret.photo_ids), 1)
        self.assertIn("Cracked", ret.reason)
        # more than five photos is refused
        files = [("photos", ("p%s.png" % i, PNG, "image/png")) for i in range(6)]
        self._post("%s/return" % url, url, files=files,
                   **{"qty_%s" % line.id: "1", "reason_id": str(self.r_desc.id)})
        self.assertEqual(len(vendor_order.return_ids), 1)
        # a non-image is refused
        self._post("%s/return" % url, url, files=[("photos", ("a.png", b"hello", "image/png"))],
                   **{"qty_%s" % line.id: "1", "reason_id": str(self.r_desc.id)})
        self.assertEqual(len(vendor_order.return_ids), 1)

    def test_switch_off_keeps_the_old_form_page_and_routes(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM, False)
        vendor_order = self._shipped(self.fbm_product, 3)
        vendor_order.fulfillment_group_ids.action_delivered()
        ret = self._request(vendor_order, 1)
        line = vendor_order.line_ids
        url = "/my/marketplace/orders/%s" % vendor_order.id
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom(url)
        self.assertFalse(dom.xpath("//select[@name='reason_id']"))
        self.assertTrue(dom.xpath("//textarea[@name='reason' and @required]"))
        page = self._dom("/my/marketplace/returns")
        self.assertFalse(page.xpath("//div[@id='lm_returns_filters']"))
        self.assertIn(ret.name, " ".join(page.xpath("string(//table)").split()))
        self.assertFalse(page.xpath("//a[@href='/my/marketplace/returns/%s']" % ret.id))
        self.assertEqual(self.url_open("/my/marketplace/returns/%s" % ret.id).status_code, 404)
        # the old form still creates a return with the free text
        self._post("%s/return" % url, url, **{"qty_%s" % line.id: "1", "reason": "Old way"})
        self.assertEqual(vendor_order.return_ids.filtered(lambda r: r.reason == "Old way").reason_id.id, False)

    def test_pages_render_in_english_and_arabic(self):
        website = self.env["website"].search([], limit=1)
        lang = self.env["res.lang"]._activate_lang("ar_001")
        website.language_ids = [Command.link(lang.id)]
        prefix = "/" + lang.url_code
        vendor_order = self._shipped(self.fbm_product, 3)
        vendor_order.fulfillment_group_ids.action_delivered()
        ret = self._request(vendor_order, 1, reason_id=self.r_desc.id, resolution="exchange")
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        for pre in ("", prefix):
            for path in ("/my/marketplace/returns", "/my/marketplace/returns/%s" % ret.id,
                         "/my/marketplace/orders/%s" % vendor_order.id):
                response = self.url_open(pre + path)
                self.assertEqual(response.status_code, 200, pre + path)
                dom = lxml_html.fromstring(response.content)
                rtl = bool(dom.xpath("//div[@id='wrapwrap' and contains(@class, 'o_rtl')]"))
                self.assertEqual(rtl, pre == prefix, pre + path)
