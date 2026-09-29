from odoo import _
from odoo.http import request, route
from odoo.exceptions import UserError

from odoo.addons.website_sale.controllers.main import WebsiteSale


class MarketplaceCheckout(WebsiteSale):

    def _check_cart(self, order_sudo):
        """Never let a cart be paid at a price the customer has not seen.

        Runs on every step that validates the cart (/shop/checkout and, through
        ``_check_cart_and_addresses``, /shop/payment). If a vendor line's offer
        went away or its price moved since add-to-cart, the line is repointed
        and repriced and the customer is sent back to review it.
        """
        if redirection := super()._check_cart(order_sudo):
            return redirection
        if order_sudo._leaders_revalidate_marketplace_prices():
            return request.redirect("/shop/cart?lm_price_changed=1")

    def _get_shop_payment_values(self, order, **kwargs):
        values = super()._get_shop_payment_values(order, **kwargs)
        values["marketplace_cod_enabled"] = bool(
            request.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.cod_enabled"))
        return values

    def _prepare_shop_payment_confirmation_values(self, order):
        values = super()._prepare_shop_payment_confirmation_values(order)
        sudo_order = order.sudo()
        present = sudo_order._marketplace_vendor_only_presentation() and sudo_order.vendor_order_ids
        values["lm_conf_total"] = sudo_order._marketplace_checkout_total() if present else False
        values["lm_vendor_only"] = bool(present and sudo_order.marketplace_vendor_only)
        return values

    @route("/shop/payment/cod", type="http", auth="public", website=True, methods=["POST"], csrf=True)
    def shop_pay_cod(self, **post):
        if not request.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.cod_enabled"):
            raise UserError(_("Cash on Delivery is not available."))
        order_sudo = request.website.sale_get_order()

        if redirection := self._check_cart_and_addresses(order_sudo):
            return redirection
        if redirection := self._check_shipping_method(order_sudo):
            return redirection

        order_sudo.write({"leaders_payment_method": "cod"})
        order_sudo.with_context(send_email=True).action_confirm()

        request.session["sale_last_order_id"] = order_sudo.id
        request.website.sale_reset()
        return request.redirect("/shop/confirmation")
