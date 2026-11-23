"""The bell's notification list: entries written with every event, read through /account/inbox."""
import json

from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import HttpCase

TOKEN = "fcm-token-inbox-" + "i" * 40


@tagged("post_install", "-at_install")
class TestInbox(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        params = env["ir.config_parameter"].sudo()
        params.set_param("leaders_marketplace.auth_cookie_secure", "False")
        params.set_param("leaders_marketplace.notify_test_event", "True")
        model = env["ir.model"]._get("res.partner")
        cls.template = env["mail.template"].create({
            "name": "Test event", "model_id": model.id, "subject": "Hello {{ object.name }}",
            "body_html": "<p>First line of the mail</p><p>Second line</p>"})
        env["ir.model.data"].create({"module": "leaders_marketplace", "name": "mail_template_test_event",
                                     "model": "mail.template", "res_id": cls.template.id})

        def person(name):
            partner = env["res.partner"].create({"name": name, "email": "%s@example.com" % name.lower()})
            user = env["res.users"].with_context(no_reset_password=True).create({
                "name": name, "login": "%s@example.com" % name.lower(), "password": "Secret123!x",
                "partner_id": partner.id, "groups_id": [Command.set([env.ref("base.group_portal").id])]})
            return partner, user
        cls.partner, cls.user = person("Inbox")
        cls.other_partner, cls.other = person("Other")
        env.flush_all()

    def call(self, method, path, user=None, lang="ar"):
        from odoo.addons.leaders_marketplace.tools.auth_tokens import encode_access_token
        headers = {"Accept-Language": lang}
        if user is not False:
            headers["Authorization"] = "Bearer %s" % encode_access_token(self.env, user or self.user)
        r = self.opener.request(method, self.base_url() + path, headers=headers, timeout=30)
        return r, (r.json() if r.content and "json" in r.headers.get("Content-Type", "") else None)

    def event(self, partner=None):
        partner = partner or self.partner
        return self.env["leaders.notification"].notify_customer("test_event", partner, partner)

    def entry(self, title="T", **vals):
        return self.env["leaders.inbox.entry"].create(dict({"user_id": self.user.id, "event": "customer_order_shipped",
                                                           "title": title, "body": "B"}, **vals))

    # ------------------------------------------------------------------ writing
    def test_an_event_is_written_for_the_person_with_or_without_a_device(self):
        self.event()
        entries = self.env["leaders.inbox.entry"].search([("user_id", "=", self.user.id)])
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries.event, entries.title, entries.body, entries.read),
                         ("test_event", "Hello Inbox", "First line of the mail", False))
        self.assertEqual((entries.res_model, entries.res_id), ("res.partner", self.partner.id))
        self.assertFalse(self.env["leaders.inbox.entry"].search([("user_id", "=", self.other.id)]))  # nobody else's

    def test_the_push_says_which_entry_it_is(self):
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("leaders_marketplace.push_enabled", "True")
        params.set_param("leaders_marketplace.push_firebase_credentials_json", '{"type": "service_account"}')
        self.env["leaders.push.device"]._leaders_register(self.user, TOKEN, "android", "en")
        self.event()
        entry = self.env["leaders.inbox.entry"].search([("user_id", "=", self.user.id)])
        message = self.env["leaders.push.message"].search([("token", "=", TOKEN)])
        self.assertEqual(json.loads(message.data)["inboxId"], str(entry.id))

    def test_old_entries_are_forgotten_after_ninety_days(self):
        old, fresh = self.entry("old"), self.entry("fresh")
        self.env.cr.execute("UPDATE leaders_inbox_entry SET create_date = now() - interval '91 days' WHERE id = %s", [old.id])
        self.env["leaders.inbox.entry"]._purge()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    # ------------------------------------------------------------------ reading
    def test_the_list_is_this_persons_newest_first_in_the_requests_language(self):
        first = self.entry("First", title_ar="الأول", body_ar="نص", reference="SF-1042")
        second = self.entry("Second", event="customer_request_offered")
        self.env["leaders.inbox.entry"].create({"user_id": self.other.id, "event": "x", "title": "Not mine"})
        r, body = self.call("GET", "/account/inbox", lang="ar")
        self.assertEqual(r.status_code, 200, body)
        self.assertEqual([i["id"] for i in body["items"]], [str(second.id), str(first.id)])
        self.assertEqual((body["totalCount"], body["page"], body["pageSize"], body["unreadCount"]), (2, 1, 10, 2))
        arabic = body["items"][1]
        self.assertEqual((arabic["title"], arabic["body"], arabic["reference"], arabic["read"], arabic["event"]),
                         ("الأول", "نص", "SF-1042", False, "customer_order_shipped"))
        self.assertRegex(arabic["createdAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(body["items"][0]["title"], "Second")  # no Arabic text: the English one
        self.assertNotIn("reference", body["items"][0])  # the key is left out when the event has none
        _r, english = self.call("GET", "/account/inbox", lang="en")
        self.assertEqual(english["items"][1]["title"], "First")

    def test_paging_and_the_unread_count_across_pages(self):
        for i in range(12):
            self.entry("E%02d" % i)
        r, page = self.call("GET", "/account/inbox?page=2&pageSize=5")
        self.assertEqual((r.status_code, len(page["items"]), page["totalCount"], page["page"], page["pageSize"],
                          page["unreadCount"]), (200, 5, 12, 2, 5, 12))
        self.assertEqual(self.call("GET", "/account/inbox?pageSize=51")[0].status_code, 422)
        self.assertEqual(self.call("GET", "/account/inbox?page=0")[0].status_code, 422)
        r, empty = self.call("GET", "/account/inbox", user=self.other)
        self.assertEqual((r.status_code, empty["items"], empty["totalCount"], empty["unreadCount"]), (200, [], 0, 0))

    def test_marking_read_is_safe_to_repeat_and_only_for_the_owner(self):
        a, b = self.entry("A"), self.entry("B")
        self.assertEqual(self.call("POST", "/account/inbox/%s/read" % a.id)[0].status_code, 204)
        self.assertEqual(self.call("POST", "/account/inbox/%s/read" % a.id)[0].status_code, 204)  # again
        self.assertEqual((a.read, b.read), (True, False))
        _r, body = self.call("GET", "/account/inbox")
        self.assertEqual(body["unreadCount"], 1)
        r, err = self.call("POST", "/account/inbox/%s/read" % b.id, user=self.other)
        self.assertEqual((r.status_code, err["code"], b.read), (404, "not_found", False))
        self.assertEqual(self.call("POST", "/account/inbox/999999/read")[0].status_code, 404)
        self.assertEqual(self.call("POST", "/account/inbox/read-all")[0].status_code, 204)
        self.assertTrue(b.read)

    def test_it_needs_a_signed_in_person_and_answers_the_preflight(self):
        r, err = self.call("GET", "/account/inbox", user=False)
        self.assertEqual((r.status_code, err["code"]), (401, "session_expired"))
        self.assertEqual(self.call("POST", "/account/inbox/read-all", user=False)[0].status_code, 401)
        for path in ("/account/inbox", "/account/inbox/1/read", "/account/inbox/read-all"):
            r = self.opener.request("OPTIONS", self.base_url() + path, timeout=30,
                                    headers={"Origin": "https://spark-fu.com", "Access-Control-Request-Method": "GET"})
            self.assertEqual(r.status_code, 204, path)
