from odoo import api, models

# FBL-only backend menus, hidden while the platform has no warehouse of its own.
FBL_ONLY_MENUS = (
    "leaders_marketplace.menu_fbl_inbound",
    "leaders_marketplace.menu_fulfillment_fee",
)


class IrUiMenu(models.Model):
    _inherit = "ir.ui.menu"

    @api.model
    def _visible_menu_ids(self, debug=False):
        visible = super()._visible_menu_ids(debug=debug)
        if self.env["res.partner"]._leaders_fbl_enabled():
            return visible
        hidden = set()
        for xmlid in FBL_ONLY_MENUS:
            menu = self.env.ref(xmlid, raise_if_not_found=False)
            if menu:
                hidden.add(menu.id)
        return visible - hidden if hidden else visible
