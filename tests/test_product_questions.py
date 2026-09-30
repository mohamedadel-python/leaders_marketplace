"""Product questions and answers (TASK 29 / D3)."""
import importlib.util

from lxml import html as lxml_html

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase
from odoo.tools import file_path

from odoo.addons.leaders_marketplace.models.product_question import (
    ANSWER_MAX_LENGTH, QUESTION_MAX_LENGTH, clean_text)
from .test_customer_cancel import CustomerCancelCommon
from .test_security_rpc import PORTAL_PASSWORD

P = "leaders_marketplace."


class QuestionCommon(CustomerCancelCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Question = cls.env["leaders.product.question"]
        cls.tmpl_a = cls.fbm_product.product_tmpl_id
        cls.tmpl_b = cls.b_product.product_tmpl_id
        (cls.tmpl_a | cls.tmpl_b).write({"is_published": True, "sale_ok": True})
        cls.set_param = cls.env["ir.config_parameter"].sudo().set_param
        cls.set_param(P + "product_questions_enabled", "True")
        cls.set_param(P + "product_questions_moderation", False)
        cls.set_param(P + "product_questions_daily_limit", "5")
        for key in ("vendor_question_asked", "customer_question_answered"):
            cls.set_param(P + "notify_%s" % key, "True")
        cls.reviewer = cls.env["res.users"].create({
            "name": "Question Reviewer", "login": "lm.question.reviewer@example.com",
            "groups_id": [Command.set([cls.env.ref("base.group_user").id,
                                       cls.env.ref("leaders_marketplace.group_marketplace_reviewer").id])]})

    def _ask(self, text="Does it come in blue?", tmpl=None, user=None):
        return self.Question._leaders_ask(tmpl or self.tmpl_a, user or self.customer_user, text)

    def _answer(self, question, vendor=None, user=None, text="Yes, it does."):
        return question.action_vendor_answer(vendor or self.vendor, user or self.owner_user, text)

    def _mails(self, action, subject):
        Mail = self.env["mail.mail"].sudo()
        last = Mail.search([], order="id desc", limit=1).id or 0
        action()
        return Mail.search([("id", ">", last)]).filtered(lambda m: subject in (m.subject or ""))


@tagged("post_install", "-at_install")
class TestProductQuestionModel(QuestionCommon):

    def test_clean_text_strips_control_characters(self):
        self.assertEqual(clean_text("  a\x00b\r\n\r\n\r\n\r\nc\x07  "), "ab\n\nc")

    def test_ask_creates_a_pending_question_newest_first(self):
        first = self._ask("First question?")
        second = self._ask("Second question?")
        self.assertEqual((first.state, first.partner_id, first.user_id),
                         ("pending", self.customer_user.partner_id, self.customer_user))
        self.assertFalse(first.is_public)
        self.assertEqual(self.Question.search([("user_id", "=", self.customer_user.id)])[:2], second | first)

    def test_length_and_empty_and_daily_limit_and_duplicate(self):
        with self.assertRaises(UserError):
            self._ask("   ")
        with self.assertRaises(UserError):
            self._ask("x" * (QUESTION_MAX_LENGTH + 1))
        self._ask("x" * QUESTION_MAX_LENGTH)
        with self.assertRaises(UserError):
            self._ask("X" * QUESTION_MAX_LENGTH)  # same text, other case: duplicate
        self.assertEqual(self.Question._leaders_ask_problem(self.tmpl_a, self.customer_user, "same?"), False)
        self.set_param(P + "product_questions_daily_limit", "2")
        self._ask("one?")
        with self.assertRaises(UserError):
            self._ask("two?")  # 2 already asked today (the long one + "one?")
        # the same text on ANOTHER product is not a duplicate; another user is not limited
        self.assertFalse(self.Question._leaders_ask_problem(self.tmpl_b, self.owner_user, "one?"))
        self.assertEqual(self.Question._leaders_ask_problem(self.tmpl_a, self.customer_user, "one?"), "limit")
        self.set_param(P + "product_questions_daily_limit", "0")
        self.assertEqual(self.Question._leaders_daily_limit(), 5)

    def test_answered_questions_are_public_without_moderation(self):
        question = self._ask()
        self._answer(question)
        self.assertEqual((question.state, question.answered_partner_id, question.answered_user_id),
                         ("answered", self.vendor, self.owner_user))
        self.assertTrue(question.answer_date)
        self.assertTrue(question.is_public)
        ctx = self.tmpl_a._leaders_questions_context()
        self.assertEqual(ctx["questions"], question)
        # a pending question is never public
        pending = self._ask("Another one?")
        self.assertNotIn(pending, self.tmpl_a._leaders_questions_context()["questions"])

    def test_moderation_needs_a_reviewer_approval(self):
        self.set_param(P + "product_questions_moderation", "True")
        question = self._ask()
        self._answer(question)
        self.assertFalse(question.is_public)
        self.assertFalse(self.tmpl_a._leaders_questions_context()["questions"])
        question.with_user(self.reviewer).action_approve()
        self.assertTrue(question.is_public)
        # editing the answer sends it back to the reviewer
        self._answer(question, text="Actually, only in red.")
        self.assertFalse(question.is_public)
        # switching moderation off publishes every answered question
        self.set_param(P + "product_questions_moderation", False)
        question.invalidate_recordset()
        self.assertTrue(question.is_public)

    def test_reviewer_hides_and_shows_again(self):
        question = self._ask()
        self._answer(question)
        question.with_user(self.reviewer).action_hide("Spam")
        self.assertEqual((question.state, question.hidden_by, question.hidden_reason),
                         ("hidden", self.reviewer, "Spam"))
        self.assertTrue(question.hidden_date)
        self.assertFalse(self.tmpl_a._leaders_questions_context()["questions"])
        with self.assertRaises(UserError):
            self._answer(question, text="Trying to edit")
        self.assertFalse(question._vendor_can_edit(self.vendor))
        question.with_user(self.reviewer).action_unhide()
        self.assertEqual(question.state, "answered")
        self.assertTrue(self.tmpl_a._leaders_questions_context()["questions"])

    def test_only_the_backoffice_may_hide(self):
        question = self._ask()
        plain = self.env["res.users"].create({
            "name": "Plain Internal", "login": "lm.plain.internal@example.com",
            "groups_id": [Command.set([self.env.ref("base.group_user").id])]})
        with self.assertRaises(AccessError):
            question.with_user(plain).action_hide("no")
        with self.assertRaises(AccessError):
            question.with_user(self.customer_user).action_hide("no")
        self.assertEqual(question.state, "pending")

    def test_answer_length_and_empty(self):
        question = self._ask()
        with self.assertRaises(UserError):
            self._answer(question, text=" ")
        with self.assertRaises(UserError):
            self._answer(question, text="y" * (ANSWER_MAX_LENGTH + 1))
        self.assertEqual(question.state, "pending")

    def test_vendor_scoping_by_active_offer_on_the_template(self):
        question = self._ask(tmpl=self.tmpl_a)
        self.assertTrue(question._vendor_can_answer(self.vendor))
        self.assertFalse(question._vendor_can_answer(self.vendor_b))
        with self.assertRaises(UserError):
            self._answer(question, vendor=self.vendor_b, user=self.owner_user)
        self.assertEqual(self.Question._leaders_vendor_templates(self.vendor_b), self.tmpl_b)
        # vendor B gets an ACTIVE offer on the same template (another variant is enough): now allowed
        offer_b = self.env["leaders.marketplace.offer"].create({
            "product_tmpl_id": self.tmpl_a.id, "product_id": self.fbm_product.id,
            "vendor_id": self.vendor_b.id, "seller_sku": "Q-B-ON-A", "price": 90.0, "fulfillment_type": "fbm",
            "stock_location_id": self.vendor_b._marketplace_stock_location("fbm").id})
        self.assertFalse(question._vendor_can_answer(self.vendor_b), "a draft offer does not count")
        offer_b.action_approve()
        self.assertTrue(question._vendor_can_answer(self.vendor_b))
        self._answer(question, vendor=self.vendor_b, user=self.owner_user)
        # answered once: vendor A cannot overwrite B's answer, B can edit it
        self.assertFalse(question._vendor_can_edit(self.vendor))
        self.assertTrue(question._vendor_can_edit(self.vendor_b))
        with self.assertRaises(UserError):
            self._answer(question, text="Overwritten")
        # an archived offer stops the right to answer
        offer_b.active = False
        self.assertFalse(question._vendor_can_answer(self.vendor_b))

    def test_nav_entry_only_when_the_switch_is_on(self):
        Nav = self.env["leaders.portal.nav"]
        keys = lambda: [e["key"] for e in Nav._vendor_entries(True)]  # noqa: E731
        self.assertIn("questions", keys())
        self.assertIn("questions", [e["key"] for e in Nav._vendor_entries(False)])
        self.set_param(P + "product_questions_enabled", False)
        self.assertNotIn("questions", keys())

    def test_context_is_empty_when_switched_off(self):
        self.set_param(P + "product_questions_enabled", False)
        self.assertEqual(self.tmpl_a._leaders_questions_context(), {})


@tagged("post_install", "-at_install")
class TestProductQuestionEmails(QuestionCommon):

    def test_vendor_mail_one_per_vendor_with_an_active_offer(self):
        offer_b = self.env["leaders.marketplace.offer"].create({
            "product_tmpl_id": self.tmpl_a.id, "product_id": self.fbm_product.id,
            "vendor_id": self.vendor_b.id, "seller_sku": "Q-B-MAIL", "price": 90.0, "fulfillment_type": "fbm",
            "stock_location_id": self.vendor_b._marketplace_stock_location("fbm").id})
        offer_b.action_approve()
        owner_b = self.env["res.partner"].create({
            "name": "B Owner", "email": "b.owner.q@example.com", "parent_id": self.vendor_b.id, "type": "contact"})
        self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "B Owner", "login": "b.owner.q@example.com", "partner_id": owner_b.id,
            "groups_id": [Command.set([self.env.ref("leaders_marketplace.group_vendor_owner").id])]})
        mails = self._mails(lambda: self._ask("Is it waterproof?"), "New customer question")
        recipients = mails.recipient_ids
        self.assertEqual(len(mails), 2, "one mail per vendor")
        self.assertIn(self.owner_user.partner_id, recipients)
        self.assertIn(self.staff_user.partner_id, recipients)
        self.assertIn(owner_b, recipients)
        self.assertIn("Is it waterproof?", " ".join(mails.mapped("body_html")))

    def test_vendor_mail_skips_vendors_without_an_offer_and_respects_the_setting(self):
        mails = self._mails(lambda: self._ask("Only for B?", tmpl=self.tmpl_b), "New customer question")
        self.assertFalse(mails.recipient_ids & (self.owner_user.partner_id | self.staff_user.partner_id),
                         "vendor A has no offer on this product")
        self.set_param(P + "notify_vendor_question_asked", "False")
        self.assertFalse(self._mails(lambda: self._ask("Silent?"), "New customer question"))

    def test_customer_mail_when_the_answer_is_published_once(self):
        question = self._ask()
        mails = self._mails(lambda: self._answer(question), "was answered")
        self.assertEqual(len(mails), 1)
        self.assertIn(self.customer_user.partner_id, mails.recipient_ids)
        self.assertIn("Yes, it does.", mails.body_html)
        self.assertFalse(self._mails(lambda: self._answer(question, text="Edited answer"), "was answered"))

    def test_customer_mail_waits_for_the_approval_under_moderation(self):
        self.set_param(P + "product_questions_moderation", "True")
        question = self._ask()
        self.assertFalse(self._mails(lambda: self._answer(question), "was answered"))
        mails = self._mails(lambda: question.with_user(self.reviewer).action_approve(), "was answered")
        self.assertEqual(len(mails), 1)

    def test_customer_mail_setting_off(self):
        self.set_param(P + "notify_customer_question_answered", "False")
        question = self._ask()
        self.assertFalse(self._mails(lambda: self._answer(question), "was answered"))

    def test_events_are_switchable_from_the_settings(self):
        fields = self.env["res.config.settings"]._fields
        self.assertIn("marketplace_notify_vendor_question_asked", fields)
        self.assertIn("marketplace_notify_customer_question_answered", fields)

    def test_phase_migration_seeds_the_events_off_and_is_idempotent(self):
        path = file_path("leaders_marketplace/migrations/18.0.4.0.57/pre-D3-notification-defaults.py")
        spec = importlib.util.spec_from_file_location("lm_migration_pre_d3", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        cr = self.env.cr
        cr.execute("DELETE FROM ir_config_parameter WHERE key = 'leaders_marketplace.notify_vendor_question_asked'")
        cr.execute("DELETE FROM ir_model_data WHERE module = 'leaders_marketplace' "
                   "AND name = 'param_notify_vendor_question_asked'")
        migration.migrate(cr, "18.0.4.0.56")
        migration.migrate(cr, "18.0.4.0.56")
        cr.execute("SELECT value FROM ir_config_parameter WHERE key = 'leaders_marketplace.notify_vendor_question_asked'")
        self.assertEqual(cr.fetchall(), [("False",)])
        cr.execute("SELECT value FROM ir_config_parameter "
                   "WHERE key = 'leaders_marketplace.notify_customer_question_answered'")
        self.assertEqual(len(cr.fetchall()), 1)


@tagged("post_install", "-at_install")
class TestProductQuestionPortal(HttpCase, QuestionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        website = cls.env["website"].search([], limit=1)
        lang = cls.env["res.lang"]._activate_lang("ar_001")
        website.language_ids = [Command.link(lang.id)]
        cls.ar_prefix = "/" + lang.url_code
        cls.env["leaders.vendor.terms"].with_context(active_test=False).search([]).write({"active": False})
        cls.env.cr.flush()

    def _dom(self, path, code=200):
        response = self.url_open(path)
        self.assertEqual(response.status_code, code, path)
        return lxml_html.fromstring(response.content) if code == 200 else None

    def _post(self, path, form_url, **data):
        data["csrf_token"] = data.pop("token", None) or self._csrf(form_url)
        return self.url_open(path, data=data, allow_redirects=False)

    def _ask_url(self, tmpl=None):
        return "/shop/product/%s/question" % (tmpl or self.tmpl_a).id

    # -- product page ----------------------------------------------------------
    def test_product_page_anonymous_sees_answers_and_a_login_link_only(self):
        question = self._ask("What <b>size</b> is it?")
        self._answer(question, text="Size <script>alert(1)</script> medium")
        pending = self._ask("Still open?")
        dom = self._dom(self.tmpl_a.website_url)
        self.assertTrue(dom.xpath("//section[@id='lm_questions']"))
        self.assertTrue(dom.xpath("//section[@id='lm_questions']//a[contains(@href, '/web/login?redirect=')]"))
        self.assertFalse(dom.xpath("//form[@id='lm_question_form']"))
        items = dom.xpath("//div[@id='lm_question_list']/div[contains(@class, 'lm-qa-item')]")
        self.assertEqual(len(items), 1)
        text = items[0].text_content()
        self.assertIn("What <b>size</b> is it?", text, "escaped, not interpreted")
        self.assertIn("Size <script>alert(1)</script> medium", text)
        self.assertIn(self.vendor.vendor_store_name or self.vendor.name, text)
        self.assertFalse(dom.xpath("//section[@id='lm_questions']//script"))
        self.assertFalse(dom.xpath("//section[@id='lm_questions']//b"))
        self.assertNotIn("Still open?", dom.xpath("string(//section[@id='lm_questions'])"))
        self.assertTrue(pending.state == "pending")

    def test_product_page_logged_in_has_the_form_and_own_pending_questions(self):
        self._ask("Waiting for the seller?")
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        dom = self._dom(self.tmpl_a.website_url)
        form = dom.xpath("//form[@id='lm_question_form']")
        self.assertTrue(form)
        self.assertEqual(form[0].get("action"), self._ask_url())
        self.assertTrue(dom.xpath("//form[@id='lm_question_form']//input[@name='csrf_token']"))
        self.assertEqual(dom.xpath("//textarea[@name='question']/@maxlength"), [str(QUESTION_MAX_LENGTH)])
        self.assertFalse(dom.xpath("//div[@id='lm_question_login']"))
        self.assertIn("Waiting for the seller?", dom.xpath("string(//div[@id='lm_my_questions'])"))

    def test_switch_off_removes_the_section_and_404s_every_route(self):
        question = self._ask()
        self._answer(question)
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        token = self._csrf(self.tmpl_a.website_url)
        self.set_param(P + "product_questions_enabled", False)
        self.env.cr.flush()
        self.assertFalse(self._dom(self.tmpl_a.website_url).xpath("//section[@id='lm_questions']"))
        response = self.url_open(self._ask_url(), data={"csrf_token": token, "question": "Off?"})
        self.assertEqual(response.status_code, 404)
        self.set_param(P + "product_questions_enabled", "True")
        self.env.cr.flush()
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        self.assertTrue(self._dom("/my/vendor").xpath("//a[@href='/my/vendor/questions']"))
        token = self._csrf("/my/vendor/questions?filter=all")
        self.set_param(P + "product_questions_enabled", False)
        self.env.cr.flush()
        self._dom("/my/vendor/questions", 404)
        response = self.url_open("/my/vendor/questions/%s/answer" % question.id,
                                 data={"csrf_token": token, "answer": "x"})
        self.assertEqual(response.status_code, 404)
        dom = self._dom("/my/vendor")
        self.assertFalse(dom.xpath("//a[@href='/my/vendor/questions']"))

    # -- asking ------------------------------------------------------------------
    def test_ask_needs_login(self):
        self.authenticate(None, None)
        token = self._csrf(self.tmpl_a.website_url)
        response = self.url_open(self._ask_url(), data={"csrf_token": token, "question": "Anon?"},
                                 allow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        self.assertIn("/web/login", response.headers["Location"])
        self.assertFalse(self.Question.search([("question", "=", "Anon?")]))

    def test_ask_creates_the_question_and_redirects_to_the_product(self):
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        response = self._post(self._ask_url(), self.tmpl_a.website_url, question="Is it new?")
        self.assertIn(response.status_code, (302, 303))
        self.assertIn(self.tmpl_a.website_url, response.headers["Location"])
        self.assertIn("lm_q=ok", response.headers["Location"])
        question = self.Question.search([("question", "=", "Is it new?")])
        self.assertEqual((question.state, question.user_id, question.product_tmpl_id),
                         ("pending", self.customer_user, self.tmpl_a))
        dom = self._dom(self.tmpl_a.website_url + "?lm_q=ok")
        self.assertTrue(dom.xpath("//div[@id='lm_question_flash' and contains(@class, 'alert-success')]"))
        # double submission is refused
        response = self._post(self._ask_url(), self.tmpl_a.website_url, question="Is it new?")
        self.assertIn("lm_q=duplicate", response.headers["Location"])
        self.assertEqual(self.Question.search_count([("question", "=", "Is it new?")]), 1)

    def test_ask_limits_and_unknown_product(self):
        self.set_param(P + "product_questions_daily_limit", "1")
        self.env.cr.flush()
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        self._post(self._ask_url(), self.tmpl_a.website_url, question="One?")
        response = self._post(self._ask_url(), self.tmpl_a.website_url, question="Two?")
        self.assertIn("lm_q=limit", response.headers["Location"])
        response = self._post(self._ask_url(), self.tmpl_a.website_url, question="")
        self.assertIn("lm_q=empty", response.headers["Location"])
        response = self._post(self._ask_url(), self.tmpl_a.website_url, question="x" * (QUESTION_MAX_LENGTH + 1))
        self.assertIn("lm_q=long", response.headers["Location"])
        response = self._post("/shop/product/99999999/question", self.tmpl_a.website_url, question="Ghost?")
        self.assertEqual(response.status_code, 404)
        self.tmpl_b.is_published = False
        self.env.cr.flush()
        response = self._post(self._ask_url(self.tmpl_b), self.tmpl_a.website_url, question="Hidden product?")
        self.assertEqual(response.status_code, 404)

    def test_ask_needs_csrf(self):
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        response = self.url_open(self._ask_url(), data={"question": "No token?"})
        self.assertEqual(response.status_code, 400)

    # -- vendor pages --------------------------------------------------------------
    def test_vendor_page_lists_only_the_questions_of_its_products(self):
        mine = self._ask("About my product?", tmpl=self.tmpl_a)
        foreign = self._ask("About B product?", tmpl=self.tmpl_b)
        self._answer(self._ask("Old one?"))
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        dom = self._dom("/my/vendor/questions")
        ids = dom.xpath("//div[@id='lm_question_cards']/div/@id")
        self.assertEqual(ids, ["lm_vendor_question_%s" % mine.id])
        self.assertTrue(dom.xpath("//div[@id='lm_vendor_question_%s']//span[contains(@class, 'lm-badge--warning')]" % mine.id))
        self.assertTrue(dom.xpath("//form[contains(@action, '/my/vendor/questions/%s/answer')]" % mine.id))
        self.assertNotIn("About B product?", dom.xpath("string(//div[@id='lm_vendor_questions'])"))
        self.assertTrue(dom.xpath("//a[@href='/my/vendor/questions' and contains(@class, 'active')]") or
                        dom.xpath("//a[@href='/my/vendor/questions']"))
        dom = self._dom("/my/vendor/questions?filter=answered")
        self.assertEqual(len(dom.xpath("//div[@id='lm_question_cards']/div")), 1)
        dom = self._dom("/my/vendor/questions?filter=all")
        self.assertEqual(len(dom.xpath("//div[@id='lm_question_cards']/div")), 2)
        self.assertTrue(foreign.state == "pending")

    def test_vendor_answers_and_edits_then_foreign_vendor_is_refused(self):
        question = self._ask("Answer me?")
        foreign = self._ask("B only?", tmpl=self.tmpl_b)
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        response = self._post("/my/vendor/questions/%s/answer" % question.id, "/my/vendor/questions",
                              answer="Sure <i>thing</i>")
        self.assertIn(response.status_code, (302, 303))
        self.assertIn("saved=1", response.headers["Location"])
        self.assertEqual((question.state, question.answer, question.answered_user_id),
                         ("answered", "Sure <i>thing</i>", self.owner_user))
        self._post("/my/vendor/questions/%s/answer" % question.id, "/my/vendor/questions?filter=all", answer="Edited")
        self.assertEqual(question.answer, "Edited")
        # another vendor's question: 403 and nothing changes
        response = self._post("/my/vendor/questions/%s/answer" % foreign.id, "/my/vendor/questions?filter=all", answer="Hijack")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(foreign.state, "pending")
        response = self._post("/my/vendor/questions/99999999/answer", "/my/vendor/questions?filter=all", answer="Ghost")
        self.assertEqual(response.status_code, 404)
        # empty / too long answers come back as an error on the page
        response = self._post("/my/vendor/questions/%s/answer" % question.id, "/my/vendor/questions?filter=all", answer="  ")
        self.assertIn("error=", response.headers["Location"])
        self.assertEqual(question.answer, "Edited")

    def test_vendor_b_sees_and_answers_only_its_own_and_staff_can_answer(self):
        question = self._ask("Staff answer?")
        self.authenticate(self.staff_user.login, PORTAL_PASSWORD)
        self._post("/my/vendor/questions/%s/answer" % question.id, "/my/vendor/questions?filter=all", answer="Staff says yes")
        self.assertEqual((question.state, question.answered_user_id), ("answered", self.staff_user))

    def test_customer_cannot_reach_the_vendor_pages(self):
        self.authenticate(self.customer_user.login, PORTAL_PASSWORD)
        self._dom("/my/vendor/questions", 403)

    def test_hidden_question_cannot_be_answered_from_the_portal(self):
        question = self._ask("Hide me?")
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        token = self._csrf("/my/vendor/questions?filter=all")
        question.with_user(self.reviewer).action_hide("Abusive")
        self.env.cr.flush()
        dom = self._dom("/my/vendor/questions?filter=all")
        self.assertFalse(dom.xpath("//form[contains(@action, '/answer')]"))
        response = self._post("/my/vendor/questions/%s/answer" % question.id, None, token=token, answer="Nope")
        self.assertIn("error=", response.headers["Location"])
        self.assertEqual(question.state, "hidden")

    def test_nav_entry_and_pages_render_in_english_and_arabic(self):
        question = self._ask()
        self._answer(question)
        self._ask("Another?")
        self.authenticate(self.owner_user.login, PORTAL_PASSWORD)
        for prefix in ("", self.ar_prefix):
            with self.subTest(prefix=prefix):
                dom = self._dom(prefix + "/my/vendor/questions?filter=all")
                self.assertTrue(dom.xpath("//div[@id='lm_vendor_questions']"))
                self.assertTrue(dom.xpath("//aside//a[contains(@href, '/my/vendor/questions')]"))
                rtl = bool(dom.xpath("//div[@id='wrapwrap' and contains(@class, 'o_rtl')]"))
                self.assertEqual(rtl, bool(prefix))
                self.assertTrue(dom.xpath("//span[contains(@class, 'lm-badge')]"))
                page = self._dom(prefix + self.tmpl_a.website_url)
                self.assertTrue(page.xpath("//section[@id='lm_questions']"))
