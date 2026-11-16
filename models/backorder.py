"""Partial stock (quote-offer contract, item 13): units beyond the seller's stock that the customer asked the
platform to source from another supplier ("نعم، وفّر الكمية من مورد آخر"). They are not billed on the order:
the sales team gets a sourcing to-do and comes back to the customer."""
from odoo import fields, models


class LeadersCartLineBackorder(models.Model):
    _inherit = "leaders.cart.line"

    backorder = fields.Integer("Units to source", default=0)


class SaleOrderBackorder(models.Model):
    _inherit = "sale.order"

    def _leaders_open_sourcing_tasks(self, wanted):
        """``wanted``: [(product, units)] -> one to-do per line for every sales user."""
        group = self.env.ref("leaders_marketplace.group_equipment_listing_sales", raise_if_not_found=False)
        users = group.sudo().users.filtered("active") if group else self.env["res.users"]
        for order in self:
            for product, units in wanted:
                for user in users:
                    summary = self.with_context(lang=user.lang or "en_US").env._(
                        "Source %(units)s × %(product)s for order %(order)s",
                        units=units, product=product.display_name, order=order.leaders_public_ref or order.name)
                    order.sudo().activity_schedule("mail.mail_activity_data_todo", user_id=user.id, summary=summary,
                                                   date_deadline=fields.Date.context_today(order))
