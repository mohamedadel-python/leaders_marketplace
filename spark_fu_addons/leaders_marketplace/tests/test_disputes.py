"""Disputes: the customer opens one, the vendor answers, the marketplace decides.

The model had fields and states but no sequence, no actions, no way in from
either portal, and the ``dispute_sla_days`` setting was never read.
"""
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD


class DisputeCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Dispute = cls.env["leaders.vendor.dispute"]
        cls.reviewer = cls.env["res.users"].create({
            "name": "LM Dispute Reviewer", "login": "lm.dsp.rev@example.com",
            "email": "lm.dsp.rev@example.com",
            "groups_id": [Command.set([
                cls.env.ref("base.group_user").id,
                cls.env.ref("leaders_marketplace.group_marketplace_reviewer").id,
            ])],
        })
        cls.customer_user = cls.env["res.users"].with_context(
            no_reset_password=True).create({
                "name": "Buying Bob", "login": "lm.dsp.buyer@example.com",
                "email": "lm.dsp.buyer@example.com", "password": PORTAL_PASSWORD,
                "partner_id": cls.customer.id,
                "groups_id": [Command.set([cls.env.ref("base.group_portal").id])],
            })

    @classmethod
    def _vendor_order(cls):
        order = cls.env["sale.order"].create({
            "partner_id": cls.customer.id,
            "order_line": [Command.create({
                "product_id": cls.fbm_product.id, "product_uom_qty": 1})],
        })
        order.action_confirm()
        return order.vendor_order_ids

    def _dispute(self, reason="The box arrived crushed"):
        return self.Dispute.create({
            "vendor_order_id": self._vendor_order().id, "reason": reason})


@tagged("post_install", "-at_install")
class TestDisputeWorkflow(DisputeCommon):

    def test_a_dispute_gets_a_name_and_a_deadline(self):
        dispute = self._dispute()
        self.assertTrue(dispute.name.startswith("DSP/"), dispute.name)
        self.assertNotEqual(dispute.name, "New")
        self.assertTrue(dispute.vendor_deadline)
        self.assertEqual(dispute.state, "open")

    def test_the_deadline_follows_the_sla_setting(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "leaders_marketplace.dispute_sla_days", 7)
        dispute = self._dispute()
        expected = fields.Datetime.now() + timedelta(days=7)
        self.assertLess(abs((dispute.vendor_deadline - expected).total_seconds()), 120)

    def test_the_vendor_reply_moves_the_state(self):
        dispute = self._dispute()
        dispute.action_vendor_reply(message="We are sending a replacement")
        self.assertEqual(dispute.state, "vendor_reply")
        self.assertTrue(any("replacement" in body
                            for body in dispute.message_ids.mapped("body")))

    def test_a_customer_message_restarts_the_vendor_clock(self):
        dispute = self._dispute()
        dispute.action_vendor_reply(message="Nothing wrong on our side")
        self.assertEqual(dispute.state, "vendor_reply")
        old_deadline = dispute.vendor_deadline
        dispute.vendor_deadline = fields.Datetime.now() - timedelta(days=1)

        dispute.action_customer_message("It is still broken")

        self.assertEqual(dispute.state, "open", "the ball is back with the vendor")
        self.assertGreater(dispute.vendor_deadline, old_deadline - timedelta(days=1))

    def test_escalation(self):
        dispute = self._dispute()
        dispute.action_escalate()
        self.assertEqual(dispute.state, "escalated")
        with self.assertRaises(UserError):
            dispute.action_escalate()

    def test_resolving_requires_a_decision(self):
        dispute = self._dispute()
        dispute.action_escalate()
        with self.assertRaises(UserError):
            dispute.with_user(self.reviewer).action_resolve()
        dispute.with_user(self.reviewer).action_resolve(decision="Full refund granted")
        self.assertEqual(dispute.state, "resolved")
        self.assertEqual(dispute.decision, "Full refund granted")

    def test_decisions_are_back_office_only(self):
        dispute = self._dispute()
        with self.assertRaises(AccessError):
            dispute.with_user(self.owner_user).action_resolve(decision="I win")
        with self.assertRaises(AccessError):
            dispute.with_user(self.owner_user).action_close()

    def test_the_back_office_guard_itself_is_wired(self):
        """Asserted on the guard, not on the ACL.

        A portal user is also stopped by the read-only ACL, so the test above
        would pass even with the guard removed. This one exercises the guard
        directly, which is the layer that also covers a sudo-ed caller.
        """
        dispute = self._dispute()
        self.assertIn("leaders.backoffice.mixin", self.Dispute._inherit)
        with self.assertRaises(AccessError):
            dispute.with_user(self.owner_user)._leaders_check_backoffice("reviewer")
        # A reviewer passes it.
        self.assertTrue(
            dispute.with_user(self.reviewer)._leaders_check_backoffice("reviewer"))

    def test_a_closed_dispute_takes_no_more_messages(self):
        dispute = self._dispute()
        dispute.with_user(self.reviewer).action_close()
        with self.assertRaises(UserError):
            dispute.action_customer_message("One more thing")

    def test_the_cron_escalates_an_unanswered_dispute(self):
        dispute = self._dispute()
        self.Dispute.cron_escalate_overdue_disputes()
        self.assertEqual(dispute.state, "open", "escalated before the deadline")

        dispute.vendor_deadline = fields.Datetime.now() - timedelta(hours=1)
        self.Dispute.cron_escalate_overdue_disputes()

        self.assertEqual(dispute.state, "escalated")
        self.assertTrue(any("did not answer" in body
                            for body in dispute.message_ids.mapped("body")))

    def test_the_cron_leaves_an_answered_dispute_alone(self):
        dispute = self._dispute()
        dispute.action_vendor_reply(message="Answered in time")
        dispute.vendor_deadline = fields.Datetime.now() - timedelta(hours=1)
        self.Dispute.cron_escalate_overdue_disputes()
        self.assertEqual(dispute.state, "vendor_reply")


