from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_is_zero


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    marketplace_vendor_id = fields.Many2one(
        "res.partner", string="Marketplace Vendor", index=True, readonly=True,
        help="Set on warehouses that hold a marketplace vendor's own (FBM) stock.",
    )


class StockQuant(models.Model):
    _inherit = "stock.quant"

    @api.model
    def _leaders_adjust_vendor_stock(self, product, location, owner, qty_delta, reason=None):
        """Add/remove vendor-owned stock through a real inventory adjustment move.

        The quant carries the vendor as owner, so it is never valued as company stock
        and is only reserved by moves restricted to that vendor.
        """
        rounding = product.uom_id.rounding
        if float_is_zero(qty_delta, precision_rounding=rounding):
            return self.browse()
        if not product.is_storable:
            raise UserError(_("Marketplace stock can only be managed for storable products."))
        Quant = self.sudo().with_context(inventory_mode=True)
        quant = Quant.search([
            ("product_id", "=", product.id),
            ("location_id", "=", location.id),
            ("owner_id", "=", owner.id if owner else False),
            ("lot_id", "=", False),
            ("package_id", "=", False),
        ], limit=1)
        current = quant.quantity if quant else 0.0
        new_qty = current + qty_delta
        if new_qty < 0 and not float_is_zero(new_qty, precision_rounding=rounding):
            raise UserError(_("Not enough vendor stock of %s to remove.", product.display_name))
        vals = {"inventory_quantity": max(new_qty, 0.0), "inventory_quantity_set": True}
        if quant:
            quant.write(vals)
        else:
            quant = Quant.create(dict(vals, product_id=product.id, location_id=location.id,
                                      owner_id=owner.id if owner else False))
        quant.with_context(inventory_name=reason or _("Marketplace vendor stock update"))._apply_inventory()
        if qty_delta > 0:  # deliveries of this vendor that were waiting for stock take it now
            self.env["stock.move"].sudo().search([
                ("product_id", "=", product.id), ("location_id", "=", location.id),
                ("state", "in", ("confirmed", "partially_available")),
                ("restrict_partner_id", "=", owner.id if owner else False)])._action_assign()
        # Availability is a Buy Box signal, so the winner of this product is stale now.
        self.env["leaders.marketplace.offer"].sudo()._recompute_buybox_winner(
            product_ids=product.ids)
        return quant


class StockMove(models.Model):
    _inherit = "stock.move"

    leaders_vendor_line_id = fields.Many2one("leaders.vendor.order.line", index=True, copy=False)

    def _prepare_move_split_vals(self, qty):
        # A backorder move is the rest of the same vendor order line.
        vals = super()._prepare_move_split_vals(qty)
        if self.leaders_vendor_line_id:
            vals["leaders_vendor_line_id"] = self.leaders_vendor_line_id.id
        return vals


