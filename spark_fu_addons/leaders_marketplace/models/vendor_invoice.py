from odoo import api, fields, models, _


class VendorInvoice(models.Model):
    """Invoice / credit note issued in the VENDOR's name to the customer.

    It is a marketplace document, not a company ``account.move``: the company only
    collects the money on the vendor's behalf (escrow) and books its own commission.
    """
    _name = "leaders.vendor.invoice"
    _description = "Vendor Invoice"
    _inherit = ["mail.thread", "portal.mixin"]
    _order = "invoice_date desc, id desc"

    name = fields.Char(required=True, copy=False, readonly=True, default="/")
    move_type = fields.Selection([
        ("out_invoice", "Invoice"), ("out_refund", "Credit Note"),
    ], required=True, default="out_invoice", readonly=True, index=True)
    vendor_id = fields.Many2one("res.partner", required=True, index=True, readonly=True)
    partner_id = fields.Many2one("res.partner", string="Customer", required=True, index=True, readonly=True)
    vendor_order_id = fields.Many2one("leaders.vendor.order", required=True, ondelete="restrict", index=True, readonly=True)
    return_id = fields.Many2one("leaders.vendor.return", readonly=True, index=True)
    reversed_invoice_id = fields.Many2one("leaders.vendor.invoice", string="Reversal of", readonly=True)
    line_ids = fields.One2many("leaders.vendor.invoice.line", "invoice_id", readonly=True)
    invoice_date = fields.Date(default=fields.Date.context_today, readonly=True)
    currency_id = fields.Many2one("res.currency", required=True, readonly=True)
    amount_untaxed = fields.Monetary(currency_field="currency_id", compute="_compute_amounts", store=True)
    amount_tax = fields.Monetary(currency_field="currency_id", compute="_compute_amounts", store=True)
    amount_total = fields.Monetary(currency_field="currency_id", compute="_compute_amounts", store=True)
    state = fields.Selection([
        ("posted", "Posted"), ("cancelled", "Cancelled"),
    ], default="posted", tracking=True, index=True)
    payment_state = fields.Selection([
        ("not_paid", "Not Paid"), ("paid", "Paid"),
    ], default="not_paid", tracking=True,
        help="Invoice: paid once the customer's payment covers it. Credit note: paid once refunded.")

    @api.depends("line_ids.price_subtotal", "line_ids.price_total")
    def _compute_amounts(self):
        for rec in self:
            rec.amount_untaxed = sum(rec.line_ids.mapped("price_subtotal"))
            rec.amount_total = sum(rec.line_ids.mapped("price_total"))
            rec.amount_tax = rec.amount_total - rec.amount_untaxed

    def _compute_access_url(self):
        super()._compute_access_url()
        for rec in self:
            rec.access_url = f"/my/marketplace/vendor-invoices/{rec.id}"

    @api.model
    def _next_name(self, vendor, move_type):
        seq = vendor._marketplace_next_invoice_number()
        year = fields.Date.context_today(self).year
        code = vendor.vendor_warehouse_id.code or f"V{vendor.id}"
        prefix = "RINV" if move_type == "out_refund" else "INV"
        return f"{code}/{prefix}/{year}/{seq:05d}"

    @api.model
    def _create_for_vendor_order(self, vendor_order):
        invoice = vendor_order.invoice_id
        if invoice:
            return invoice
        vendor = vendor_order.vendor_id.commercial_partner_id
        invoice = self.sudo().create({
            "name": self._next_name(vendor, "out_invoice"),
            "vendor_id": vendor.id,
            "partner_id": vendor_order.partner_id.id,
            "vendor_order_id": vendor_order.id,
            "currency_id": vendor_order.currency_id.id,
            "line_ids": [(0, 0, {
                "vendor_line_id": line.id,
                "product_id": line.product_id.id,
                "name": line.name,
                "quantity": line.product_uom_qty,
                "product_uom": line.product_uom.id,
                "price_unit": line.price_unit,
                "discount": line.discount,
                "tax_ids": [(6, 0, line.tax_ids.ids)],
            }) for line in vendor_order.line_ids],
        })
        vendor_order.invoice_id = invoice
        return invoice

    @api.model
    def _create_credit_note_for_return(self, vendor_return):
        if vendor_return.credit_note_id:
            return vendor_return.credit_note_id
        order = vendor_return.vendor_order_id
        vendor = order.vendor_id.commercial_partner_id
        credit_note = self.sudo().create({
            "name": self._next_name(vendor, "out_refund"),
            "move_type": "out_refund",
            "vendor_id": vendor.id,
            "partner_id": order.partner_id.id,
            "vendor_order_id": order.id,
            "return_id": vendor_return.id,
            "reversed_invoice_id": order.invoice_id.id,
            "currency_id": order.currency_id.id,
            "line_ids": [(0, 0, {
                "vendor_line_id": rl.vendor_line_id.id,
                "product_id": rl.product_id.id,
                "name": rl.vendor_line_id.name,
                "quantity": rl.quantity,
                "product_uom": rl.vendor_line_id.product_uom.id,
                "price_unit": rl.vendor_line_id.price_unit,
                "discount": rl.vendor_line_id.discount,
                "tax_ids": [(6, 0, rl.vendor_line_id.tax_ids.ids)],
            }) for rl in vendor_return.line_ids],
        })
        vendor_return.credit_note_id = credit_note
        return credit_note

    @api.model
    def _create_credit_note_for_cancellation(self, vendor_order, lines_qty):
        """Credit note for a cancelled remainder: ``lines_qty`` = {vendor order line: qty}.

        Numbered like a return credit note (``<code>/RINV/<year>/<seq>``) and
        pointing at the vendor order's invoice, but with no return behind it.
        """
        lines_qty = {line: qty for line, qty in lines_qty.items() if qty}
        if not lines_qty:
            return self.browse()
        vendor = vendor_order.vendor_id.commercial_partner_id
        return self.sudo().create({
            "name": self._next_name(vendor, "out_refund"),
            "move_type": "out_refund",
            "vendor_id": vendor.id,
            "partner_id": vendor_order.partner_id.id,
            "vendor_order_id": vendor_order.id,
            "reversed_invoice_id": vendor_order.invoice_id.id,
            "currency_id": vendor_order.currency_id.id,
            "line_ids": [(0, 0, {
                "vendor_line_id": line.id,
                "product_id": line.product_id.id,
                "name": line.name,
                "quantity": qty,
                "product_uom": line.product_uom.id,
                "price_unit": line.price_unit,
                "discount": line.discount,
                "tax_ids": [(6, 0, line.tax_ids.ids)],
            }) for line, qty in lines_qty.items()],
        })

    def _get_report_base_filename(self):
        self.ensure_one()
        label = "Vendor Credit Note" if self.move_type == "out_refund" else "Vendor Invoice"
        return f"{label} {self.name}".replace("/", "-")

    def action_print(self):
        return self.env.ref("leaders_marketplace.action_report_vendor_invoice").report_action(self)


