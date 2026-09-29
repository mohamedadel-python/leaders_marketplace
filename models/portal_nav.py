import re

from odoo import _, api, models

PARAM_STANDARD_ENTRIES = "leaders_marketplace.portal_show_standard_entries"
STANDARD_MODES = ("only_if_documents", "always", "never")
# Odoo's own "configuration" cards a customer always keeps (no documents behind them).
KEPT_STANDARD_URLS = ("/my/security",)


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
    def _customer_entries(self):
        """Cards of the customer's /my home (marketplace entries)."""
        entries = [
            {"key": "orders", "url": "/my/marketplace/orders", "route_exists": True,
             "title": _("My orders"), "text": _("Follow your orders, deliveries and invoices")},
            {"key": "returns", "url": "/my/marketplace/returns", "route_exists": True,
             "title": _("Returns & disputes"), "text": _("Your return requests and disputes")},
            # Odoo 18 has no "/my/addresses" page: addresses are edited on the
            # account page, so there is no separate entry until it exists.
            {"key": "addresses", "url": "/my/addresses", "route_exists": False,
             "title": _("Addresses"), "text": _("Your delivery and billing addresses")},
            {"key": "account", "url": "/my/account", "route_exists": True,
             "title": _("Account details"), "text": _("Your contact and address information")},
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
            {"key": "returns", "url": "/my/vendor/returns", "icon": "fa-undo", "label": _("Returns")},
            {"key": "disputes", "url": "/my/vendor/disputes", "icon": "fa-gavel", "label": _("Disputes")},
            # Questions (product Q&A) is not built yet: flip route_exists when it is.
            {"key": "questions", "url": "/my/vendor/questions", "icon": "fa-question-circle",
             "label": _("Questions"), "route_exists": False},
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
            "purchases_url": "/my/marketplace/orders",
        }
