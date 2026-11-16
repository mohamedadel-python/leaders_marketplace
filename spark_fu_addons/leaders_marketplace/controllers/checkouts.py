"""Unified "My orders": one row per checkout (TASK 29 / D1).

Behind the switch ``leaders_marketplace.unified_customer_orders`` (default off: every
route here is a 404). The heavy forms (cancel, return, report a problem, rate,
confirm delivery) stay on the vendor-order page and keep their own rules; the
checkout page shows the state and the actions allowed now, linking to them.
"""
from datetime import timedelta

from werkzeug.exceptions import NotFound

from odoo import _, fields, http
from urllib.parse import quote

from odoo.exceptions import AccessError, UserError
from odoo.http import request
from odoo.osv import expression
from odoo.addons.portal.controllers.portal import pager as portal_pager

from ..models.unified_orders import CHECKOUT_STATUSES, status_labels

PAGE_SIZE = 10
SORTINGS = ("date", "date_asc", "total")


class MarketplaceCheckouts(http.Controller):

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _require_enabled():
        if not request.env["sale.order"]._leaders_unified_orders_enabled():
            raise NotFound()

    @staticmethod
    def _partner():
        return request.env.user.partner_id.commercial_partner_id

    def _customer_checkout(self, sale_order_id):
        """The customer's checkout: unknown -> 404, somebody else's -> AccessError (403)."""
        Sale = request.env["sale.order"].sudo()
        sale = Sale.browse(sale_order_id).exists()
        if not sale:
            raise NotFound()
        partner = self._partner()
        if not Sale.search_count([("id", "=", sale.id), ("partner_id", "child_of", partner.id)]):
            raise AccessError(_("You cannot access this order."))
        if not sale._leaders_is_checkout_of(partner):
            raise NotFound()
        return sale

    @staticmethod
    def _parse_date(value):
        try:
            return fields.Date.to_date(value) if value else False
        except (TypeError, ValueError):
            return False

    def _list_domain(self, partner, search, begin, end):
        Sale = request.env["sale.order"]
        domain = Sale._leaders_checkout_domain(partner)
        if search:
            search_domain = expression.OR([[(f, "ilike", search)] for f in (
                "name", "order_line.name", "vendor_order_ids.name", "vendor_order_ids.vendor_id.name",
                "vendor_order_ids.vendor_id.vendor_store_name", "vendor_order_ids.line_ids.name")])
            domain = expression.AND([domain, search_domain])
        if begin:
            domain.append(("date_order", ">=", fields.Datetime.to_string(begin)))
        if end:
            domain.append(("date_order", "<", fields.Datetime.to_string(end + timedelta(days=1))))
        return domain

    # ------------------------------------------------------------------ list
    @http.route(["/my/marketplace/checkouts", "/my/marketplace/checkouts/page/<int:page>"],
                type="http", auth="user", website=True)
    def customer_checkouts(self, page=1, search="", status="all", date_begin=None, date_end=None,
                           sortby="date", **kw):
        self._require_enabled()
        partner = self._partner()
        search = (search or "").strip()
        begin, end = self._parse_date(date_begin), self._parse_date(date_end)
        if sortby not in SORTINGS:
            sortby = "date"
        if status not in CHECKOUT_STATUSES:
            status = "all"
        order = "date_order asc, id asc" if sortby == "date_asc" else "date_order desc, id desc"
        sales = request.env["sale.order"].sudo().search(
            self._list_domain(partner, search, begin, end), order=order)
        # The overall status is computed (not stored): filter on it in Python.
        rows = []
        for sale in sales:
            info = sale._leaders_status_info()
            if status != "all" and info["code"] != status:
                continue
            rows.append({
                "order": sale, "info": info, "amount": sale._leaders_checkout_amount(),
                "payment": sale._leaders_payment_method_label(),
                "sellers": ", ".join(sorted({
                    v.vendor_id.vendor_store_name or v.vendor_id.name for v in sale.vendor_order_ids})),
            })
        if sortby == "total":
            rows.sort(key=lambda r: (-r["amount"], -r["order"].id))
        total = len(rows)
        url_args = {"search": search or None, "status": None if status == "all" else status,
                    "date_begin": date_begin if begin else None, "date_end": date_end if end else None,
                    "sortby": None if sortby == "date" else sortby}
        url_args = {k: v for k, v in url_args.items() if v}
        pager = portal_pager(url="/my/marketplace/checkouts", url_args=url_args, total=total,
                             page=page, step=PAGE_SIZE)
        labels = status_labels()
        return request.render("leaders_marketplace.portal_customer_checkouts", {
            "rows": rows[pager["offset"]:pager["offset"] + PAGE_SIZE],
            "pager": pager,
            "search": search, "status": status, "sortby": sortby,
            "date_begin": date_begin if begin else "", "date_end": date_end if end else "",
            "status_options": [(code, labels[code]) for code in CHECKOUT_STATUSES],
            "sort_options": [
                ("date", _("Newest first")), ("date_asc", _("Oldest first")), ("total", _("Highest total"))],
            "filtered": bool(search or begin or end or status != "all"),
        })

    # ------------------------------------------------------------------ detail
    @http.route("/my/marketplace/checkouts/<int:sale_order_id>", type="http", auth="user", website=True)
    def customer_checkout_detail(self, sale_order_id, **kw):
        self._require_enabled()
        sale = self._customer_checkout(sale_order_id)
        vendor_parts = []
        for vorder in sale.vendor_order_ids.sorted("id"):
            code, label = vorder._leaders_part_status()
            documents = (vorder.invoice_id | vorder.credit_note_ids).filtered(lambda d: d.state == "posted")
            vendor_parts.append({
                "order": vorder, "code": code, "label": label,
                "chips": vorder._customer_action_chips(),
                "documents": documents,
                "lines": vorder.line_ids.filtered(lambda l: not l.is_delivery),
            })
        return request.render("leaders_marketplace.portal_customer_checkout_detail", {
            "sale": sale,
            "info": sale._leaders_status_info(),
            "amount": sale._leaders_checkout_amount(),
            "payment": sale._leaders_payment_method_label(),
            "company": sale._leaders_company_part_info() if sale._leaders_has_company_part() else False,
            "vendor_parts": vendor_parts,
            "actions": sale._leaders_checkout_actions(),
            "delivery_text": sale._leaders_delivery_breakdown_text(),
            "bought_error": kw.get("error"),
            "cancel": sale._leaders_company_only_cancel_info(),
            "cancelled_now": kw.get("cancelled"),
        })

    # ------------------------------------------------------------------ buy again
    @http.route("/my/marketplace/checkouts/<int:sale_order_id>/buy-again", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def customer_checkout_buy_again(self, sale_order_id, **post):
        self._require_enabled()
        sale = self._customer_checkout(sale_order_id)
        cart = request.website.sale_get_order(force_create=True)
        added, skipped = sale._leaders_buy_again(cart)
        if skipped:
            cart.shop_warning = _(
                "Some items could not be added again: %s",
                ", ".join(_("%(product)s (%(reason)s)", product=p.display_name or _("Unknown product"), reason=r)
                          for p, r in skipped))
        return request.redirect("/shop/cart")

    # ------------------------------------------------------------------ cancel
    @http.route("/my/marketplace/checkouts/<int:sale_order_id>/cancel", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def customer_checkout_cancel(self, sale_order_id, **post):
        """Cancel the whole checkout, also when it only has company products (A2 rules).

        Needs both switches (customer cancellation and unified orders). A checkout
        with vendor orders goes through the same whole-checkout cancel as the
        vendor-order route (scope ``all``), so the result is identical.
        """
        self._require_enabled()
        sale = self._customer_checkout(sale_order_id)
        if not request.env["leaders.vendor.order"]._customer_cancel_enabled():
            raise NotFound()
        url = f"/my/marketplace/checkouts/{sale.id}"
        try:
            with request.env.cr.savepoint():
                sale.action_customer_cancel_checkout(post.get("reason_id"), post.get("note"))
        except UserError as error:
            return request.redirect(f"{url}?error={quote(str(error.args[0]))}")
        return request.redirect(f"{url}?cancelled=all")
