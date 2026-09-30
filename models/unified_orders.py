"""Unified "My orders" (TASK 29 / D1).

A *checkout* is one website ``sale.order``. Behind it there are up to N+1 parts:
the company part (the sale order itself, unless it is a vendor-only checkout that
was cancelled after the split) and one ``leaders.vendor.order`` per seller.

``sale.order._leaders_overall_status()`` folds the state of every part into ONE
status the customer sees. State machine (documented in README / D1 notes):

Part codes (one per part):

* vendor order: pending -> ``placed``; confirmed -> ``confirmed``; processing ->
  ``processing``; partially_fulfilled -> ``partially_shipped``; fulfilled ->
  ``shipped``; completed -> ``delivered``; cancelled -> ``cancelled``. When every
  product line of the order was returned (received/refunded) the code is
  ``returned``.
* company part (sale order): draft/sent -> ``placed``; cancel -> ``cancelled``;
  sale/done with outgoing deliveries: none done -> ``confirmed`` (``processing``
  once a delivery is reserved/ready), some done -> ``partially_shipped``, all done
  -> ``delivered``; without any delivery (services): ``confirmed`` (``delivered``
  once the order is locked/done).

Overall status of the checkout (order of the rules matters):

1. no part at all -> ``placed``;
2. every part cancelled -> ``cancelled``;
3. some parts cancelled and others alive -> ``partially_cancelled`` (the progress
   of the living parts is still available as ``progress``);
4. every living part ``returned`` -> ``returned``;
5. every living part delivered/returned -> ``delivered``;
6. every living part shipped/delivered/returned -> ``shipped``;
7. any living part partially shipped, shipped or delivered -> ``partially_shipped``;
8. any living part ``processing`` -> ``processing``;
9. any living part ``confirmed`` -> ``confirmed``; else ``placed``.

The status is NOT stored: it is computed for one customer's few checkouts.
"""
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare

PARAM_UNIFIED = "leaders_marketplace.unified_customer_orders"

CHECKOUT_STATUSES = (
    "placed", "confirmed", "processing", "partially_shipped", "shipped", "delivered",
    "returned", "partially_cancelled", "cancelled",
)
_VENDOR_CODES = {
    "pending": "placed", "confirmed": "confirmed", "processing": "processing",
    "partially_fulfilled": "partially_shipped", "fulfilled": "shipped",
    "completed": "delivered", "cancelled": "cancelled",
}
_SHIPPED_OR_MORE = ("shipped", "delivered", "returned")
_STARTED_SHIPPING = ("partially_shipped", "shipped", "delivered", "returned")


def status_labels():
    """{code: translated label} of the overall / part statuses."""
    return {
        "placed": _("Placed"),
        "confirmed": _("Confirmed"),
        "processing": _("Processing"),
        "partially_shipped": _("Partially shipped"),
        "shipped": _("Shipped"),
        "delivered": _("Delivered"),
        "returned": _("Returned"),
        "partially_cancelled": _("Partially cancelled"),
        "cancelled": _("Cancelled"),
    }


def aggregate_status(codes):
    """(overall, progress) of a list of part codes. Pure function (see module doc)."""
    codes = list(codes)
    if not codes:
        return "placed", "placed"
    living = [c for c in codes if c != "cancelled"]
    if not living:
        return "cancelled", "cancelled"
    if all(c == "returned" for c in living):
        progress = "returned"
    elif all(c in ("delivered", "returned") for c in living):
        progress = "delivered"
    elif all(c in _SHIPPED_OR_MORE for c in living):
        progress = "shipped"
    elif any(c in _STARTED_SHIPPING for c in living):
        progress = "partially_shipped"
    elif "processing" in living:
        progress = "processing"
    elif "confirmed" in living:
        progress = "confirmed"
    else:
        progress = "placed"
    overall = "partially_cancelled" if len(living) < len(codes) else progress
    return overall, progress


