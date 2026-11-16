"""Item requests: "could not find it? request the product" on an empty catalog result (POST /item-requests)."""
import logging

from odoo import _, fields, models

_logger = logging.getLogger(__name__)

ITEM_TYPES = [("power", "Power solution"), ("equipment", "Equipment"), ("part", "Spare part")]


class LeadersCustomerRequestItem(models.Model):
    _inherit = "leaders.customer.request"

    kind = fields.Selection(selection_add=[("item", "Item request")], ondelete={"item": "cascade"})
    contact_name = fields.Char("Contact name", help="The name typed in the form (a guest has no account).")
    contact_email = fields.Char("Contact email")
    item_type = fields.Selection(ITEM_TYPES, "Product type")
    item_brand = fields.Char("Brand asked", help="As typed by the customer.")
    item_brand_id = fields.Many2one("leaders.brand", "Catalog brand", ondelete="set null",
                                    help="The catalog brand matching the typed brand, when there is one.")
    item_model = fields.Char("Model asked")
    request_locale = fields.Selection([("ar", "Arabic"), ("en", "English")], "Form language")

    def notify_item_sales(self):
        """A to-do for every sales user, so a product request is answered."""
        group = self.env.ref("leaders_marketplace.group_equipment_listing_sales", raise_if_not_found=False)
        users = group.sudo().users.filtered("active") if group else self.env["res.users"]
        for rec in self:
            for user in users:
                summary = self.with_context(lang=user.lang or "en_US").env._("New product request %s", rec.name)
                rec.sudo().activity_schedule("mail.mail_activity_data_todo", user_id=user.id, summary=summary,
                                             date_deadline=fields.Date.context_today(rec))

    def send_item_confirmation(self):
        """Email the customer the reference, in the form's language; a mail problem never fails the request."""
        template = self.env.ref("leaders_marketplace.mail_template_item_request_received", raise_if_not_found=False)
        for rec in self.filtered(lambda r: r.kind == "item" and r.contact_email):
            try:
                template.sudo().send_mail(rec.id, email_values={"email_to": rec.contact_email, "recipient_ids": []})
                cron = self.env.ref("mail.ir_cron_mail_scheduler_action", raise_if_not_found=False)
                if cron:
                    cron.sudo()._trigger()  # sent within seconds, not at the next hourly run
            except Exception:
                _logger.exception("Could not queue the confirmation email of product request %s", rec.name)


class LeadersBrandLogo(models.Model):
    _inherit = "leaders.brand"

    logo = fields.Image("Logo", max_width=512, max_height=512,
                        help="Shown on the storefront's brand filter. A transparent PNG or SVG-like image works best.")
