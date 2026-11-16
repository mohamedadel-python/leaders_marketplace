"""One-time sign-in link from the storefront to the vendor application pages."""
import secrets
from datetime import timedelta

from odoo import api, fields, models

from ..tools.auth_tokens import hash_token

TICKET_SECONDS = 60


class LeadersVendorApplyTicket(models.Model):
    """The storefront signs the customer in with a JWT that Odoo's website does not see.

    ``POST /vendor-apply/link`` trades that JWT for one of these tickets; opening
    ``/vendor/apply/start?ticket=...`` signs the same account into the website once and
    sends it to the page that fits its application. Only the hash of the ticket is stored.
    """
    _name = "leaders.vendor.apply.ticket"
    _description = "Vendor Application Sign-in Link"

    token_hash = fields.Char(required=True, index=True, readonly=True)
    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    target = fields.Char(required=True, help="Path the ticket opens after signing in.")
    expires_at = fields.Datetime(required=True)
    used = fields.Boolean(default=False)

    @api.model
    def issue(self, user, target):
        tickets = self.sudo()
        tickets.search([("expires_at", "<", fields.Datetime.now() - timedelta(hours=1))]).unlink()
        raw = secrets.token_urlsafe(32)
        tickets.create({"token_hash": hash_token(raw), "user_id": user.id, "target": target,
                        "expires_at": fields.Datetime.now() + timedelta(seconds=TICKET_SECONDS)})
        return raw

    @api.model
    def consume(self, raw):
        """The ticket of ``raw`` if it is still valid, marked used; else an empty recordset."""
        if not raw:
            return self.browse()
        # one atomic UPDATE: two requests with the same ticket cannot both get it
        self.env.cr.execute(
            "UPDATE leaders_vendor_apply_ticket SET used = TRUE WHERE token_hash = %s AND NOT used "
            "AND expires_at > %s RETURNING id", (hash_token(raw), fields.Datetime.now()))
        row = self.env.cr.fetchone()
        self.invalidate_model(["used"])
        ticket = self.sudo().browse(row[0] if row else [])
        return ticket