class StockPicking(models.Model):
    _inherit = "stock.picking"

    leaders_fulfillment_group_id = fields.Many2one("leaders.fulfillment.group", index=True, copy=False)
    leaders_vendor_order_id = fields.Many2one(
        related="leaders_fulfillment_group_id.vendor_order_id", store=True, index=True)
    leaders_return_id = fields.Many2one("leaders.vendor.return", index=True, copy=False)

    def _create_backorder_picking(self):
        # A partial shipment: the backorder stays in the same fulfillment group.
        backorder = super()._create_backorder_picking()
        if self.leaders_fulfillment_group_id:
            backorder.leaders_fulfillment_group_id = self.leaders_fulfillment_group_id
        return backorder

    def action_cancel(self):
        """A backorder cancelled in the warehouse gives up that quantity for good.

        When the picking belongs to a fulfillment group that already shipped
        something, the cancelled quantity is a cancelled remainder: the vendor
        order money is resized through ``_leaders_cancel_quantities`` (by the
        warehouse), exactly as the vendor's "Cancel Remaining" does. Cancelling an
        order that never shipped stays the whole-order cancellation (its own flow).
        """
        tracked = {}
        if not self.env.context.get("leaders_skip_cancel_quantities"):
            for picking in self.filtered(
                    lambda p: p.leaders_fulfillment_group_id and not p.leaders_return_id
                    and p.state not in ("done", "cancel")):
                group = picking.leaders_fulfillment_group_id.sudo()
                if group.state in ("cancelled",) or group.vendor_order_id.state in ("pending", "cancelled"):
                    continue
                if not group.picking_ids.filtered(lambda p: p.state == "done"):
                    continue
                quantities = tracked.setdefault(group, {})
                for move in picking.move_ids.filtered(
                        lambda m: m.leaders_vendor_line_id and m.state not in ("done", "cancel")):
                    line = move.leaders_vendor_line_id
                    quantities[line] = quantities.get(line, 0.0) + move.product_uom_qty
        res = super().action_cancel()
        for group, quantities in tracked.items():
            order = group.vendor_order_id
            usable = {}
            for line, qty in quantities.items():
                qty = min(qty, line.product_uom_qty - line.qty_cancelled - line.qty_delivered)
                if qty > 0:
                    usable[line] = qty
            if usable:
                company = group.picking_ids[:1].company_id or self.env.company
                order.with_company(company)._leaders_cancel_quantities(
                    usable, reason=_("Delivery cancelled in the warehouse"), by="warehouse")
            group._sync_from_pickings()
        return res

    def _leaders_marketplace_pickings(self, company):
        """This company's pickings, as sudo, for the marketplace side-effects.

        Validating the picking stays in the warehouse user's own env. What the
        marketplace does next -- fulfillment group and vendor order states, the
        wallet ledger, journal entries, vendor invoices -- is platform
        bookkeeping a warehouse user has no ACL for, so it runs as sudo.

        ``with_company`` is not cosmetic: the accounting engine reads
        ``self.env.company`` for the customer receivable and the bank journal,
        and the validating user's default company need not be the picking's.
        """
        return self.filtered(
            lambda p: p.company_id == company).sudo().with_company(company)

    def _leaders_log_validation(self):
        """Name the human behind a sudo-run validation on the vendor order."""
        if self.env.su:
            # No real user to attribute: an internal flow, a cron or a test.
            return
        user_name = self.env.user.display_name
        for picking in self.filtered("leaders_fulfillment_group_id"):
            order = picking.leaders_fulfillment_group_id.sudo().vendor_order_id
            if order:
                order.message_post(body=_(
                    "Shipment validated by %(user)s (%(picking)s).",
                    user=user_name, picking=picking.name))

    def _leaders_refresh_buybox(self):
        """Refresh the Buy Box of products whose vendor-owned stock just moved.

        A delivery out of a vendor location changes what that vendor can still
        ship, which is a Buy Box signal; nothing edits the offer, so without
        this the stored winner would keep selling a sold-out offer.
        """
        moves = self.move_ids.filtered(
            lambda m: m.state == "done"
            and (m.restrict_partner_id.is_marketplace_vendor
                 or m.picking_id.owner_id.is_marketplace_vendor))
        products = moves.product_id
        if products:
            self.env["leaders.marketplace.offer"].sudo()._recompute_buybox_winner(
                product_ids=products.ids)

    def _action_done(self):
        res = super()._action_done()
        self._leaders_refresh_buybox()
        for company in self.company_id:
            pickings = self._leaders_marketplace_pickings(company)
            pickings.filtered("leaders_fulfillment_group_id").leaders_fulfillment_group_id._sync_from_pickings()
            # Return receipts validated by the warehouse (FBL) trigger the refund too.
            pickings.filtered("leaders_return_id").leaders_return_id._on_goods_received()
        self._leaders_log_validation()
        return res

    def action_assign(self):
        res = super().action_assign()
        for company in self.company_id:
            pickings = self._leaders_marketplace_pickings(company)
            pickings.filtered("leaders_fulfillment_group_id").leaders_fulfillment_group_id._sync_from_pickings()
        return res