class SaleOrder(models.Model):
    _inherit = "sale.order"

    # ------------------------------------------------------------------ setting
    @api.model
    def _leaders_unified_orders_enabled(self):
        return self.env["ir.config_parameter"].sudo().get_param(PARAM_UNIFIED) in ("True", "true", "1")

    # ------------------------------------------------------------------ scope
    @api.model
    def _leaders_checkout_domain(self, partner):
        """Checkouts of a customer: confirmed orders, or cancelled ones that were real checkouts."""
        return [
            ("partner_id", "child_of", partner.commercial_partner_id.id),
            "|", ("state", "in", ("sale", "done")),
            "&", ("state", "=", "cancel"),
            "|", ("vendor_order_ids", "!=", False), ("leaders_cancelled_by_type", "!=", False),
        ]

    def _leaders_is_checkout_of(self, partner):
        """True when the order is one of the customer's checkouts (same rule as the list)."""
        self.ensure_one()
        return bool(self.sudo().search_count(
            [("id", "=", self.id)] + self._leaders_checkout_domain(partner)))

    # ------------------------------------------------------------------ parts
    def _leaders_has_company_part(self):
        self.ensure_one()
        order = self.sudo()
        return not order.marketplace_vendor_only and order.state != "draft" and bool(
            order._marketplace_company_lines())

    def _leaders_company_part_code(self):
        self.ensure_one()
        order = self.sudo()
        if order.state == "cancel":
            return "cancelled"
        if order.state in ("draft", "sent"):
            return "placed"
        pickings = order.picking_ids.filtered(
            lambda p: p.state != "cancel" and p.picking_type_code == "outgoing")
        if not pickings:
            return "delivered" if order.state == "done" else "confirmed"
        done = pickings.filtered(lambda p: p.state == "done")
        if done and len(done) == len(pickings):
            return "delivered"
        if done:
            return "partially_shipped"
        return "processing" if pickings.filtered(lambda p: p.state == "assigned") else "confirmed"

    def _leaders_part_codes(self):
        """Codes of every part of the checkout (company part first, then each seller)."""
        self.ensure_one()
        order = self.sudo()
        codes = []
        if order._leaders_has_company_part():
            codes.append(order._leaders_company_part_code())
        codes.extend(v._leaders_part_code() for v in order.vendor_order_ids)
        return codes

    def _leaders_status_info(self):
        """{'code', 'label', 'progress', 'progress_label'} of the checkout."""
        self.ensure_one()
        overall, progress = aggregate_status(self._leaders_part_codes())
        labels = status_labels()
        return {"code": overall, "label": labels[overall],
                "progress": progress, "progress_label": labels[progress]}

    def _leaders_overall_status(self):
        """``(code, label)`` of the whole checkout, from ALL its parts."""
        info = self._leaders_status_info()
        return info["code"], info["label"]

    def _leaders_checkout_amount(self):
        """Total shown in the list: everything the customer paid at checkout."""
        self.ensure_one()
        order = self.sudo()
        if order.vendor_order_ids:
            return order._marketplace_checkout_total()
        return order.amount_total

    def _leaders_payment_method_label(self):
        self.ensure_one()
        method = self.sudo().leaders_payment_method
        return dict(self._fields["leaders_payment_method"]._description_selection(self.env)).get(method, "")

    def _leaders_company_part_info(self):
        """What the detail page shows for the company part."""
        self.ensure_one()
        order = self.sudo()
        pickings = []
        for picking in order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing"):
            pickings.append({
                "name": picking.name,
                "state": dict(picking._fields["state"]._description_selection(picking.env)).get(picking.state),
                "tracking_ref": picking.carrier_tracking_ref if "carrier_tracking_ref" in picking._fields else False,
                "tracking_url": picking.carrier_tracking_url if "carrier_tracking_url" in picking._fields else False,
                "date_done": picking.date_done,
            })
        invoices = [{
            "name": inv.name, "url": "%s?report_type=pdf" % inv.access_url,
        } for inv in order.invoice_ids.filtered(
            lambda m: m.state == "posted" and m.move_type == "out_invoice")]
        code = order._leaders_company_part_code()
        return {
            "code": code,
            "label": status_labels()[code],
            "lines": order._marketplace_company_lines(),
            "pickings": pickings,
            "invoices": invoices,
            # The standard sale portal page of the company order.
            "order_url": order.access_url if order.state in ("sale", "done") else False,
        }

    def _leaders_checkout_dates(self):
        """(ordered, shipped, delivered) dates for the checkout timeline."""
        self.ensure_one()
        order = self.sudo()
        shipped = [d for d in order.vendor_order_ids.mapped("date_shipped") if d]
        done = order.picking_ids.filtered(
            lambda p: p.state == "done" and p.picking_type_code == "outgoing").mapped("date_done")
        shipped += [d for d in done if d]
        delivered = [d for d in order.vendor_order_ids.fulfillment_group_ids.mapped("delivered_date") if d]
        return order.date_order, (min(shipped) if shipped else False), (max(delivered) if delivered else False)

    def _leaders_cancel_date(self):
        self.ensure_one()
        order = self.sudo()
        dates = [d for d in order.vendor_order_ids.mapped("cancel_date") if d]
        if order.leaders_cancel_date:
            dates.append(order.leaders_cancel_date)
        return max(dates) if dates else False

    # ------------------------------------------------------------------ actions
    def _leaders_checkout_actions(self):
        """Actions allowed now on the whole checkout (same predicates as the order page)."""
        self.ensure_one()
        actions = []
        living = self.sudo().vendor_order_ids.filtered(lambda v: v.state != "cancelled")
        for vorder in living:
            if vorder._customer_cancel_scope_status("all") is None:
                actions.append({
                    "key": "cancel_all", "label": _("Cancel entire order"),
                    "url": "/my/marketplace/orders/%s#lm_cancel_all_form" % vorder.id,
                })
                break
        return actions

    # ------------------------------------------------------------------ buy again
    def _leaders_buy_again_items(self):
        """[(product, qty, is_vendor_line)] of the checkout: company and vendor lines, no delivery."""
        self.ensure_one()
        order = self.sudo()
        items = {}
        for line in order._marketplace_company_lines():
            key = (line.product_id.id, False)
            items[key] = items.get(key, 0.0) + line.product_uom_qty
        for vorder in order.vendor_order_ids:
            for line in vorder.line_ids.filtered(lambda l: not l.is_delivery):
                key = (line.product_id.id, True)
                items[key] = items.get(key, 0.0) + line.product_uom_qty
        Product = self.env["product.product"].sudo()
        return [(Product.browse(pid), qty, vendor) for (pid, vendor), qty in items.items()]

    def _leaders_buy_again(self, cart):
        """Put this checkout's items in ``cart`` again through the standard cart update.

        The cart re-prices every line with the CURRENT winning offer (and the
        checkout re-validates the prices once more), so a product whose offer
        is gone, an archived / unpublished product, or one out of stock is
        skipped. Returns ``(added, skipped)`` where ``added`` is a list of
        ``(product, qty)`` and ``skipped`` a list of ``(product, reason)``.
        """
        self.ensure_one()
        Offer = self.env["leaders.marketplace.offer"]
        added, skipped = [], []
        for product, qty, vendor_line in self._leaders_buy_again_items():
            if not product.exists() or not product.active:
                skipped.append((product, _("no longer available")))
                continue
            if vendor_line and not Offer._get_marketplace_winner(product):
                skipped.append((product, _("no seller offers it right now")))
                continue
            try:
                with self.env.cr.savepoint():
                    result = cart._cart_update(product_id=product.id, add_qty=qty)
            except UserError:
                skipped.append((product, _("no longer available")))
                continue
            got = result.get("quantity", 0) if result else 0
            if got <= 0:
                skipped.append((product, _("out of stock")))
                continue
            added.append((product, qty))
        return added, skipped


