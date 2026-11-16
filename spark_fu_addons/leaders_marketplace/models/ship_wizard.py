from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ShipWizard(models.TransientModel):
    _name = "leaders.ship.wizard"
    _description = "Ship Quantities of a Fulfillment Group"

    group_id = fields.Many2one("leaders.fulfillment.group", required=True, readonly=True)
    tracking_ref = fields.Char(string="Tracking Reference")
    line_ids = fields.One2many("leaders.ship.wizard.line", "wizard_id", string="Products")

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        group = self.env["leaders.fulfillment.group"].browse(
            self.env.context.get("leaders_ship_group_id") or vals.get("group_id"))
        if group:
            group.ensure_one()
            if group.state in ("shipped", "delivery_reported", "delivered", "cancelled"):
                raise UserError(_("This shipment can no longer be shipped."))
            info = group._ship_available_quantities()
            vals["group_id"] = group.id
            if not group.picking_ids.filtered(lambda p: p.state == "done"):
                vals["tracking_ref"] = group.tracking_ref
            vals["line_ids"] = [(0, 0, {
                "vendor_line_id": line.id,
                "demanded_qty": demanded,
                "available_qty": available,
                "quantity": available,
            }) for line, (demanded, available) in info.items()]
        return vals

    def action_ship(self):
        self.ensure_one()
        quantities = {l.vendor_line_id.id: l.quantity for l in self.line_ids}
        self.group_id.action_ship(quantities=quantities, tracking_ref=self.tracking_ref)
        return {"type": "ir.actions.act_window_close"}


class ShipWizardLine(models.TransientModel):
    _name = "leaders.ship.wizard.line"
    _description = "Ship Wizard Line"

    wizard_id = fields.Many2one("leaders.ship.wizard", required=True, ondelete="cascade")
    vendor_line_id = fields.Many2one("leaders.vendor.order.line", required=True, readonly=True)
    product_id = fields.Many2one(related="vendor_line_id.product_id")
    demanded_qty = fields.Float(string="Ordered", readonly=True, digits="Product Unit of Measure")
    available_qty = fields.Float(string="Available", readonly=True, digits="Product Unit of Measure")
    quantity = fields.Float(string="To Ship", digits="Product Unit of Measure")
