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
        order_sudo = order.sudo()
        cod_on = bool(request.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.cod_enabled"))
        allowed, message = order_sudo._leaders_cod_availability() if cod_on else (False, "")
        # Above the limit the option stays visible with the reason; a customer
        # blocked for refusals simply does not see it.
        blocked = bool(cod_on and not allowed and order_sudo.partner_id.commercial_partner_id._leaders_cod_blocked())
        values["marketplace_cod_enabled"] = bool(cod_on and allowed)
        values["marketplace_cod_message"] = message if cod_on and not allowed and not blocked else ""
        require_otp = order_sudo._leaders_cod_require_otp()
        values["marketplace_cod_otp"] = bool(require_otp)
        values["marketplace_cod_verified"] = bool(order_sudo.leaders_cod_phone_verified)
        values["marketplace_cod_phone"] = (
            order_sudo.leaders_cod_phone or request.session.get("lm_cod_phone")
            or order_sudo.partner_id.phone or order_sudo.partner_id.mobile or "")
        values["marketplace_cod_status"] = request.params.get("lm_cod") or ""
        values["marketplace_cod_error"] = (
            request.session.pop("lm_cod_error", "") if values["marketplace_cod_status"] == "error" else "")
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
        # Amount limit, refusal block and (when required) phone verification.
        order_sudo._leaders_cod_check_allowed()

        order_sudo.write({"leaders_payment_method": "cod"})
        order_sudo.with_context(send_email=True).action_confirm()

        request.session["sale_last_order_id"] = order_sudo.id
        request.website.sale_reset()
        return request.redirect("/shop/confirmation")

    def _cod_otp_order(self):
        order_sudo = request.website.sale_get_order()
        if not order_sudo or order_sudo.state != "draft":
            return False
        return order_sudo

    @route("/shop/payment/cod/otp/send", type="http", auth="public", website=True, methods=["POST"], csrf=True)
    def shop_cod_otp_send(self, phone=None, **post):
        """Send a verification code for the current cart's COD payment."""
        order_sudo = self._cod_otp_order()
        if not order_sudo:
            return request.redirect("/shop/cart")
        status = "sent"
        try:
            order_sudo._leaders_cod_check_allowed_for_otp()
            request.session["lm_cod_phone"] = (phone or "").strip()
            request.env["leaders.cod.otp"].sudo().leaders_send_code(order_sudo, phone)
        except UserError as error:
            status = "error"
            request.session["lm_cod_error"] = str(error.args[0])
        return request.redirect("/shop/payment?lm_cod=%s" % status)

    @route("/shop/payment/cod/otp/verify", type="http", auth="public", website=True, methods=["POST"], csrf=True)
    def shop_cod_otp_verify(self, phone=None, code=None, **post):
        """Check the code the customer typed; success marks the order's phone verified."""
        order_sudo = self._cod_otp_order()
        if not order_sudo:
            return request.redirect("/shop/cart")
        status = "verified"
        try:
            order_sudo._leaders_cod_check_allowed_for_otp()
            if not request.env["leaders.cod.otp"].sudo().leaders_verify_code(order_sudo, phone, code):
                status = "wrong"
        except UserError as error:
            status = "error"
            request.session["lm_cod_error"] = str(error.args[0])
        return request.redirect("/shop/payment?lm_cod=%s" % status)
