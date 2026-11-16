"""Vendor staff invitations that survive the real world.

Three ways the old route broke: an archived login still holds the unique index
so creation crashed on the constraint, a mail-server failure rolled the whole
user back, and once invited there was no way to deactivate, reactivate or
re-invite anyone.
"""
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .test_security_rpc import MarketplacePortalUserCommon, PORTAL_PASSWORD


class StaffCommon(MarketplacePortalUserCommon):

    def _staff_of(self, vendor):
        return self.env["res.users"].with_context(active_test=False).search([
            ("partner_id.commercial_partner_id", "=", vendor.id)])

    def _user_by_login(self, login):
        return self.env["res.users"].with_context(active_test=False).search([
            ("login", "=", login)], limit=1)


@tagged("post_install", "-at_install")
class TestStaffInvitation(HttpCase, StaffCommon):

    def setUp(self):
        super().setUp()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)

    def _invite(self, name, email):
        return self._post("/my/vendor/staff", "/my/vendor/staff", name=name, email=email)

    def test_a_normal_invitation_creates_a_staff_user(self):
        self._invite("New Hire", "lm.hire@example.com")
        user = self._user_by_login("lm.hire@example.com")
        self.assertTrue(user)
        self.assertTrue(user.active)
        self.assertEqual(user.partner_id.commercial_partner_id, self.vendor)
        self.assertTrue(user.has_group("leaders_marketplace.group_vendor_staff"))

    def test_an_archived_login_gives_a_friendly_error_not_a_crash(self):
        """The bug: an archived user keeps its login and the unique index."""
        self._invite("Left Us", "lm.gone@example.com")
        gone = self._user_by_login("lm.gone@example.com")
        gone.active = False

        page = self._invite("Someone Else", "lm.gone@example.com")

        self.assertEqual(page.status_code, 200)
        self.assertIn("belongs to a deactivated account", page.text)
        # And exactly one user still carries that login.
        self.assertEqual(len(self.env["res.users"].with_context(active_test=False).search([
            ("login", "=", "lm.gone@example.com")])), 1)

    def test_a_live_duplicate_still_says_so(self):
        self._invite("First", "lm.dup@example.com")
        page = self._invite("Second", "lm.dup@example.com")
        self.assertIn("already exists", page.text)

    def test_a_malformed_email_is_refused(self):
        page = self._invite("Bad Email", "not-an-email")
        self.assertIn("valid email address", page.text)
        self.assertFalse(self._user_by_login("not-an-email"))

    def test_the_email_is_normalized(self):
        self._invite("Shouty", "  LM.Shouty@Example.COM  ")
        self.assertTrue(self._user_by_login("lm.shouty@example.com"))

    def test_a_mail_failure_keeps_the_user(self):
        """The account is worth more than the email; Resend covers the rest."""
        target = "odoo.addons.auth_signup.models.res_users.ResUsers.action_reset_password"
        with patch(target, side_effect=UserError("SMTP is down")):
            page = self._invite("Mail Fails", "lm.mailfail@example.com")

        user = self._user_by_login("lm.mailfail@example.com")
        self.assertTrue(user, "the user was rolled back with the failed email")
        self.assertTrue(user.active)
        self.assertIn("invitation email could not be sent", page.text)


@tagged("post_install", "-at_install")
class TestStaffManagement(HttpCase, StaffCommon):

    def setUp(self):
        super().setUp()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)

    def _act(self, action, user):
        url = "/my/vendor/staff/%s/%s" % (user.id, action)
        return self.url_open(url, data={"csrf_token": self._csrf("/my/vendor/staff")})

    def test_the_owner_deactivates_and_reactivates_staff(self):
        self.assertTrue(self.staff_user.active)

        self._act("deactivate", self.staff_user)
        self.assertFalse(self.staff_user.active)

        self._act("reactivate", self.staff_user)
        self.assertTrue(self.staff_user.active)

    def test_a_deactivated_member_cannot_log_in(self):
        self._act("deactivate", self.staff_user)
        with self.assertRaises(Exception):
            self.authenticate(self.staff_user.login, PORTAL_PASSWORD)

    def test_the_owner_cannot_deactivate_themselves(self):
        response = self._act("deactivate", self.owner_user)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.owner_user.active)

    def test_an_owner_cannot_be_deactivated(self):
        second_owner = self._create_vendor_user(
            "Second Owner", "lm.owner2@example.com",
            "leaders_marketplace.group_vendor_owner")
        response = self._act("deactivate", second_owner)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(second_owner.active)

    def test_another_vendors_staff_cannot_be_touched(self):
        other_vendor = self.env["res.partner"].create({
            "name": "Rival Vendor", "is_marketplace_vendor": True, "vendor_state": "approved",
        })
        contact = self.env["res.partner"].create({
            "name": "Rival Staff", "email": "lm.rival@example.com",
            "parent_id": other_vendor.id, "type": "contact",
        })
        rival = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Rival Staff", "login": "lm.rival@example.com",
            "partner_id": contact.id,
            "groups_id": [Command.set([
                self.env.ref("leaders_marketplace.group_vendor_staff").id])],
        })

        response = self._act("deactivate", rival)

        self.assertEqual(response.status_code, 403)
        self.assertTrue(rival.active, "another vendor's staff was deactivated")

    def test_resend_reports_a_mail_failure_without_losing_anything(self):
        target = "odoo.addons.auth_signup.models.res_users.ResUsers.action_reset_password"
        with patch(target, side_effect=UserError("SMTP is down")):
            response = self._act("resend", self.staff_user)
        self.assertEqual(response.status_code, 200)
        self.assertIn("could not be sent", response.text)
        self.assertTrue(self.staff_user.active)

    def test_resend_works(self):
        response = self._act("resend", self.staff_user)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Invitation sent again", response.text)

    def test_staff_cannot_manage_staff(self):
        self.authenticate(self.staff_user.login, PORTAL_PASSWORD)
        self.assertEqual(self.url_open("/my/vendor/staff").status_code, 403)


@tagged("post_install", "-at_install")
class TestStaffList(HttpCase, StaffCommon):

    def test_the_list_shows_status_and_actions(self):
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/staff")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Deactivate", page.text)
        self.assertIn("Resend", page.text)

    def test_a_deactivated_member_still_appears_with_a_way_back(self):
        self.staff_user.active = False
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        page = self.url_open("/my/vendor/staff")
        self.assertIn("Deactivated", page.text)
        self.assertIn("Reactivate", page.text)
