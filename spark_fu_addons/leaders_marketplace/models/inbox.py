"""The notification list of the app's bell (``GET /account/inbox``).

Every notification event of a customer (or a vendor's staff) that is sent as a push is also written here, whether or
not the person has a device, so one who missed or dismissed the push still finds it. The texts are kept in Arabic
and English; the list shows the language of the request. Entries live for ``KEEP_DAYS`` days.
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

KEEP_DAYS = 90


class LeadersInboxEntry(models.Model):
    _name = "leaders.inbox.entry"
    _description = "Notification List Entry"
    _order = "id desc"

    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    event = fields.Char(index=True, help="The event name the push carries: customer_order_shipped...")
    title = fields.Char("Title (English)", required=True)
    body = fields.Char("Text (English)")
    title_ar = fields.Char("Title (Arabic)")
    body_ar = fields.Char("Text (Arabic)")
    reference = fields.Char(help="The number the customer knows (SF-1042); empty when the event has none.")
    res_model = fields.Char("Record model")
    res_id = fields.Integer("Record")
    read = fields.Boolean(default=False, index=True)

    @api.model
    def _leaders_users_of(self, partners, whole_company=False):
        """The people behind ``partners``: exactly those contacts, or (a customer's order is on the company) every
        user of their company: the same people the devices are taken from."""
        if not partners:
            return self.env["res.users"]
        domain = [("partner_id.commercial_partner_id", "in", partners.commercial_partner_id.ids)] if whole_company \
            else [("partner_id", "in", partners.ids)]
        return self.env["res.users"].sudo().search(domain)

    @api.model
    def _leaders_record(self, users, key, record, texts_for, data):
        """One entry per user. ``texts_for(lang)`` gives ``(title, body)`` in ``"ar"`` or ``"en"``. Returns
        ``{user id: entry}``; an event with no text (no template) leaves no entry."""
        title_en, body_en = texts_for("en")
        if not title_en:
            return {}
        title_ar, body_ar = texts_for("ar")
        vals = {"event": key, "title": title_en, "body": body_en or "", "title_ar": title_ar or False,
                "body_ar": body_ar or False, "reference": data.get("reference") or False,
                "res_model": data.get("model") or False, "res_id": int(data.get("id") or 0)}
        entries = self.sudo().create([dict(vals, user_id=user.id) for user in users])
        return {entry.user_id.id: entry for entry in entries}

    def _leaders_texts_in(self, arabic):
        self.ensure_one()
        if arabic and self.title_ar:
            return self.title_ar, self.body_ar or ""
        return self.title, self.body or ""

    @api.model
    def _purge(self):
        limit = fields.Datetime.now() - timedelta(days=KEEP_DAYS)
        self.sudo().search([("create_date", "<", limit)]).unlink()
