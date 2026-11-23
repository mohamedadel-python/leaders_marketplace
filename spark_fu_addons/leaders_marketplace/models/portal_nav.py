import json
import re

from odoo import _, api, models
from odoo.http import request

PARAM_STANDARD_ENTRIES = "leaders_marketplace.portal_show_standard_entries"
STANDARD_MODES = ("only_if_documents", "always", "never")
# Odoo's own "configuration" cards a customer always keeps (no documents behind them).
KEPT_STANDARD_URLS = ("/my/security",)

# ONE colour map for every state the portal shows (TASK 29 / C2). The value is the
# "tone" of the badge: the SCSS maps each tone to theme colours (.lm-badge--<tone>).
# Selection values shared by several models (approved, pending, done ...) share the tone.
STATE_TONES = {
    # waiting for somebody
    "pending": "warning", "requested": "warning", "submitted": "warning", "under_review": "warning", "new": "warning",
    "pending_approval": "warning", "hold": "warning", "held": "warning", "open": "warning",
    "not_paid": "warning", "vendor_reply": "warning", "delivery_reported": "warning",
    "returned": "warning", "late": "danger", "draft": "secondary",
    "placed": "info", "partially_cancelled": "warning",
    # in progress
    "confirmed": "info", "processing": "info", "inProgress": "info", "executing": "info", "reviewing": "info", "contacting": "info", "contacted": "info", "offered": "primary", "allocated": "info", "picking": "info",
    "packing": "info", "validating": "info", "scheduled": "info", "created": "info",
    "picked_up": "info", "in_transit": "info", "uploaded": "secondary", "not_started": "secondary",
    # moving on / nearly done
    "partially_fulfilled": "primary", "fulfilled": "primary", "partially_shipped": "primary",
    "shipped": "primary", "ready": "primary", "received": "primary", "available": "primary",
    "validated": "primary", "released": "primary",
    # good
    "completed": "success", "delivered": "success", "done": "success", "active": "success",
    "published": "success", "verified": "success", "refunded": "success", "resolved": "success",
    "paid": "success", "exchanged": "success", "settled": "success", "posted": "success", "valid": "success",
    "applied": "success", "signed": "success", "lifted": "success", "approved": "success",
    # bad
    "cancelled": "danger", "cancel": "danger", "rejected": "danger", "failed": "danger",
    "escalated": "danger", "refused": "danger", "banned": "danger", "invalid": "danger",
    "expired": "danger",
    # stopped
    "suspended": "dark", "frozen": "dark",
    # penalty levels
    "warning": "warning", "freeze": "dark", "ban": "danger",
    # product questions
    "answered": "success", "hidden": "dark",
    # neutral
    "closed": "secondary", "none": "secondary", "consumed": "secondary",
    "used": "secondary", "superseded": "secondary",
    # equipment requests, rentals and assets, push messages
    "confirming": "info", "awaitingPayment": "warning", "reserved": "info", "with_customer": "primary",
    "rented": "primary", "inspection": "warning", "maintenance": "warning", "out_of_service": "dark",
    "sent": "success",
}
TIMELINE_KEYS = ("ordered", "confirmed", "shipped", "delivered")


FORM_STATE_SKIPPED = ("csrf_token", "password")  # never handed back to the page


