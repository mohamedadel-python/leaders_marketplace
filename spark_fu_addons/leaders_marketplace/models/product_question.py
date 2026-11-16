import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

PARAM_ENABLED = "leaders_marketplace.product_questions_enabled"
PARAM_MODERATION = "leaders_marketplace.product_questions_moderation"
PARAM_DAILY_LIMIT = "leaders_marketplace.product_questions_daily_limit"
DEFAULT_DAILY_LIMIT = 5
QUESTION_MAX_LENGTH = 500
ANSWER_MAX_LENGTH = 1000
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_text(value):
    """Plain text of a user input: no control characters, trimmed, at most one blank line.

    The text is never trusted as markup: every template prints it with t-esc.
    """
    value = _CONTROL_CHARS.sub("", (value or "").replace("\r\n", "\n").replace("\r", "\n"))
    return _BLANK_LINES.sub("\n\n", value).strip()


class ProductQuestion(models.Model):
    """A customer's question about a product, answered by a seller of that product (TASK 29 / D3).

    Behind the master switch ``product_questions_enabled`` (default off). Lifecycle:
    ``pending`` (not answered) -> ``answered`` (a vendor with an active offer on the
    product template answered) -> ``hidden`` (a reviewer took it off, reversible).
    A question is public on the product page only when it is answered; with the
    moderation setting on it also needs a reviewer's approval. The vendor may edit
    their answer until a reviewer hides the question.
    """
    _name = "leaders.product.question"
    _description = "Product Question"
    _inherit = ["leaders.backoffice.mixin"]
    _order = "create_date desc, id desc"

    product_tmpl_id = fields.Many2one("product.template", required=True, ondelete="cascade", index=True, string="Product")
    product_id = fields.Many2one("product.product", ondelete="set null", string="Variant")
    partner_id = fields.Many2one("res.partner", string="Asked by", required=True, index=True)
    user_id = fields.Many2one("res.users", string="Asking User", required=True, index=True)
    question = fields.Text(required=True)
    state = fields.Selection([
        ("pending", "Pending"), ("answered", "Answered"), ("hidden", "Hidden"),
    ], default="pending", required=True, index=True)
    answer = fields.Text()
    answered_partner_id = fields.Many2one("res.partner", string="Answered by (Vendor)", index=True)
    answered_user_id = fields.Many2one("res.users", string="Answered by (User)")
    answer_date = fields.Datetime()
    approved = fields.Boolean(
        help="A reviewer approved the answer. Only needed while the moderation setting is on.")
    customer_notified = fields.Boolean(copy=False)
    hidden_by = fields.Many2one("res.users", readonly=True)
    hidden_date = fields.Datetime(readonly=True)
    hidden_reason = fields.Char()
    is_public = fields.Boolean(compute="_compute_is_public", string="Published")

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @api.model
    def _leaders_enabled(self):
        return self.env["ir.config_parameter"].sudo().get_param(PARAM_ENABLED) in ("True", "true", "1")

    @api.model
    def _leaders_moderation(self):
        return self.env["ir.config_parameter"].sudo().get_param(PARAM_MODERATION) in ("True", "true", "1")

    @api.model
    def _leaders_daily_limit(self):
        try:
            limit = int(self.env["ir.config_parameter"].sudo().get_param(PARAM_DAILY_LIMIT) or DEFAULT_DAILY_LIMIT)
        except (TypeError, ValueError):
            limit = DEFAULT_DAILY_LIMIT
        return limit if limit > 0 else DEFAULT_DAILY_LIMIT

    # ------------------------------------------------------------------
    # Computes and constraints
    # ------------------------------------------------------------------
    @api.depends("state", "approved")
    def _compute_is_public(self):
        moderation = self._leaders_moderation()
        for rec in self:
            rec.is_public = rec.state == "answered" and (rec.approved or not moderation)

    @api.constrains("question", "answer")
    def _check_lengths(self):
        for rec in self:
            if len(rec.question or "") > QUESTION_MAX_LENGTH:
                raise ValidationError(_("A question can have at most %(n)s characters.", n=QUESTION_MAX_LENGTH))
            if len(rec.answer or "") > ANSWER_MAX_LENGTH:
                raise ValidationError(_("An answer can have at most %(n)s characters.", n=ANSWER_MAX_LENGTH))

    # ------------------------------------------------------------------
    # Asking
    # ------------------------------------------------------------------
    @api.model
    def _leaders_ask_problem(self, template, user, text):
        """Code of what stops ``user`` asking ``text`` about ``template`` (or False).

        ``empty``, ``long``, ``limit`` (daily maximum per user) or ``duplicate``
        (same text on the same product within a day: also stops a double click).
        """
        if not text:
            return "empty"
        if len(text) > QUESTION_MAX_LENGTH:
            return "long"
        since = fields.Datetime.now() - timedelta(days=1)
        recent = self.sudo().search([("user_id", "=", user.id), ("create_date", ">=", since)])
        if len(recent) >= self._leaders_daily_limit():
            return "limit"
        folded = text.casefold()
        if any(q.product_tmpl_id == template and q.question.casefold() == folded for q in recent):
            return "duplicate"
        return False

    @api.model
    def _leaders_flash_message(self, code):
        """(tone, text) shown on the product page after an ask, from the ``lm_q`` query code."""
        messages = {
            "ok": ("success", _("Thank you. Your question was sent to the sellers of this product. "
                                "It appears here once it is answered.")),
            "empty": ("danger", _("Please write your question.")),
            "long": ("danger", _("Your question is too long (maximum %(n)s characters).", n=QUESTION_MAX_LENGTH)),
            "limit": ("warning", _("You reached the maximum number of questions for today. Please try again tomorrow.")),
            "duplicate": ("warning", _("You already asked this question on this product.")),
        }
        return messages.get(code) or False

    @api.model
    def _leaders_ask(self, template, user, text, product=None):
        """Create a pending question and tell the sellers of the product (one mail each)."""
        text = clean_text(text)
        code = self._leaders_ask_problem(template, user, text)
        if code:
            raise UserError(self._leaders_flash_message(code)[1])
        question = self.sudo().create({
            "product_tmpl_id": template.id,
            "product_id": product.id if product and product.product_tmpl_id == template else False,
            "partner_id": user.partner_id.id,
            "user_id": user.id,
            "question": text,
        })
        question._notify_vendors()
        return question

    # ------------------------------------------------------------------
    # Vendors
    # ------------------------------------------------------------------
    @api.model
    def _leaders_vendor_templates(self, vendor):
        """Product templates on which ``vendor`` has an active offer (any variant)."""
        offers = self.env["leaders.marketplace.offer"].sudo().search([
            ("vendor_id", "=", vendor.commercial_partner_id.id),
            ("state", "=", "active"), ("active", "=", True)])
        return offers.product_tmpl_id

    def _leaders_vendors(self):
        """Vendors with an active offer on this question's product template."""
        self.ensure_one()
        offers = self.env["leaders.marketplace.offer"].sudo().search([
            ("product_tmpl_id", "=", self.product_tmpl_id.id),
            ("state", "=", "active"), ("active", "=", True)])
        return offers.vendor_id.commercial_partner_id

    def _vendor_can_answer(self, vendor):
        self.ensure_one()
        return vendor.commercial_partner_id in self._leaders_vendors()

    def _vendor_can_edit(self, vendor):
        """Answer once; the answering vendor may edit it until a reviewer hides the question."""
        self.ensure_one()
        if self.state == "hidden" or not self._vendor_can_answer(vendor):
            return False
        return self.state == "pending" or self.answered_partner_id == vendor.commercial_partner_id

    def action_vendor_answer(self, vendor, user, text):
        """Answer (or edit the answer of) this question as ``vendor``. Called by the portal (sudo)."""
        self.ensure_one()
        text = clean_text(text)
        if not text:
            raise UserError(_("Please write an answer."))
        if len(text) > ANSWER_MAX_LENGTH:
            raise UserError(_("An answer can have at most %(n)s characters.", n=ANSWER_MAX_LENGTH))
        if not self._vendor_can_answer(vendor):
            raise UserError(_("Only a seller with an active offer on this product can answer."))
        if self.state == "hidden":
            raise UserError(_("This question was hidden by the marketplace."))
        if self.state == "answered" and self.answered_partner_id != vendor.commercial_partner_id:
            raise UserError(_("This question was already answered by another seller."))
        self.sudo().write({
            "state": "answered", "answer": text,
            "answered_partner_id": vendor.commercial_partner_id.id, "answered_user_id": user.id,
            "answer_date": fields.Datetime.now(),
            # an edited answer goes back to the reviewer while moderation is on
            "approved": not self._leaders_moderation(),
        })
        self._notify_asker()
        return True

    # ------------------------------------------------------------------
    # Reviewers
    # ------------------------------------------------------------------
    def action_hide(self, reason=None):
        self._leaders_check_backoffice("reviewer")
        self.sudo().write({
            "state": "hidden", "hidden_by": self.env.user.id, "hidden_date": fields.Datetime.now(),
            "hidden_reason": reason or self.hidden_reason or False})
        return True

    def action_unhide(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self.sudo():
            rec.write({
                "state": "answered" if rec.answer else "pending",
                "hidden_by": False, "hidden_date": False, "hidden_reason": False})
        return True

    def action_approve(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self.sudo().filtered(lambda q: q.state == "answered"):
            rec.approved = True
            rec._notify_asker()
        return True

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    def _notify_vendors(self):
        """``vendor_question_asked``: one mail per vendor with an active offer (owner and staff)."""
        Notification = self.env["leaders.notification"]
        for rec in self.sudo():
            for vendor in rec._leaders_vendors():
                Notification.notify_vendor("vendor_question_asked", rec, vendor)

    def _notify_asker(self):
        """``customer_question_answered``: once, when the answer becomes public."""
        for rec in self.sudo():
            if rec.customer_notified or not rec.is_public:
                continue
            rec.customer_notified = True
            self.env["leaders.notification"].notify_customer("customer_question_answered", rec, rec.partner_id)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    def _leaders_questions_context(self):
        """What the product page shows for questions and answers ({} = nothing: switch off)."""
        self.ensure_one()
        Question = self.env["leaders.product.question"]
        if not Question._leaders_enabled():
            return {}
        sudo_questions = Question.sudo()
        public = sudo_questions.search([
            ("product_tmpl_id", "=", self.id), ("state", "=", "answered")]).filtered("is_public")
        user = self.env.user
        mine = sudo_questions.browse()
        if not user._is_public():
            mine = sudo_questions.search([
                ("product_tmpl_id", "=", self.id), ("user_id", "=", user.id),
                ("state", "!=", "hidden")]) - public
        return {
            "questions": public.sorted(lambda q: (q.answer_date or q.create_date, q.id), reverse=True),
            "mine": mine,
            "max_length": QUESTION_MAX_LENGTH,
        }