@tagged("post_install", "-at_install")
class TestDisputePortal(HttpCase, DisputeCommon):

    def test_the_customer_opens_a_dispute_and_the_vendor_answers(self):
        vendor_order = self._vendor_order()
        order_url = "/my/marketplace/orders/%s" % vendor_order.id

        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        page = self.url_open(order_url)
        self.assertIn("Report a problem", page.text)
        self._post("%s/dispute" % order_url, order_url,
                   reason="The item never worked")

        dispute = self.Dispute.search([("vendor_order_id", "=", vendor_order.id)])
        self.assertEqual(len(dispute), 1)
        self.assertEqual(dispute.state, "open")

        # The vendor sees it and answers.
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        listing = self.url_open("/my/vendor/disputes")
        self.assertIn(dispute.name, listing.text)
        detail_url = "/my/vendor/disputes/%s" % dispute.id
        self.assertEqual(self.url_open(detail_url).status_code, 200)
        self._post("%s/reply" % detail_url, detail_url,
                   message="Sorry, sending a new one today")

        self.assertEqual(dispute.state, "vendor_reply")
        self.assertTrue(any("sending a new one" in body
                            for body in dispute.message_ids.mapped("body")))

    def test_the_customer_sees_the_conversation_and_can_escalate(self):
        vendor_order = self._vendor_order()
        dispute = self.Dispute.create({
            "vendor_order_id": vendor_order.id, "reason": "Wrong colour"})
        dispute.action_vendor_reply(message="That is the colour you ordered")
        order_url = "/my/marketplace/orders/%s" % vendor_order.id

        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        page = self.url_open(order_url)
        self.assertIn("That is the colour you ordered", page.text)

        self._post("/my/marketplace/disputes/%s/escalate" % dispute.id, order_url)
        self.assertEqual(dispute.state, "escalated")

    def test_the_customer_can_add_a_message(self):
        vendor_order = self._vendor_order()
        dispute = self.Dispute.create({
            "vendor_order_id": vendor_order.id, "reason": "Late"})
        order_url = "/my/marketplace/orders/%s" % vendor_order.id
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        self._post("/my/marketplace/disputes/%s/message" % dispute.id, order_url,
                   message="Any news?")
        self.assertTrue(any("Any news?" in body
                            for body in dispute.message_ids.mapped("body")))

    def test_another_customer_cannot_open_a_dispute_on_this_order(self):
        vendor_order = self._vendor_order()
        stranger_partner = self.env["res.partner"].create({"name": "Stranger"})
        stranger = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Stranger", "login": "lm.stranger@example.com",
            "password": PORTAL_PASSWORD, "partner_id": stranger_partner.id,
            "groups_id": [Command.set([self.env.ref("base.group_portal").id])],
        })

        self.authenticate(stranger.login, PORTAL_PASSWORD)
        # Their own (empty) marketplace order page carries a usable csrf token;
        # the point of the test is the ownership check, not the token.
        response = self.url_open(
            "/my/marketplace/orders/%s/dispute" % vendor_order.id,
            data={"csrf_token": self._stranger_csrf(), "reason": "Give me a refund"})

        self.assertIn(response.status_code, (403, 404))
        self.assertFalse(self.Dispute.search([("vendor_order_id", "=", vendor_order.id)]))

    def test_another_vendor_cannot_read_this_dispute(self):
        vendor_order = self._vendor_order()
        dispute = self.Dispute.create({
            "vendor_order_id": vendor_order.id, "reason": "Private matter"})
        rival = self.env["res.partner"].create({
            "name": "Rival", "is_marketplace_vendor": True, "vendor_state": "approved"})
        rival._marketplace_ensure_vendor_stock()
        contact = self.env["res.partner"].create({
            "name": "Rival Owner", "email": "lm.rival.owner@example.com",
            "parent_id": rival.id, "type": "contact"})
        rival_owner = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Rival Owner", "login": "lm.rival.owner@example.com",
            "password": PORTAL_PASSWORD, "partner_id": contact.id,
            "groups_id": [Command.set([
                self.env.ref("leaders_marketplace.group_vendor_owner").id])],
        })

        self.authenticate(rival_owner.login, PORTAL_PASSWORD)
        response = self.url_open("/my/vendor/disputes/%s" % dispute.id)

        self.assertIn(response.status_code, (403, 404))

    def _stranger_csrf(self):
        """A csrf token from a page this user is allowed to open."""
        return self._csrf("/my/account")

    def test_an_empty_reason_is_refused(self):
        vendor_order = self._vendor_order()
        order_url = "/my/marketplace/orders/%s" % vendor_order.id
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        self._post("%s/dispute" % order_url, order_url, reason="   ")
        self.assertFalse(self.Dispute.search([("vendor_order_id", "=", vendor_order.id)]))


@tagged("post_install", "-at_install")
class TestDisputeBackend(DisputeCommon):

    def test_the_needs_decision_filter_is_the_default(self):
        action = self.env.ref("leaders_marketplace.action_vendor_dispute")
        self.assertIn("search_default_dsp_needs_decision", action.context)

    def test_the_sequence_is_installed(self):
        sequence = self.env["ir.sequence"].search(
            [("code", "=", "leaders.vendor.dispute")], limit=1)
        self.assertTrue(sequence)
        self.assertEqual(sequence.prefix, "DSP/")