class VendorInvoiceLine(models.Model):
    _name = "leaders.vendor.invoice.line"
    _description = "Vendor Invoice Line"
    _order = "invoice_id, id"

    invoice_id = fields.Many2one("leaders.vendor.invoice", required=True, ondelete="cascade", index=True)
    currency_id = fields.Many2one(related="invoice_id.currency_id", store=True)
    vendor_id = fields.Many2one(related="invoice_id.vendor_id", store=True, index=True)
    vendor_line_id = fields.Many2one("leaders.vendor.order.line")
    product_id = fields.Many2one("product.product")
    name = fields.Text(required=True)
    quantity = fields.Float(digits="Product Unit of Measure", default=1.0)
    product_uom = fields.Many2one("uom.uom", string="Unit")
    price_unit = fields.Float(digits="Product Price")
    discount = fields.Float(digits="Discount")
    tax_ids = fields.Many2many("account.tax", string="Taxes")
    price_subtotal = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    price_total = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")

    @api.depends("quantity", "price_unit", "discount", "tax_ids", "currency_id")
    def _compute_amounts(self):
        for line in self:
            price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
            taxes = line.tax_ids.compute_all(
                price, line.currency_id, line.quantity,
                product=line.product_id, partner=line.invoice_id.partner_id)
            line.price_subtotal = taxes["total_excluded"]
            line.price_total = taxes["total_included"]
