from odoo import fields, models


class LeadersReturnReason(models.Model):
    """Reason a customer picks when requesting a return (TASK 29 / D2).

    Configuration under Marketplace > Configuration > Return Reasons. The two
    flags drive the request: ``requires_photo`` refuses a request without a
    photo, ``customer_pays_shipping`` deducts the return shipping fee (setting
    ``return_shipping_fee``) from the refund.
    """
    _name = "leaders.return.reason"
    _description = "Return Reason"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    leaders_parts_allowed = fields.Boolean(
        "Allowed for spare parts", help="A spare part is returned only when it does not match the order "
                                        "(damaged, wrong item, not as described) and before it is installed.")
    requires_photo = fields.Boolean(
        string="Photo Required",
        help="The customer must attach at least one photo to request a return with this reason.")
    customer_pays_shipping = fields.Boolean(
        string="Customer Pays Shipping",
        help="The return shipping fee (Settings > Marketplace) is deducted from the customer's refund.")