class VendorOrder(models.Model):
    _inherit = "leaders.vendor.order"

    def _leaders_part_code(self):
        """Part code of this seller's order (see the module docstring)."""
        self.ensure_one()
        order = self.sudo()
        if order.state == "cancelled":
            return "cancelled"
        lines = order.line_ids.filtered(lambda l: not l.is_delivery)
        if lines and order.state != "pending" and all(
                float_compare(l.qty_returned, l.product_uom_qty - l.qty_cancelled, precision_digits=4) >= 0 for l in lines):
            return "returned"
        return _VENDOR_CODES.get(order.state, "placed")

    def _leaders_part_status(self):
        code = self._leaders_part_code()
        return code, status_labels()[code]

    def _customer_returnable_lines(self):
        """[(line, qty)] the customer may ask to return now (window and state rules)."""
        self.ensure_one()
        window = int(self.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.return_window_days", 14) or 0)
        returnable = [(line, line._qty_returnable()) for line in self.line_ids.filtered(
            lambda l: not l.is_delivery and l._qty_returnable() > 0)]
        in_window = bool(self.date_shipped) and (
            not window or fields.Datetime.now() <= self.date_shipped + timedelta(days=window))
        if in_window and self.state in ("fulfilled", "completed", "partially_fulfilled"):
            return returnable
        return []

    def _customer_rating_info(self):
        """(can_rate, rating record or False) of the customer's rating form."""
        self.ensure_one()
        catalog_on = self.env["res.partner"]._marketplace_catalog_enabled()
        rating = self.env["rating.rating"].sudo().search([
            ("res_model", "=", "leaders.vendor.order"), ("res_id", "=", self.id),
            ("consumed", "=", True)], limit=1) if catalog_on else False
        return bool(catalog_on and self.state == "completed" and not rating), rating

    def _customer_action_chips(self):
        """Actions the customer can take on this seller's part now (link to the order page)."""
        self.ensure_one()
        base = "/my/marketplace/orders/%s" % self.id
        chips = []
        if self._customer_cancel_scope_status("vendor") is None:
            chips.append({"key": "cancel", "label": _("Cancel this part"), "url": base + "#lm_cancel_card"})
        if self._customer_returnable_lines():
            chips.append({"key": "return", "label": _("Request a return"), "url": base + "#lm_return_card"})
        if self.state != "cancelled":
            chips.append({"key": "report", "label": _("Report a problem"), "url": base + "#lm_report_problem"})
        if self._customer_rating_info()[0]:
            chips.append({"key": "rate", "label": _("Rate"), "url": base + "#lm_rate_form"})
        if self.fulfillment_group_ids.filtered(lambda g: g.state == "delivery_reported"):
            chips.append({"key": "confirm_delivery", "label": _("Confirm delivery"), "url": base})
        return chips
