"""Storefront profile (profile-pages contract): business details and notification channels on the customer."""
from odoo import fields, models


class ResPartnerAccountProfile(models.Model):
    _inherit = "res.partner"

    leaders_activity_type = fields.Char("Activity type", help="The customer's line of business, as typed on the storefront.")
    leaders_commercial_register = fields.Char("Commercial register number")
    # notification channels (GET/PUT /account/notifications); email is the one the platform sends today
    leaders_notify_email = fields.Boolean(
        "Email notifications", default=True,
        help="Off: no optional email is sent (shipping, returns, answers, promotions). "
             "Transactional emails (cancellations, refunds) are always sent.")
    leaders_notify_sms = fields.Boolean("SMS notifications", default=False,
                                        help="Kept for when SMS notifications exist; nothing is texted today.")
    leaders_notify_whatsapp = fields.Boolean("WhatsApp notifications", default=False,
                                             help="Kept for when WhatsApp notifications exist; nothing is sent today.")

    def _leaders_customer_accepts(self, category):
        accepted = super()._leaders_customer_accepts(category)
        if accepted and category != "orders" and not self.commercial_partner_id.sudo().leaders_notify_email:
            return False
        return accepted
