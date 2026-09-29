from odoo import models


class Website(models.Model):
    _inherit = "website"

    def _get_product_available_qty(self, product, **kwargs):
        """Marketplace products are sold from vendor-owned stock, not the website warehouse."""
        offer = self.env["leaders.marketplace.offer"]._get_marketplace_winner(product)
        if offer:
            return offer.available_qty()
        return super()._get_product_available_qty(product, **kwargs)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _get_cart_and_free_qty(self, product, line=None):
        cart_qty, free_qty = super()._get_cart_and_free_qty(product, line=line)
        product = product or line.product_id
        offer = self.env["leaders.marketplace.offer"]._get_marketplace_winner(product)
        explicit = line.marketplace_offer_id if line else self.env["leaders.marketplace.offer"]
        explicit = explicit or self.env["leaders.marketplace.offer"].sudo().browse(
            self.env.context.get("leaders_explicit_offer_id"))
        if explicit and explicit != offer and explicit.state == "active":
            # A seller picked from "Other sellers": their stock, their lines.
            cart_qty = sum(self.order_line.filtered(
                lambda l: l.product_id == product and l.marketplace_offer_id == explicit).mapped("product_uom_qty"))
            return cart_qty, explicit.available_qty()
        if offer:
            free_qty = offer.available_qty()
        return cart_qty, free_qty

    def _verify_updated_quantity(self, order_line, product_id, new_qty, **kwargs):
        offer = self._leaders_explicit_offer(
            self.env["product.product"].browse(product_id), kwargs.get("offer_id"))
        if offer:
            self = self.with_context(leaders_explicit_offer_id=offer.id)
        return super(SaleOrder, self)._verify_updated_quantity(order_line, product_id, new_qty, **kwargs)
