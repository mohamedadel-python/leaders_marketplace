"""Push notifications: devices, the queue, the events with and without an email, and the sending (Firebase replaced)."""
import json
from unittest import mock

from odoo import Command
from odoo.tests import tagged

from .test_catalog_spec import TestStorefrontApi

TOKEN_A = "fcm-token-A-" + "a" * 40
TOKEN_B = "fcm-token-B-" + "b" * 40
TOKEN_C = "fcm-token-C-" + "c" * 40
DELIVER = "odoo.addons.leaders_marketplace.models.push._deliver"
APP = "odoo.addons.leaders_marketplace.models.push._firebase_app"


@tagged("post_install", "-at_install")
class TestPush(TestStorefrontApi):

    def setUp(self):
        super().setUp()
        env = self.env
        params = env["ir.config_parameter"].sudo()
        params.set_param("leaders_marketplace.push_enabled", "True")
        params.set_param("leaders_marketplace.push_firebase_credentials_json", '{"type": "service_account"}')
        params.set_param("leaders_marketplace.notify_test_event", "True")
        self.Device = env["leaders.push.device"]
        self.Message = env["leaders.push.message"]
        # an event of its own: a template on res.partner whose subject and first line are the push
        model = env["ir.model"]._get("res.partner")
        self.template = env["mail.template"].create({
            "name": "Test event", "model_id": model.id, "subject": "Hello {{ object.name }}",
            "body_html": "<p>First line of the mail</p><p>Second line</p>"})
        env["ir.model.data"].create({"module": "leaders_marketplace", "name": "mail_template_test_event",
                                     "model": "mail.template", "res_id": self.template.id})
        self.customer = env["res.partner"].create({"name": "Push Customer", "email": "pc@example.com"})
        self.customer_user = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Push Customer", "login": "pc@example.com", "password": "Secret123!x", "partner_id": self.customer.id,
            "groups_id": [Command.set([env.ref("base.group_portal").id])]})

    # ---- the devices
    def test_register_and_unregister_a_device(self):
        token = self.token()
        r, body = self.send("POST", "/account/devices", {"token": TOKEN_A, "platform": "ios", "locale": "ar"}, token=token)
        self.assertEqual((r.status_code, body), (200, {"registered": True}))
        device = self.Device.search([("token", "=", TOKEN_A)])
        self.assertEqual((device.user_id, device.platform, device.lang), (self.user, "ios", "ar"))
        r, _b = self.send("POST", "/account/devices", {"token": TOKEN_A, "platform": "ios", "locale": "en"}, token=token)
        self.assertEqual((r.status_code, self.Device.search_count([("token", "=", TOKEN_A)]), device.lang), (200, 1, "en"))
        # a second phone of the same person
        self.send("POST", "/account/devices", {"token": TOKEN_B}, token=token)
        self.assertEqual(self.Device.search_count([("user_id", "=", self.user.id)]), 2)
        # the same phone, signed in by somebody else: the token moves
        other_token = __import__("odoo.addons.leaders_marketplace.tools.auth_tokens", fromlist=["x"]).encode_access_token(
            self.env, self.customer_user)
        self.send("POST", "/account/devices", {"token": TOKEN_A}, token=other_token)
        self.assertEqual(self.Device.search([("token", "=", TOKEN_A)]).user_id, self.customer_user)
        # bad bodies and no sign-in
        r, bad = self.send("POST", "/account/devices", {"token": "short", "platform": "windows", "locale": "fr"}, token=token)
        self.assertEqual((r.status_code, sorted(bad["fields"])), (422, ["locale", "platform", "token"]))
        self.assertEqual(self.send("POST", "/account/devices", {"token": TOKEN_C})[0].status_code, 401)
        # out
        r, _b = self.send("DELETE", "/account/devices?token=%s" % TOKEN_B, token=token)
        self.assertEqual((r.status_code, self.Device.search_count([("token", "=", TOKEN_B)])), (204, 0))
        self.send("DELETE", "/account/devices", {"token": TOKEN_A}, token=other_token)  # the token in the body
        self.assertFalse(self.Device.search([("token", "=", TOKEN_A)]))

    def test_a_person_keeps_at_most_ten_devices(self):
        for i in range(12):
            self.Device._leaders_register(self.user, "fcm-%02d-%s" % (i, "x" * 30), "android", "ar")
        self.assertEqual(self.Device.search_count([("user_id", "=", self.user.id)]), 10)

    # ---- an event with an email
    def _event(self, key="test_event", **kw):
        Notification = self.env["leaders.notification"]
        return Notification.notify_customer(key, self.customer, self.customer, **kw)

    def test_the_customers_devices_get_the_event_in_their_language(self):
        self.Device._leaders_register(self.customer_user, TOKEN_A, "android", "en")
        self.Device._leaders_register(self.customer_user, TOKEN_B, "ios", "ar")
        self._event()
        messages = self.Message.search([("event", "=", "test_event")])
        self.assertEqual(sorted(messages.mapped("token")), sorted([TOKEN_A, TOKEN_B]))
        by_token = {m.token: m for m in messages}
        self.assertEqual((by_token[TOKEN_A].title, by_token[TOKEN_A].body), ("Hello Push Customer", "First line of the mail"))
        self.assertEqual(set(messages.mapped("state")), {"pending"})
        self.assertEqual(json.loads(by_token[TOKEN_A].data)["event"], "test_event")
        self.assertEqual(json.loads(by_token[TOKEN_A].data)["model"], "res.partner")

    def test_nothing_is_queued_when_it_is_off_or_nobody_has_a_device(self):
        self._event()  # no device
        self.assertFalse(self.Message.search([("event", "=", "test_event")]))
        self.Device._leaders_register(self.customer_user, TOKEN_A, "android", "en")
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("leaders_marketplace.push_enabled", "False")
        self._event()
        self.assertFalse(self.Message.search([("event", "=", "test_event")]))
        params.set_param("leaders_marketplace.push_enabled", "True")
        params.set_param("leaders_marketplace.notify_test_event", "False")  # the event's own switch
        self._event()
        self.assertFalse(self.Message.search([("event", "=", "test_event")]))

    def test_the_customers_preferences_decide_for_the_push_too(self):
        from odoo.addons.leaders_marketplace.models import notification as notification_module
        self.Device._leaders_register(self.customer_user, TOKEN_A, "android", "en")
        with mock.patch.dict(notification_module.CUSTOMER_EVENT_CATEGORY, {"test_event": "returns"}):
            self.customer.leaders_notify_returns = False
            self._event()
            self.assertFalse(self.Message.search([("event", "=", "test_event")]))
            self.customer.leaders_notify_returns = True
            self._event()
            self.assertEqual(len(self.Message.search([("event", "=", "test_event")])), 1)
            self.Message.search([]).unlink()
            with mock.patch.object(notification_module, "CUSTOMER_MANDATORY_EVENTS", frozenset({"test_event"})):
                self.customer.leaders_notify_returns = False
                self._event()  # a transactional event always goes out
                self.assertEqual(len(self.Message.search([("event", "=", "test_event")])), 1)

    def test_a_customer_company_reaches_the_users_of_the_company(self):
        company = self.env["res.partner"].create({"name": "Push Company", "is_company": True})
        contact = self.env["res.partner"].create({"name": "Push Contact", "parent_id": company.id, "type": "contact"})
        user = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Push Contact", "login": "contact@example.com", "password": "Secret123!x", "partner_id": contact.id,
            "groups_id": [Command.set([self.env.ref("base.group_portal").id])]})
        self.Device._leaders_register(user, TOKEN_A, "android", "ar")
        self.env["leaders.notification"].notify_customer("test_event", company, company)  # the order is on the company
        self.assertEqual(self.Message.search([("event", "=", "test_event")]).user_id, user)

    def test_the_vendors_owner_and_staff_get_it_with_or_without_an_email(self):
        env = self.env
        vendor = env["res.partner"].create({"name": "Push Vendor", "is_marketplace_vendor": True, "vendor_state": "approved"})
        contact = env["res.partner"].create({"name": "Push Owner", "parent_id": vendor.id, "type": "contact"})  # no email
        owner = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Push Owner", "login": "owner-push", "password": "Secret123!x", "partner_id": contact.id,
            "groups_id": [Command.set([env.ref("base.group_portal").id, env.ref("leaders_marketplace.group_vendor_owner").id])]})
        stranger = self.customer_user
        self.Device._leaders_register(owner, TOKEN_A, "android", "ar")
        self.Device._leaders_register(stranger, TOKEN_B, "android", "ar")
        env["leaders.notification"].notify_vendor("test_event", vendor, vendor)
        messages = self.Message.search([("event", "=", "test_event")])
        self.assertEqual(messages.user_id, owner)
        self.assertTrue(messages.title)

    # ---- the sending
    def test_the_cron_sends_marks_and_cleans(self):
        self.Device._leaders_register(self.customer_user, TOKEN_A, "android", "en")
        self.Device._leaders_register(self.customer_user, TOKEN_B, "ios", "en")
        self.Device._leaders_register(self.customer_user, TOKEN_C, "web", "en")
        self._event()
        order = {TOKEN_A: "ok", TOKEN_B: "gone", TOKEN_C: "retry"}
        with mock.patch(APP, return_value=object()), mock.patch(
                DELIVER, side_effect=lambda app, rows: [order[row["token"]] for row in rows]) as deliver:
            self.Message._cron_send()
        sent = {m.token: m for m in self.Message.search([("event", "=", "test_event")])}
        self.assertEqual((sent[TOKEN_A].state, sent[TOKEN_B].state, sent[TOKEN_C].state), ("sent", "failed", "pending"))
        self.assertEqual(sent[TOKEN_C].tries, 1)
        self.assertFalse(self.Device.search([("token", "=", TOKEN_B)]))  # Firebase does not know it: forgotten
        self.assertTrue(self.Device.search([("token", "=", TOKEN_A)]))
        rows = deliver.call_args[0][1]
        self.assertTrue(all(r["title"] == "Hello Push Customer" and r["data"]["event"] == "test_event" for r in rows))
        for _i in range(3):  # Firebase keeps failing: after three tries the message is given up
            with mock.patch(APP, return_value=object()), mock.patch(DELIVER, side_effect=lambda app, rows: ["retry"] * len(rows)):
                self.Message._cron_send()
        self.assertEqual(sent[TOKEN_C].state, "failed")

    def test_without_firebase_the_message_fails_with_a_reason(self):
        self.Device._leaders_register(self.customer_user, TOKEN_A, "android", "en")
        self._event()
        with mock.patch(APP, side_effect=ValueError("bad key")):
            self.Message._cron_send()
        message = self.Message.search([("event", "=", "test_event")])
        self.assertEqual(message.state, "failed")
        self.assertIn("bad key", message.error)

    def test_the_firebase_message_is_a_high_priority_notification(self):
        """What lets a phone show it while the app is closed: a notification block and high priority."""
        from odoo.addons.leaders_marketplace.models import push
        sent = []

        class Reply:
            status_code = 200

        class Session:
            def post(self, url, json=None, headers=None, timeout=None):
                sent.append((url, json, headers))
                return Reply()
        account = {"project_id": "demo-project", "_key": "k"}
        with mock.patch("requests.Session", Session), mock.patch.object(push, "_access_token", return_value="tok"):
            answers = push._deliver(account, [{"token": TOKEN_A, "title": "T", "body": "B", "data": {"n": 5}}])
        url, body, headers = sent[0]
        message = body["message"]
        self.assertEqual((answers, url, headers["Authorization"], body["validate_only"]),
                         (["ok"], "https://fcm.googleapis.com/v1/projects/demo-project/messages:send", "Bearer tok", False))
        self.assertEqual((message["token"], message["notification"], message["data"]),
                         (TOKEN_A, {"title": "T", "body": "B"}, {"n": "5"}))
        self.assertEqual((message["android"]["priority"], message["apns"]["headers"]["apns-priority"]), ("HIGH", "10"))

    def test_how_firebase_answers_is_understood(self):
        from odoo.addons.leaders_marketplace.models import push

        class Reply:
            def __init__(self, status, error=None):
                self.status_code, self._error, self.text = status, error, "x"

            def json(self):
                return {"error": self._error} if self._error is not None else {}
        gone = {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}], "message": "gone"}
        invalid = {"status": "INVALID_ARGUMENT", "message": "The registration token is not a valid FCM registration token"}
        wrong = {"status": "INVALID_ARGUMENT", "message": "Invalid JSON payload"}
        self.assertEqual([push._answer(r) for r in (Reply(200), Reply(404, gone), Reply(400, invalid), Reply(503, {}), Reply(429, {}))],
                         ["ok", "gone", "gone", "retry", "retry"])
        self.assertTrue(push._answer(Reply(400, wrong)).startswith("failed: 400"))
        self.assertTrue(push._answer(Reply(403, {"message": "denied"})).startswith("failed: 403"))

    def test_the_access_token_is_signed_with_the_service_account_and_kept(self):
        from odoo.addons.leaders_marketplace.models import push
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import jwt
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        account = {"project_id": "p", "client_email": "svc@p.iam.gserviceaccount.com", "private_key": pem, "_key": "tk1"}
        calls = []

        class Reply:
            def raise_for_status(self):
                pass

            def json(self):
                return {"access_token": "abc", "expires_in": 3600}

        def post(url, data=None, timeout=None):
            calls.append((url, data))
            return Reply()
        push._TOKENS.clear()
        with mock.patch("requests.post", post):
            self.assertEqual(push._access_token(account), "abc")
            self.assertEqual(push._access_token(account), "abc")  # kept for the hour: one exchange
            self.assertEqual(len(calls), 1)
            self.assertEqual(push._access_token(account, force=True), "abc")
            self.assertEqual(len(calls), 2)
        url, data = calls[0]
        claims = jwt.decode(data["assertion"], key.public_key(), algorithms=["RS256"], audience=url)
        self.assertEqual((claims["iss"], claims["scope"]), ("svc@p.iam.gserviceaccount.com", push.FCM_SCOPE))
        self.assertEqual(data["grant_type"], "urn:ietf:params:oauth:grant-type:jwt-bearer")

    # ---- what has no email behind it
    def test_staff_hear_about_a_new_request_and_the_customer_follows_it(self):
        env = self.env
        staff = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Reviewer", "login": "reviewer-push", "password": "Secret123!x",
            "groups_id": [Command.set([env.ref("base.group_user").id, env.ref("leaders_marketplace.group_marketplace_reviewer").id])]})
        self.Device._leaders_register(staff, TOKEN_A, "android", "ar")
        self.Device._leaders_register(self.customer_user, TOKEN_B, "android", "en")
        request = env["leaders.customer.request"].create({
            "kind": "purchase", "partner_id": self.customer.id, "phone": "+201005550123", "product_tmpl_id": self.eq.id,
            "title": "Api Generator", "unit_price": 1000})
        mine = self.Message.search([("event", "=", "staff_new_request")])
        self.assertEqual(mine.user_id, staff)
        self.assertIn(request.name, mine.title)
        self.assertEqual(json.loads(mine.data)["reference"], request.name)
        request.state = "inProgress"
        follow = self.Message.search([("event", "=", "customer_request_inProgress")])
        self.assertEqual((follow.user_id, follow.title), (self.customer_user, "Your request is confirmed"))
        request.state = "executing"
        self.assertEqual(len(self.Message.search([("event", "=", "customer_request_executing")])), 1)
        self.assertFalse(self.Message.search([("event", "=", "customer_request_completed")]))
        # the staff setting off: nothing for the back office
        self.env["ir.config_parameter"].sudo().set_param("leaders_marketplace.push_staff", "False")
        self.Message.search([]).unlink()
        env["leaders.customer.request"].create({"kind": "item", "partner_id": self.customer.id, "title": "A part"})
        self.assertFalse(self.Message.search([("event", "=", "staff_new_request")]))

    def test_a_new_equipment_request_reaches_the_vendors_phone(self):
        env = self.env
        vendor = env["res.partner"].create({"name": "Push EqVendor", "is_marketplace_vendor": True,
                                            "vendor_state": "approved", "leaders_contract_model": "subscription"})
        vendor._marketplace_ensure_vendor_stock()
        contact = env["res.partner"].create({"name": "EqVendor Owner", "parent_id": vendor.id, "type": "contact"})
        owner = env["res.users"].with_context(no_reset_password=True).create({
            "name": "EqVendor Owner", "login": "eq-owner-push", "password": "Secret123!x", "partner_id": contact.id,
            "groups_id": [Command.set([env.ref("base.group_portal").id, env.ref("leaders_marketplace.group_vendor_owner").id])]})
        self.Device._leaders_register(owner, TOKEN_A, "ios", "ar")
        self.eq.marketplace_owner_vendor_id = vendor
        request = env["leaders.customer.request"].create({
            "kind": "purchase", "partner_id": self.customer.id, "phone": "+201005550123", "product_tmpl_id": self.eq.id,
            "title": "Api Generator", "unit_price": 1000})
        message = self.Message.search([("event", "=", "vendor_equipment_request")])
        self.assertEqual((message.user_id, request.name in message.title), (owner, True))

    def test_the_test_button_sends_to_my_devices(self):
        self.Device._leaders_register(self.env.user, TOKEN_A, "android", "en")
        settings = self.env["res.config.settings"].create({})
        with mock.patch(APP, return_value=object()), mock.patch(DELIVER, return_value=["ok"]):
            action = settings.action_push_test()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(self.Message.search([("event", "=", "test")]).state, "sent")

    def test_the_back_office_hears_about_an_escalated_dispute_a_change_and_a_subscription(self):
        env = self.env
        staff = env["res.users"].with_context(no_reset_password=True).create({
            "name": "Manager Push", "login": "manager-push", "password": "Secret123!x",
            "groups_id": [Command.set([env.ref("base.group_user").id, env.ref("leaders_marketplace.group_marketplace_manager").id])]})
        self.Device._leaders_register(staff, TOKEN_A, "android", "ar")
        vendor = env["res.partner"].create({"name": "Push SubVendor", "is_marketplace_vendor": True, "vendor_state": "approved"})
        plan = env["leaders.subscription.plan"].search([], limit=1)
        order = env["leaders.subscription.order"].create({"vendor_id": vendor.id, "plan_id": plan.id})
        message = self.Message.search([("event", "=", "staff_subscription")])
        self.assertEqual((message.user_id, "Push SubVendor" in message.body), (staff, True))
        self.assertEqual(message.title, "اشتراك ينتظر الدفع")  # the device speaks Arabic
        helpers = env["leaders.push.helpers"]
        helpers._lp_staff(order, "staff_dispute_escalated", lambda r: ("D", "d"), {"id": "1"})
        self.assertEqual(len(self.Message.search([("event", "=", "staff_dispute_escalated")])), 1)