class LeadersPortalNav(models.AbstractModel):
    """Data-driven navigation of the customer and vendor portal areas.

    One place decides which entries exist, so the vendor layout, the customer
    home and the tests share the same rules. An entry is a dict; ``visible``
    already folds in the permission (owner only) and the feature switch, and
    ``route_exists`` is False for pages planned but not built yet (a later item
    only flips that flag to publish the entry: no dead link is ever rendered).
    """
    _name = "leaders.portal.nav"
    _description = "Marketplace Portal Navigation"

    # ------------------------------------------------------------------
    # Settings and installed-module probes
    # ------------------------------------------------------------------
    @api.model
    def _standard_entries_mode(self):
        value = self.env["ir.config_parameter"].sudo().get_param(PARAM_STANDARD_ENTRIES)
        return value if value in STANDARD_MODES else "only_if_documents"

    @api.model
    def _module_installed(self, name):
        return bool(self.env["ir.module.module"].sudo().search_count([
            ("name", "=", name), ("state", "=", "installed")]))

    @api.model
    def _docs_entry_flags(self, own, url, has_counter, show_count=False):
        """(show, always_visible) of one ``portal.portal_docs_entry`` card.

        ``own`` marks the marketplace's own cards, never hidden by the setting.
        The default mode keeps Odoo's native rule (a counter card appears once
        its counter is above zero) and drops standard cards with no documents
        behind them; ``always`` shows every card even with a zero counter;
        ``never`` removes the standard ones.
        """
        if own or url in KEPT_STANDARD_URLS:
            return True, False
        mode = self._standard_entries_mode()
        if mode == "never":
            return False, False
        if mode == "always":
            return True, bool(has_counter and not show_count)
        return bool(has_counter), False

    # ------------------------------------------------------------------
    # Customer home
    # ------------------------------------------------------------------
    @api.model
    def _customer_orders_url(self):
        """Where "My orders" leads: the unified checkout list when the switch is on."""
        if self.env["sale.order"]._leaders_unified_orders_enabled():
            return "/my/marketplace/checkouts"
        return "/my/marketplace/orders"

    @api.model
    def _customer_entries(self):
        """Cards of the customer's /my home (marketplace entries)."""
        entries = [
            {"key": "orders", "url": self._customer_orders_url(), "route_exists": True,
             "title": _("My orders"), "text": _("Follow your orders, deliveries and invoices")},
            {"key": "returns", "url": "/my/marketplace/returns", "route_exists": True,
             "title": _("Returns & disputes"), "text": _("Your return requests and disputes")},
            # Odoo 18 has no "/my/addresses" page: addresses are edited on the
            # account page, so there is no separate entry until it exists.
            {"key": "addresses", "url": "/my/addresses", "route_exists": False,
             "title": _("Addresses"), "text": _("Your delivery and billing addresses")},
            {"key": "account", "url": "/my/account", "route_exists": True,
             "title": _("Account details"), "text": _("Your contact and address information")},
            {"key": "notifications", "url": "/my/marketplace/notifications", "route_exists": True,
             "title": _("Notification preferences"), "text": _("Choose which emails you receive")},
            {"key": "wishlist", "url": "/shop/wishlist", "route_exists": True,
             "visible": self._module_installed("website_sale_wishlist"),
             "title": _("Wishlist"), "text": _("Products you saved for later")},
        ]
        return [e for e in entries if e["route_exists"] and e.get("visible", True)]

    # ------------------------------------------------------------------
    # Vendor layout
    # ------------------------------------------------------------------
    @api.model
    def _vendor_entries(self, is_owner):
        Import = self.env["leaders.vendor.import"]
        ApiKey = self.env["leaders.vendor.api.key"]
        entries = [
            {"key": "dashboard", "url": "/my/vendor", "icon": "fa-tachometer", "label": _("Dashboard"),
             "exact": True},
            {"key": "products", "url": "/my/vendor/products", "icon": "fa-cube", "label": _("Products"),
             "prefixes": ("/my/vendor/products", "/my/vendor/templates")},
            {"key": "import", "url": "/my/vendor/import", "icon": "fa-upload", "label": _("Import"),
             "enabled": Import._leaders_import_enabled()},
            {"key": "orders", "url": "/my/vendor/orders", "icon": "fa-shopping-cart", "label": _("Orders")},
            {"key": "equipment_requests", "url": "/my/vendor/equipment-requests", "icon": "fa-truck",
             "label": _("Equipment requests")},
            {"key": "own_services", "url": "/my/vendor/own-services", "icon": "fa-wrench", "label": _("My services"),
             "prefixes": ("/my/vendor/own-services", "/my/vendor/provide-services")},
            {"key": "returns", "url": "/my/vendor/returns", "icon": "fa-undo", "label": _("Returns")},
            {"key": "rentals", "url": "/my/vendor/rentals", "icon": "fa-calendar", "label": _("Rentals")},
            {"key": "contract", "url": "/my/vendor/contract", "icon": "fa-handshake-o", "label": _("Contract model")},
            {"key": "disputes", "url": "/my/vendor/disputes", "icon": "fa-gavel", "label": _("Disputes")},
            # Product Q&A (D3): only while its master switch is on.
            {"key": "questions", "url": "/my/vendor/questions", "icon": "fa-question-circle",
             "label": _("Questions"), "enabled": self.env["leaders.product.question"]._leaders_enabled()},
            {"key": "performance", "url": "/my/vendor/performance", "icon": "fa-line-chart",
             "label": _("Performance")},
            # Owner-only, mirroring what the routes enforce.
            {"key": "finance", "url": "/my/vendor/finance", "icon": "fa-money", "label": _("Finance"),
             "owner_only": True},
            {"key": "zones", "url": "/my/vendor/delivery-zones", "icon": "fa-map-marker",
             "label": _("Delivery Zones"), "owner_only": True},
            {"key": "staff", "url": "/my/vendor/staff", "icon": "fa-users", "label": _("Staff"),
             "owner_only": True},
            {"key": "api", "url": "/my/vendor/api", "icon": "fa-key", "label": _("API"),
             "owner_only": True, "enabled": ApiKey._leaders_api_enabled()},
            {"key": "profile", "url": "/my/vendor/profile", "icon": "fa-id-card-o", "label": _("Profile"),
             "owner_only": True},
        ]
        result = []
        for entry in entries:
            if not entry.get("route_exists", True) or not entry.get("enabled", True):
                continue
            if entry.get("owner_only") and not is_owner:
                continue
            entry.setdefault("prefixes", (entry["url"],))
            result.append(entry)
        return result

    _DETAIL_LABELS = (
        (r"^/my/vendor/products/new", lambda s: _("New product")),
        (r"^/my/vendor/products/sell-existing", lambda s: _("Sell an existing product")),
        (r"^/my/vendor/products/\d+", lambda s: _("Edit product")),
        (r"^/my/vendor/templates/\d+/variants", lambda s: _("Variants")),
        (r"^/my/vendor/orders/\d+", lambda s: _("Order details")),
        (r"^/my/vendor/equipment-requests/\d+", lambda s: _("Request details")),
        (r"^/my/vendor/own-services/new", lambda s: _("New service")),
        (r"^/my/vendor/own-services/\d+", lambda s: _("Service details")),
        (r"^/my/vendor/returns/\d+", lambda s: _("Return details")),
        (r"^/my/vendor/disputes/\d+", lambda s: _("Dispute details")),
        (r"^/my/vendor/import/\d+", lambda s: _("Import job")),
        (r"^/my/vendor/finance/statement", lambda s: _("Statement of account")),
        (r"^/my/vendor/finance/settlement", lambda s: _("Settlement")),
        (r"^/my/vendor/delivery-zones/", lambda s: _("Delivery zone")),
    )

    @api.model
    def _vendor_nav(self, path, is_owner=None):
        """Everything the vendor layout needs, from one call.

        Returns ``entries`` (each with ``active``), ``disabled`` (the owner
        must still accept the terms: nothing else is reachable), ``crumbs``
        (list of (label, url or None)) and the customer-area link.
        """
        user = self.env.user
        if is_owner is None:
            is_owner = user.has_group("leaders_marketplace.group_vendor_owner")
        vendor = user.partner_id.commercial_partner_id
        entries = self._vendor_entries(is_owner)
        current = None
        best = -1
        for entry in entries:
            if entry.get("exact"):
                matched = path.rstrip("/") == entry["url"]
                length = len(entry["url"])
            else:
                lengths = [len(p) for p in entry["prefixes"]
                           if path == p or path.startswith(p + "/")]
                matched = bool(lengths)
                length = max(lengths) if lengths else 0
            if matched and length > best:
                current, best = entry, length
        for entry in entries:
            entry["active"] = entry is current
        pending = self.env["leaders.vendor.terms"].sudo()._pending_for(vendor)
        crumbs = [(_("Home"), "/my/home"), (_("Vendor"), "/my/vendor")]
        if path.rstrip("/") == "/my/vendor":
            crumbs[-1] = (_("Vendor"), None)
        elif path.startswith("/my/vendor/terms"):
            crumbs.append((_("Vendor terms"), None))
        else:
            detail = None
            for pattern, label in self._DETAIL_LABELS:
                if re.match(pattern, path):
                    detail = label(self)
                    break
            if current:
                # A detail page keeps the section as a link; the section itself is the leaf.
                crumbs.append((current["label"], current["url"] if detail else None))
            if detail:
                crumbs.append((detail, None))
        return {
            "entries": entries,
            "disabled": bool(pending),
            "crumbs": crumbs,
            "is_owner": is_owner,
            "purchases_url": self._customer_orders_url(),
        }

    # ------------------------------------------------------------------
    # Shared UI components (state badge, order timeline)
    # ------------------------------------------------------------------
    @api.model
    def _state_badge(self, record, field="state"):
        """{'tone', 'label', 'value'} of a record's state, for ``lm_state_badge``."""
        value = record[field] if record and field in record._fields else False
        label = dict(record._fields[field]._description_selection(record.env)).get(value) if value else ""
        return {"tone": STATE_TONES.get(value, "secondary"), "label": label or "", "value": value or ""}

    @api.model
    def _code_badge(self, code, label):
        """Badge data for a computed status code (checkout overall status)."""
        return {"tone": STATE_TONES.get(code, "secondary"), "label": label or "", "value": code or ""}

    @api.model
    def _timeline(self, record):
        """Steps of the order timeline (ordered > confirmed > shipped > delivered).

        ``record`` is a ``leaders.vendor.order`` or a ``leaders.fulfillment.group``.
        Returns {'steps': [{key, label, status, date}], 'terminal': {key, label, icon, tone} | False}
        where status is done / current / todo. A cancelled record freezes the
        steps reached so far and adds a red "Cancelled" branch; a returned one
        keeps every step done and adds an amber "Returned" branch.
        """
        labels = {
            "ordered": _("Ordered"), "confirmed": _("Confirmed"),
            "shipped": _("Shipped"), "delivered": _("Delivered"),
        }
        dates = dict.fromkeys(TIMELINE_KEYS, False)
        terminal = False
        if record._name == "sale.order":
            # A checkout: the progress of its living parts; cancelled = every part cancelled.
            info = record._leaders_status_info()
            ordered, shipped, delivered = record._leaders_checkout_dates()
            dates.update(ordered=ordered, shipped=shipped, delivered=delivered)
            stage = {"placed": 0, "confirmed": 1, "processing": 1, "partially_shipped": 2,
                     "shipped": 2, "delivered": 3, "returned": 3, "cancelled": 1}.get(info["progress"], 0)
            cancelled = info["code"] == "cancelled"
            returned = info["progress"] == "returned" and not cancelled
            cancel_date = record._leaders_cancel_date()
        elif record._name == "leaders.fulfillment.group":
            stage = {
                "allocated": 1, "picking": 1, "packing": 1, "ready": 1,
                "partially_shipped": 2, "shipped": 2, "delivery_reported": 2,
                "delivered": 3, "cancelled": 1,
            }.get(record.state, 1)
            dates["ordered"] = record.vendor_order_id.date_order
            dates["delivered"] = record.delivered_date
            cancelled = record.state == "cancelled"
            returned = record.shipping_status in ("returned", "refused")
            cancel_date = False
        else:
            stage = {
                "pending": 0, "confirmed": 1, "processing": 1, "partially_fulfilled": 2,
                "fulfilled": 2, "completed": 3, "cancelled": 0,
            }.get(record.state, 0)
            dates["ordered"] = record.date_order
            dates["shipped"] = record.date_shipped
            cancelled = record.state == "cancelled"
            cancel_date = record.cancel_date
            returned = bool(record.return_ids.filtered(lambda r: r.state in ("received", "refunded")))
        if cancelled:
            terminal = {"key": "cancelled", "label": _("Cancelled"), "icon": "fa-times", "tone": "danger",
                        "date": cancel_date}
        elif returned:
            stage = 3
            terminal = {"key": "returned", "label": _("Returned"), "icon": "fa-undo", "tone": "warning",
                        "date": False}
        steps = []
        for index, key in enumerate(TIMELINE_KEYS):
            if cancelled:
                status = "done" if index <= stage else "todo"
            elif index < stage or (index == stage and (key == "delivered" or returned)):
                status = "done"
            elif index == stage:
                status = "current"
            else:
                status = "todo"
            steps.append({"key": key, "label": labels[key], "status": status, "date": dates[key]})
        return {"steps": steps, "terminal": terminal}

    @api.model
    def _vendor_form_state(self):
        """What the vendor typed, when a POST was refused and the page is shown again with an error.

        The page puts it in a data attribute and portal_actions.js fills the form back in, so a refused
        save never throws away what was entered. Files and passwords are not kept.
        """
        http = request.httprequest if request else None
        if not http or http.method != "POST":
            return ""
        state = {}
        for key in http.form:
            if any(skipped in key.lower() for skipped in FORM_STATE_SKIPPED):
                continue
            state[key] = [value[:5000] for value in http.form.getlist(key)[:200]]
        text = json.dumps(state)
        return text if len(text) <= 200000 else ""
