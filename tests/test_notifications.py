"""Email notifications: one queued mail per event, to the right people, switchable."""
from odoo import Command, fields
from odoo.tests import tagged

from .test_security_rpc import MarketplacePortalUserCommon
from . import test_vendor_application as va


@tagged("post_install", "-at_install")
class TestNotifications(MarketplacePortalUserCommon):

    _doc = va.TestVendorApplication._doc
    _application = va.TestVendorApplication._application
    _to_review = va.TestVendorApplication._to_review

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.category = cls.env["product.public.category"].create({"name": "Notify Category"})
        cls.customer.email = "notify.customer@example.com"
        cls.env["stock.quant"]._leaders_adjust_vendor_stock(
            cls.fbm_product, cls.vendor.vendor_location_id, cls.vendor, 100)
        cls.reviewer = cls.env["res.users"].create({
            "name": "Notify Reviewer", "login": "lm.notify.reviewer@example.com",
            "groups_id": [Command.set([
                cls.env.ref("base.group_user").id,
                cls.env.ref("leaders_marketplace.group_marketplace_manager").id])],
        })

    # ------------------------------------------------------------------ helpers
    @property
    def vendor_team(self):
        return self.owner_user.partner_id | self.staff_user.partner_id

    def _vendor_order(self, shipped=False):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": self.fbm_product.id, "product_uom_qty": 1})],
        })
        order.action_confirm()
        vendor_order = order.vendor_order_ids
        if shipped:
            group = vendor_order.fulfillment_group_ids
            group.tracking_ref = "NOTIFY-TRK"
            group.action_ship()
        return vendor_order

    def _return(self):
        vendor_order = self._vendor_order(shipped=True)
        return self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": vendor_order.line_ids[:1].id, "quantity": 1})],
        })

    def _dispute(self):
        return self.env["leaders.vendor.dispute"].create({
            "vendor_order_id": self._vendor_order().id, "reason": "Late"})

    def _settlement(self, amount):
        return self.env["leaders.settlement"].create({
            "vendor_id": self.vendor.id, "period_from": fields.Date.today(),
            "period_to": fields.Date.today(), "currency_id": self.env.company.currency_id.id,
            "transaction_ids": [Command.create({
                "vendor_id": self.vendor.id, "type": "adjustment", "amount": amount,
                "currency_id": self.env.company.currency_id.id, "state": "available",
                "available_date": fields.Date.today(), "reference": "notify"})],
        })

    def _offer(self, sku):
        product = self.env["product.product"].create({
            "name": "Notify " + sku, "type": "consu", "is_storable": True,
            "marketplace_owner_vendor_id": self.vendor.id, "marketplace_state": "pending"})
        return self.env["leaders.marketplace.offer"].create({
            "product_tmpl_id": product.product_tmpl_id.id, "product_id": product.id,
            "vendor_id": self.vendor.id, "seller_sku": sku, "price": 10.0, "state": "pending",
            "fulfillment_type": "fbm",
            "stock_location_id": self.vendor._marketplace_stock_location("fbm").id})

    def _application_in_review(self, email):
        app = self._application(owner_email=email)
        return self._to_review(app)

    def _penalty(self, applied=False):
        penalty = self.env["leaders.vendor.penalty"].create({
            "vendor_id": self.vendor.id, "level": "warning", "reason": "Late shipping"})
        if applied:
            penalty.action_apply()
        return penalty

    # ---------------------------------------------------------------- the events
    def _events(self):
        """key -> (setup returning the action to measure, subject fragment, recipients).

        ``recipients`` is a partner recordset, or an email address (str)."""
        team, cust = self.vendor_team, self.customer
        n = self.env["leaders.notification"]

        def order_confirmed():
            order = self.env["sale.order"].create({
                "partner_id": self.customer.id,
                "order_line": [Command.create({"product_id": self.fbm_product.id, "product_uom_qty": 1})]})
            return order.action_confirm

        def ret(method, *args):
            record = self._return()
            return lambda: getattr(record, method)(*args)

        def dispute(method, *args):
            record = self._dispute()
            return lambda: getattr(record, method)(*args)

        def settle(amount, method):
            record = self._settlement(amount)
            return getattr(record, method)

        def paid():
            record = self._settlement(50.0)
            record.action_approve()
            return record.action_mark_paid

        def offer_approve():
            return self._offer("N-APP").action_approve

        def offer_reject():
            offer = self._offer("N-REJ")
            offer.reject_reason = "Bad photo"
            return offer.action_reject

        def product_approve():
            return self._offer("N-PAPP").product_tmpl_id.action_marketplace_approve

        def product_reject():
            template = self._offer("N-PREJ").product_tmpl_id
            template.marketplace_reject_reason = "Wrong category"
            return template.action_marketplace_reject

        def app_approve():
            app = self._application_in_review("notify.app1@example.com")
            for doc in app.document_ids:
                doc.action_verify()
            return app.action_approve

        def app_reject():
            app = self._application_in_review("notify.app2@example.com")
            app.reject_reason = "Incomplete"
            return app.action_reject

        def lifted():
            return self._penalty(applied=True).action_lift

        def shipped():
            vendor_order = self._vendor_order()
            group = vendor_order.fulfillment_group_ids
            group.tracking_ref = "NOTIFY-TRK-2"
            return group.action_ship

        def delivered():
            vendor_order = self._vendor_order(shipped=True)
            return vendor_order.fulfillment_group_ids.action_delivered

        def cancelled():
            vendor_order = self._vendor_order()
            return vendor_order.action_cancel

        def refunded():
            record = self._return()
            record.action_vendor_approve()
            return record.action_receive

        return {
            "vendor_order_confirmed": (order_confirmed, "New order", team),
            "vendor_return_requested": (self._return_requested, "Return request", team),
            "vendor_return_escalated": (lambda: ret("action_escalate"), "escalated", team),
            "vendor_return_decided": (lambda: ret("action_platform_approve"), "Marketplace decision", team),
            "vendor_dispute_opened": (self._dispute_opened, "opened", team),
            "vendor_dispute_escalated": (lambda: dispute("action_escalate"), "escalated", team),
            "vendor_dispute_resolved": (lambda: dispute("action_resolve", "Refund agreed"), "resolved", team),
            "vendor_settlement_approved": (lambda: settle(50.0, "action_approve"), "approved", team),
            "vendor_settlement_paid": (paid, "paid", team),
            "vendor_settlement_debt": (lambda: settle(-50.0, "action_approve"), "amount due", team),
            "vendor_offer_approved": (offer_approve, "approved", team),
            "vendor_offer_rejected": (offer_reject, "rejected", team),
            "vendor_product_approved": (product_approve, "approved", team),
            "vendor_product_rejected": (product_reject, "rejected", team),
            "vendor_application_approved": (app_approve, "was approved", None),
            "vendor_application_rejected": (app_reject, "vendor application", "notify.app2@example.com"),
            "vendor_penalty_applied": (self._penalty_applied, "penalty", team),
            "vendor_penalty_lifted": (lifted, "penalty lifted", team),
            "customer_order_shipped": (shipped, "has shipped", cust),
            "customer_order_delivered": (delivered, "was delivered", cust),
            "customer_return_approved": (lambda: ret("action_vendor_approve"), "was approved", cust),
            "customer_return_rejected": (lambda: ret("action_vendor_reject", "Not eligible"), "not accepted", cust),
            "customer_return_refunded": (refunded, "was refunded", cust),
            "customer_order_cancelled": (cancelled, "was cancelled", cust),
        }

    def _return_requested(self):
        vendor_order = self._vendor_order(shipped=True)
        return lambda: self.env["leaders.vendor.return"].create({
            "vendor_order_id": vendor_order.id, "reason": "Damaged",
            "line_ids": [Command.create({"vendor_line_id": vendor_order.line_ids[:1].id, "quantity": 1})]})

    def _dispute_opened(self):
        vendor_order = self._vendor_order()
        return lambda: self.env["leaders.vendor.dispute"].create({
            "vendor_order_id": vendor_order.id, "reason": "Late"})

    def _penalty_applied(self):
        penalty = self.env["leaders.vendor.penalty"].create({
            "vendor_id": self.vendor.id, "level": "warning", "reason": "Late shipping"})
        return penalty.action_apply

    def _run(self, key):
        setup, subject, recipients = self._events()[key]
        action = setup()
        Mail = self.env["mail.mail"].sudo()
        last = Mail.search([], order="id desc", limit=1).id or 0
        action()
        mails = Mail.search([("id", ">", last)]).filtered(
            lambda m: subject.lower() in (m.subject or "").lower())
        return mails, recipients

    def _set(self, key, on):
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.notify_%s" % key, "True" if on else False)

    # -------------------------------------------------------------------- tests
    def test_every_event_queues_one_mail_to_the_right_people(self):
        for key in self._events():
            with self.subTest(key=key):
                self._set(key, True)
                mails, recipients = self._run(key)
                self.assertEqual(len(mails), 1, "%s: %s" % (key, mails.mapped("subject")))
                if isinstance(recipients, str):
                    self.assertEqual(mails.email_to, recipients)
                elif recipients is None:
                    self.assertTrue(mails.recipient_ids)
                    self.assertEqual(mails.recipient_ids.mapped("email"), ["notify.app1@example.com"])
                else:
                    self.assertEqual(mails.recipient_ids, recipients)
                self.assertEqual(mails.state, "outgoing")   # queued, not sent

    def test_nothing_is_queued_when_the_setting_is_off(self):
        for key in self._events():
            with self.subTest(key=key):
                self._set(key, False)
                mails, _recipients = self._run(key)
                self.assertFalse(mails, key)
                self._set(key, True)

    def test_the_vendor_owner_follows_the_vendor_order(self):
        self._set("vendor_order_confirmed", True)
        vendor_order = self._vendor_order()
        self.assertIn(self.owner_user.partner_id, vendor_order.message_partner_ids)
