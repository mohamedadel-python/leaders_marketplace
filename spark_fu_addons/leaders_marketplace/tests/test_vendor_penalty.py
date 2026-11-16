"""Vendor penalties that actually bite, and can be taken back.

Banning used to switch off ``vendor_id.user_ids``, but portal users hang off
child contacts, so nobody lost access and the banned vendor's offers kept
selling. Freezing had no way back, and a frozen vendor was locked out of the
orders they still owed their customers.
"""
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD


class PenaltyCommon(MarketplacePortalUserCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Penalty = cls.env["leaders.vendor.penalty"]
        cls.manager = cls.env["res.users"].create({
            "name": "LM Manager", "login": "lm.pen.manager@example.com",
            # Applying a penalty posts to the chatter, which needs a sender.
            "email": "lm.pen.manager@example.com",
            "groups_id": [Command.set([
                cls.env.ref("base.group_user").id,
                cls.env.ref("leaders_marketplace.group_marketplace_manager").id,
            ])],
        })

    def _penalty(self, level, reason="Repeated late shipments", **extra):
        vals = {"vendor_id": self.vendor.id, "level": level, "reason": reason}
        vals.update(extra)
        return self.Penalty.create(vals)

    def _vendor_users(self):
        return self.env["res.users"].with_context(active_test=False).search([
            ("partner_id.commercial_partner_id", "=", self.vendor.id)])


@tagged("post_install", "-at_install")
class TestPenaltyBan(PenaltyCommon):

    def test_a_ban_switches_off_owner_and_staff(self):
        """The bug: portal users hang off child contacts, not the vendor itself."""
        self.assertTrue(self.owner_user.active)
        self.assertTrue(self.staff_user.active)
        self.assertNotIn(self.owner_user, self.vendor.user_ids,
                         "fixture assumption: users are on child contacts")

        penalty = self._penalty("ban")
        penalty.with_user(self.manager).action_apply()

        self.assertEqual(self.vendor.vendor_state, "banned")
        self.assertFalse(self.owner_user.active)
        self.assertFalse(self.staff_user.active)
        self.assertEqual(penalty.state, "applied")
        self.assertEqual(penalty.applied_by, self.manager)

    def test_a_ban_stops_the_offers_selling(self):
        self.assertEqual(self.fbm_offer.state, "active")
        penalty = self._penalty("ban")
        penalty.with_user(self.manager).action_apply()
        self.assertEqual(self.fbm_offer.state, "suspended")
        self.assertEqual(self.fbl_offer.state, "suspended")
        winner = self.env["leaders.marketplace.offer"]._get_marketplace_winner(
            self.fbm_product)
        self.assertFalse(winner, "a banned vendor's product is still on sale")

    def test_lifting_a_ban_restores_exactly_what_was_there(self):
        self.fbl_offer.state = "pending"        # not every offer was active
        penalty = self._penalty("ban")
        penalty.with_user(self.manager).action_apply()
        self.assertEqual(self.fbl_offer.state, "suspended")

        penalty.with_user(self.manager).action_lift()

        self.assertEqual(self.vendor.vendor_state, "approved")
        self.assertEqual(self.fbm_offer.state, "active")
        self.assertEqual(self.fbl_offer.state, "pending", "the snapshot was not honoured")
        self.assertTrue(self.owner_user.active)
        self.assertTrue(self.staff_user.active)
        self.assertEqual(penalty.state, "lifted")

    def test_lifting_only_reactivates_users_this_penalty_switched_off(self):
        other = self._create_vendor_user(
            "Already Off", "lm.off@example.com", "leaders_marketplace.group_vendor_staff")
        other.active = False                    # off before the ban
        penalty = self._penalty("ban")
        penalty.with_user(self.manager).action_apply()
        penalty.with_user(self.manager).action_lift()
        self.assertFalse(other.active, "an unrelated inactive user was switched back on")


@tagged("post_install", "-at_install")
class TestPenaltyFreezeAndWarning(PenaltyCommon):

    def test_a_freeze_suspends_offers_but_keeps_the_users(self):
        penalty = self._penalty("freeze")
        penalty.with_user(self.manager).action_apply()
        self.assertEqual(self.vendor.vendor_state, "frozen")
        self.assertEqual(self.fbm_offer.state, "suspended")
        self.assertTrue(self.owner_user.active, "a freeze must not lock the vendor out")

    def test_a_warning_only_says_so(self):
        before = len(self.vendor.message_ids)
        penalty = self._penalty("warning", reason="Please answer returns faster")
        penalty.with_user(self.manager).action_apply()
        self.assertEqual(self.vendor.vendor_state, "approved")
        self.assertEqual(self.fbm_offer.state, "active")
        self.assertGreater(len(self.vendor.message_ids), before)
        self.assertTrue(any("Please answer returns faster" in body
                            for body in self.vendor.message_ids.mapped("body")))

    def test_a_fine_creates_a_penalty_transaction(self):
        penalty = self._penalty("warning", amount=75.0)
        penalty.with_user(self.manager).action_apply()
        tx = penalty.wallet_transaction_id
        self.assertTrue(tx)
        self.assertEqual(tx.type, "penalty")
        self.assertEqual(tx.amount, -75.0)
        self.assertEqual(tx.state, "available")

    def test_no_fine_creates_no_transaction(self):
        penalty = self._penalty("warning")
        penalty.with_user(self.manager).action_apply()
        self.assertFalse(penalty.wallet_transaction_id)

    def test_the_cron_lifts_an_expired_penalty(self):
        penalty = self._penalty(
            "freeze", date_end=fields.Date.today() - timedelta(days=1))
        penalty.with_user(self.manager).action_apply()
        self.assertEqual(self.vendor.vendor_state, "frozen")

        self.Penalty.cron_lift_expired_penalties()

        self.assertEqual(penalty.state, "lifted")
        self.assertEqual(self.vendor.vendor_state, "approved")
        self.assertEqual(self.fbm_offer.state, "active")

    def test_the_cron_leaves_an_open_ended_penalty_alone(self):
        penalty = self._penalty("freeze")        # no date_end
        penalty.with_user(self.manager).action_apply()
        self.Penalty.cron_lift_expired_penalties()
        self.assertEqual(penalty.state, "applied")
        self.assertEqual(self.vendor.vendor_state, "frozen")

    def test_applying_twice_is_harmless(self):
        penalty = self._penalty("freeze", amount=10.0)
        penalty.with_user(self.manager).action_apply()
        penalty.with_user(self.manager).action_apply()
        self.assertEqual(self.env["leaders.wallet.transaction"].search_count([
            ("vendor_id", "=", self.vendor.id), ("type", "=", "penalty")]), 1)

    def test_a_lifted_penalty_cannot_be_reapplied(self):
        penalty = self._penalty("freeze")
        penalty.with_user(self.manager).action_apply()
        penalty.with_user(self.manager).action_lift()
        with self.assertRaises(UserError):
            penalty.with_user(self.manager).action_apply()

    def test_penalties_are_manager_only(self):
        penalty = self._penalty("freeze")
        with self.assertRaises(AccessError):
            penalty.with_user(self.owner_user).action_apply()
        penalty.action_apply()
        with self.assertRaises(AccessError):
            penalty.with_user(self.owner_user).action_lift()


@tagged("post_install", "-at_install")
class TestFrozenVendorPortal(HttpCase, PenaltyCommon):

    def setUp(self):
        super().setUp()
        self._penalty("freeze", reason="Too many cancellations").action_apply()
        self.assertEqual(self.vendor.vendor_state, "frozen")
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)

    def test_a_frozen_vendor_still_reaches_their_obligations(self):
        for url in ("/my/vendor", "/my/vendor/orders", "/my/vendor/returns",
                    "/my/vendor/disputes", "/my/vendor/finance",
                    "/my/vendor/performance", "/my/vendor/products"):
            with self.subTest(url=url):
                self.assertEqual(self.url_open(url).status_code, 200, url)

    def test_a_frozen_vendor_cannot_list_new_products(self):
        self.assertEqual(self.url_open("/my/vendor/products/new").status_code, 403)

    def test_a_frozen_vendor_cannot_edit_an_offer(self):
        self.assertEqual(
            self.url_open("/my/vendor/products/%s/edit" % self.fbm_offer.id).status_code, 403)

    def test_the_banner_names_the_reason(self):
        page = self.url_open("/my/vendor/orders")
        self.assertIn("Your account is frozen", page.text)
        self.assertIn("Too many cancellations", page.text)

    def test_an_approved_vendor_sees_no_banner(self):
        self.env["leaders.vendor.penalty"].search([
            ("vendor_id", "=", self.vendor.id), ("state", "=", "applied")]).action_lift()
        page = self.url_open("/my/vendor/orders")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Your account is frozen", page.text)
        self.assertEqual(self.url_open("/my/vendor/products/new").status_code, 200)
