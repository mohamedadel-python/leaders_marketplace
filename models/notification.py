import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

# Customer preference categories (D5). Every ``customer_*`` event of NOTIFICATION_EVENTS must
# be listed here (a test fails otherwise). The category is a Boolean on res.partner.
CUSTOMER_EVENT_CATEGORY = {
    "customer_order_shipped": "shipping",
    "customer_order_delivered": "shipping",
    "customer_order_cancelled": "orders",
    "customer_order_cancelled_by_customer": "orders",
    "customer_return_approved": "returns",
    "customer_return_rejected": "returns",
    "customer_return_refunded": "returns",
    "customer_question_answered": "questions",
}
# Transactional / financial mails: always sent whatever the preferences say.
CUSTOMER_MANDATORY_EVENTS = frozenset({
    "customer_order_cancelled", "customer_order_cancelled_by_customer", "customer_return_refunded",
})


class MarketplaceNotification(models.AbstractModel):
    """Queues the marketplace's email notifications.

    Every event has a ``mail.template`` named ``mail_template_<key>`` and a
    setting ``leaders_marketplace.notify_<key>`` (on by default). Mails are only
    queued, never sent inline, and a failure to queue one never breaks the
    business transaction that raised the event.
    """
    _name = "leaders.notification"
    _description = "Marketplace Notifications"

    @api.model
    def _enabled(self, key):
        return self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.notify_%s" % key) in ("True", "true", "1")

    @api.model
    def _vendor_recipients(self, vendor):
        """Partners of the vendor's owner and staff users that have an email."""
        groups = (self.env.ref("leaders_marketplace.group_vendor_owner")
                  | self.env.ref("leaders_marketplace.group_vendor_staff"))
        users = self.env["res.users"].sudo().search([
            ("partner_id.commercial_partner_id", "=", vendor.commercial_partner_id.id),
            ("groups_id", "in", groups.ids),
        ])
        return users.partner_id.filtered("email")

    @api.model
    def _vendor_owner_partners(self, vendor):
        group = self.env.ref("leaders_marketplace.group_vendor_owner")
        return self.env["res.users"].sudo().search([
            ("partner_id.commercial_partner_id", "=", vendor.commercial_partner_id.id),
            ("groups_id", "in", group.ids),
        ]).partner_id

    @api.model
    def notify(self, key, record, partners=None, email_to=None, force=False, template_xmlid=None):
        """Queue one mail for ``record`` to ``partners`` (and/or a raw address).

        ``template_xmlid`` picks another template of the event (same setting) when the record is
        not the event's usual model. ``force`` skips the on/off setting: only for security notices that must
        always reach their recipient (see ``leaders.vendor.bank.change``).
        """
        if not force and not self._enabled(key):
            return False
        partners = partners.filtered("email") if partners else self.env["res.partner"]
        category = CUSTOMER_EVENT_CATEGORY.get(key)
        if category and not force and key not in CUSTOMER_MANDATORY_EVENTS:
            # D5: the customer's own preference (kept on the commercial partner).
            accepted = partners.filtered(lambda p: p._leaders_customer_accepts(category))
            if accepted != partners:
                _logger.debug("%s: skipped for %s (email preference)", key, (partners - accepted).ids)
            partners = accepted
        if not partners and not email_to:
            return False
        template = self.env.ref(
            template_xmlid or "leaders_marketplace.mail_template_%s" % key, raise_if_not_found=False)
        if not template:
            return False
        values = {}
        if partners:
            values["recipient_ids"] = [(6, 0, partners.ids)]
        if email_to:
            values["email_to"] = email_to
        try:
            with self.env.cr.savepoint():
                return template.sudo().send_mail(record.id, force_send=False, email_values=values)
        except Exception:  # noqa: BLE001 - a notification must never block the transaction
            _logger.exception("Could not queue the %s notification for %s", key, record)
            return False

    @api.model
    def notify_vendor(self, key, record, vendor):
        return self.notify(key, record, partners=self._vendor_recipients(vendor))

    @api.model
    def notify_customer(self, key, record, customer, template_xmlid=None):
        return self.notify(key, record, partners=customer, template_xmlid=template_xmlid)
