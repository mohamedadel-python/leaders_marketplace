import base64
from datetime import timedelta
from urllib.parse import quote

from werkzeug.exceptions import NotFound

from odoo import fields, http, _
from odoo.http import request
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import email_normalize
from odoo.tools.float_utils import float_compare, float_is_zero
from odoo.tools.mimetypes import guess_mimetype
from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.sale.controllers.portal import CustomerPortal as SaleCustomerPortal


class MarketplacePortal(CustomerPortal):

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    def _vendor_partner(self):
        return request.env.user.partner_id.commercial_partner_id

    def _is_vendor_user(self, allow_frozen=False):
        vendor = self._vendor_partner()
        allowed = ("approved", "frozen") if allow_frozen else ("approved",)
        return bool(vendor.is_marketplace_vendor and vendor.vendor_state in allowed)

    def _is_vendor_owner(self):
        return request.env.user.has_group("leaders_marketplace.group_vendor_owner")

    def _require_vendor(self, allow_frozen=False):
        """The vendor behind the current user, or an AccessError.

        A frozen vendor still has obligations -- orders to ship, returns to
        answer, money to be settled -- so the pages that carry those pass
        ``allow_frozen=True``. Everything that puts new goods on sale stays
        closed until the freeze is lifted. A banned vendor has no active user
        at all, so they never reach here.
        """
        if not self._is_vendor_user(allow_frozen=allow_frozen):
            vendor = self._vendor_partner()
            if vendor.is_marketplace_vendor and vendor.vendor_state == "frozen":
                raise AccessError(_(
                    "Your vendor account is frozen, so you cannot list or change "
                    "products. You can still handle your existing orders and returns."))
            raise AccessError(_("Your account is not an approved marketplace vendor."))
        return self._vendor_partner()

    def _vendor_freeze_notice(self, vendor):
        """Banner text for a frozen vendor, or False."""
        if vendor.vendor_state != "frozen":
            return False
        penalty = request.env["leaders.vendor.penalty"].sudo().search([
            ("vendor_id", "=", vendor.id), ("level", "=", "freeze"), ("state", "=", "applied"),
        ], order="id desc", limit=1)
        return _("Your account is frozen: %s", penalty.reason or _("contact the marketplace."))

    def _offer_owned_by_vendor(self, offer_id, vendor):
        offer = request.env["leaders.marketplace.offer"].sudo().search([
            ("id", "=", offer_id),
            ("vendor_id", "=", vendor.id),
        ], limit=1)
        if not offer:
            raise AccessError(_("You cannot access this offer."))
        return offer

    def _vendor_form_values(self, vendor, offer=None, error=None):
        # A new product is always the vendor's own content; an existing one only
        # while no other seller shares its template.
        owns_content = True
        if offer:
            owns_content = offer.product_tmpl_id.sudo()._leaders_vendor_owns_content(vendor)
        categories = offer.product_tmpl_id.public_categ_ids if offer else \
            request.env["product.public.category"].browse()
        forced_policy = request.env["leaders.fulfillment.policy"].sudo()._policy_for(
            vendor, categories)
        return {
            "vendor": vendor,
            "offer": offer,
            "vendor_owns_content": owns_content,
            "forced_fulfillment": forced_policy.forced_fulfillment_type or False,
            "forced_fulfillment_name": forced_policy.name or "",
            "categories": request.env["product.public.category"].sudo().search([], order="name"),
            "websites": request.env["website"].sudo().search([]),
            "currencies": request.env["res.currency"].sudo().search([("active", "=", True)], order="name"),
            "error": error,
        }

    def _vendor_over_offer_limit(self, vendor):
        tier = vendor.vendor_tier_id
        if not (tier and tier.product_limit):
            return False
        current = request.env["leaders.marketplace.offer"].sudo().search_count([
            ("vendor_id", "=", vendor.id),
            ("active", "=", True),
        ])
        return current >= tier.product_limit

    def _catalog_values(self, vendor, query="", **extra):
        """Catalog search results for "Sell an existing product" (empty when the feature is off)."""
        enabled = request.env["res.partner"]._marketplace_catalog_enabled()
        results = request.env["product.product"].browse()
        query = (query or "").strip()
        if enabled and query:
            results = request.env["product.product"].sudo().search([
                ("name", "ilike", query), ("sale_ok", "=", True),
                ("product_tmpl_id.marketplace_state", "=", "published")], limit=20)
        values = dict(self._vendor_form_values(vendor), **extra)
        values.update({"catalog_enabled": enabled, "catalog_query": query, "catalog_results": results})
        return values

    def _vendor_image_max_bytes(self):
        param = request.env["ir.config_parameter"].sudo().get_param(
            "leaders_marketplace.vendor_image_max_mb", 5)
        try:
            limit = int(param)
        except (TypeError, ValueError):
            limit = 5
        return max(limit, 1) * 1024 * 1024

    def _read_vendor_image(self, image_file):
        """Validate an uploaded product image.

        :return: ``(image_b64, error)`` -- ``(False, False)`` when no file was
            sent, ``(False, message)`` when the upload is refused. The mimetype
            is guessed from the bytes, never from the filename or the
            browser-supplied content type.
        """
        if not image_file or not image_file.filename:
            return False, False
        data = image_file.read()
        if not data:
            return False, _("The uploaded image is empty.")
        max_bytes = self._vendor_image_max_bytes()
        if len(data) > max_bytes:
            return False, _(
                "The image is too large (max %(size)s MB).",
                size=max_bytes // (1024 * 1024))
        if not guess_mimetype(data, default="").startswith("image/"):
            return False, _("Only image files (PNG, JPEG, GIF, WebP) can be uploaded.")
        return base64.b64encode(data), False

    @staticmethod
    def _to_float(value, default=0.0):
        try:
            return float(value or default)
        except (TypeError, ValueError):
            return default

    def _vendor_render(self, template, values):
        """Render a vendor page with the frozen-account banner attached."""
        values.setdefault("freeze_notice", self._vendor_freeze_notice(values["vendor"]))
        return request.render(template, values)

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        # Empty counters means the /my/home page render (not the /my/counters RPC).
        if not counters and self._is_vendor_user():
            vendor = self._vendor_partner()
            vendor_domain = [("vendor_id", "=", vendor.id)]
            env = request.env
            values.update({
                "lm_is_vendor": True,
                "lm_vendor": vendor,
                "lm_is_vendor_owner": self._is_vendor_owner(),
                "lm_offers_count": env["leaders.marketplace.offer"].sudo().search_count(vendor_domain),
                "lm_orders_to_process": env["leaders.vendor.order"].sudo().search_count(
                    vendor_domain + [("state", "in", ("pending", "confirmed", "processing", "partially_fulfilled"))]
                ),
                "lm_returns_open": env["leaders.vendor.return"].sudo().search_count(
                    vendor_domain + [("state", "in", ("requested", "approved", "escalated"))]
                ),
                "lm_disputes_open": env["leaders.vendor.dispute"].sudo().search_count(
                    vendor_domain + [("state", "in", ("open", "vendor_reply", "escalated"))]
                ),
                "lm_staff_count": env["res.users"].sudo().search_count([
                    ("partner_id.commercial_partner_id", "=", vendor.id),
                    ("id", "!=", env.user.id),
                ]),
            })
        return values

    # -------------------------------------------------------------------------
    # Vendor dashboard / portal navigation
    # -------------------------------------------------------------------------
    @http.route("/my/vendor", type="http", auth="user", website=True)
    def vendor_dashboard(self, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        VendorOrder = request.env["leaders.vendor.order"].sudo()
        Offer = request.env["leaders.marketplace.offer"].sudo()
        Return = request.env["leaders.vendor.return"].sudo()
        Dispute = request.env["leaders.vendor.dispute"].sudo()
        vendor_domain = [("vendor_id", "=", vendor.id)]
        offer_states = dict(Offer._fields["state"]._description_selection(request.env))
        offer_state_counts = {
            state: count for state, count in Offer._read_group(vendor_domain, ["state"], ["__count"])
        }
        values = {
            "vendor": vendor,
            "currency": vendor.company_currency_id or request.env.company.currency_id,
            "orders": VendorOrder.search(vendor_domain, limit=8, order="id desc"),
            "stock_offers": Offer.search(vendor_domain + [("active", "=", True)], limit=8, order="id desc"),
            "orders_count": VendorOrder.search_count(vendor_domain),
            "orders_to_process": VendorOrder.search_count(
                vendor_domain + [("state", "in", ("pending", "confirmed", "processing", "partially_fulfilled"))]
            ),
            "offers_count": sum(offer_state_counts.values()),
            "offer_state_counts": [(offer_states[s], s, c) for s, c in offer_state_counts.items()],
            "returns_count": Return.search_count(vendor_domain),
            "returns_open": Return.search_count(vendor_domain + [("state", "in", ("requested", "approved", "escalated"))]),
            "cod_pending_confirmation": VendorOrder.search(vendor_domain + [
                ("payment_method", "=", "cod"), ("state", "=", "completed"), ("cod_cash_confirmed", "=", False),
            ]).filtered(lambda o: o.fulfillment_group_ids and set(o.fulfillment_group_ids.mapped("fulfillment_type")) == {"fbm"}),
            "disputes_count": Dispute.search_count(vendor_domain),
            "disputes_open": Dispute.search_count(vendor_domain + [("state", "in", ("open", "vendor_reply", "escalated"))]),
            "wallet_available": vendor.vendor_wallet_balance,
            "wallet_hold": vendor.vendor_wallet_hold,
            "is_vendor_owner": self._is_vendor_owner(),
        }
        return self._vendor_render("leaders_marketplace.portal_vendor_dashboard", values)

    # -------------------------------------------------------------------------
    # Vendor products / offers
    # -------------------------------------------------------------------------
    @http.route("/my/vendor/products", type="http", auth="user", website=True)
    def vendor_products(self, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        offers = request.env["leaders.marketplace.offer"].sudo().search([
            ("vendor_id", "=", vendor.id)
        ], order="id desc")
        return self._vendor_render("leaders_marketplace.portal_vendor_products", {
            "offers": offers,
            "vendor": vendor,
            "error": kw.get("error"),
        })

    @http.route(["/my/vendor/products/new"], type="http", auth="user", website=True, methods=["GET", "POST"], csrf=True)
    def vendor_product_create(self, **post):
        vendor = self._require_vendor()
        if request.httprequest.method == "GET":
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._catalog_values(vendor, post.get("q")))

        name = (post.get("name") or "").strip()
        seller_sku = (post.get("seller_sku") or "").strip()
        price = self._to_float(post.get("price"))
        fulfillment_type = post.get("fulfillment_type") or "fbm"
        category_id = int(post.get("public_category_id") or 0)
        currency_id = int(post.get("currency_id") or request.env.company.currency_id.id)
        website_ids = [int(x) for x in request.httprequest.form.getlist("market_ids") if x]

        if not name or not seller_sku or price < 0:
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._vendor_form_values(vendor, error=_("Product name, seller SKU and a valid price are required.")),
            )

        if self._vendor_over_offer_limit(vendor):
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._vendor_form_values(vendor, error=_("Your vendor tier product limit has been reached.")),
            )

        image_b64, image_error = self._read_vendor_image(
            request.httprequest.files.get("image_1920"))
        if image_error:
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._vendor_form_values(vendor, error=image_error),
            )

        template_vals = {
            "name": name,
            "description_sale": post.get("description_sale") or False,
            "type": "consu",
            "is_storable": True,
            "sale_ok": True,
            "purchase_ok": False,
            "marketplace_state": "draft",
            # Stamps who may edit this product's content from the portal.
            "marketplace_owner_vendor_id": vendor.id,
        }
        if image_b64:
            template_vals["image_1920"] = image_b64
        if category_id:
            template_vals["public_categ_ids"] = [(6, 0, [category_id])]

        tmpl = request.env["product.template"].sudo().create(template_vals)
        product = tmpl.product_variant_id
        offer = request.env["leaders.marketplace.offer"].sudo().create({
            "product_tmpl_id": tmpl.id,
            "product_id": product.id,
            "vendor_id": vendor.id,
            "seller_sku": seller_sku,
            "price": price,
            "currency_id": currency_id,
            "fulfillment_type": fulfillment_type,
            "stock_location_id": vendor._marketplace_stock_location(fulfillment_type).id,
            "market_ids": [(6, 0, website_ids)],
        })
        return request.redirect(f"/my/vendor/products/{offer.id}/edit?created=1")

    @http.route("/my/vendor/products/sell-existing", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_product_sell_existing(self, **post):
        """Offer a published catalog product: only an offer is created, never a template."""
        vendor = self._require_vendor()
        if not request.env["res.partner"]._marketplace_catalog_enabled():
            raise NotFound()
        Offer = request.env["leaders.marketplace.offer"].sudo()
        product_id = int(post.get("product_id") or 0) if str(post.get("product_id") or "").isdigit() else 0
        product = request.env["product.product"].sudo().browse(product_id).exists()
        seller_sku = (post.get("seller_sku") or "").strip()
        price = self._to_float(post.get("price"))
        fulfillment_type = post.get("fulfillment_type") if post.get("fulfillment_type") in ("fbm", "fbl") else "fbm"
        currency_id = int(post.get("currency_id") or request.env.company.currency_id.id)
        website_ids = [int(x) for x in request.httprequest.form.getlist("market_ids") if x]
        query = post.get("q") or (product.name if product else "")

        error = False
        if not product or product.product_tmpl_id.marketplace_state != "published" or not product.sale_ok:
            error = _("Choose a published product from the catalog.")
        elif not seller_sku or price < 0:
            error = _("Seller SKU and a valid price are required.")
        elif Offer.with_context(active_test=False).search_count([
                ("vendor_id", "=", vendor.id), ("product_id", "=", product.id),
                ("fulfillment_type", "=", fulfillment_type)]):
            error = _("You already have an offer for this product with the same fulfillment.")
        elif Offer.with_context(active_test=False).search_count([
                ("vendor_id", "=", vendor.id), ("seller_sku", "=", seller_sku)]):
            error = _("Seller SKU %s is already used by another of your offers.", seller_sku)
        elif self._vendor_over_offer_limit(vendor):
            error = _("Your vendor tier product limit has been reached.")
        if error:
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._catalog_values(vendor, query, error=error))

        offer = Offer.create({
            "product_tmpl_id": product.product_tmpl_id.id,
            "product_id": product.id,
            "vendor_id": vendor.id,
            "seller_sku": seller_sku,
            "price": price,
            "currency_id": currency_id,
            "fulfillment_type": fulfillment_type,
            "stock_location_id": vendor._marketplace_stock_location(fulfillment_type).id,
            "market_ids": [(6, 0, website_ids)],
        })
        return request.redirect(f"/my/vendor/products/{offer.id}/edit?created=1")

    @http.route(["/my/vendor/products/<int:offer_id>/edit"], type="http", auth="user", website=True, methods=["GET", "POST"], csrf=True)
    def vendor_product_edit(self, offer_id, **post):
        vendor = self._require_vendor()
        offer = self._offer_owned_by_vendor(offer_id, vendor)

        if request.httprequest.method == "GET":
            values = self._vendor_form_values(vendor, offer=offer)
            values["created"] = request.params.get("created")
            return self._vendor_render("leaders_marketplace.portal_vendor_product_form", values)

        name = (post.get("name") or "").strip()
        seller_sku = (post.get("seller_sku") or "").strip()
        price = self._to_float(post.get("price"))
        fulfillment_type = post.get("fulfillment_type") or offer.fulfillment_type
        category_id = int(post.get("public_category_id") or 0)
        currency_id = int(post.get("currency_id") or offer.currency_id.id)
        website_ids = [int(x) for x in request.httprequest.form.getlist("market_ids") if x]
        if not name or not seller_sku or price < 0:
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._vendor_form_values(vendor, offer=offer, error=_("Product name, seller SKU and a valid price are required.")),
            )

        image_b64, image_error = self._read_vendor_image(
            request.httprequest.files.get("image_1920"))
        if image_error:
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._vendor_form_values(vendor, offer=offer, error=image_error),
            )
        if fulfillment_type != offer.fulfillment_type and request.env["leaders.marketplace.offer"].sudo().search_count([
                ("vendor_id", "=", vendor.id), ("product_id", "=", offer.product_id.id),
                ("fulfillment_type", "=", fulfillment_type), ("id", "!=", offer.id),
                ("state", "!=", "rejected")]):
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._vendor_form_values(vendor, offer=offer, error=_(
                    "You already have an offer for this product with the same fulfillment.")),
            )

        # The template is shared as soon as another seller lists on it, so its
        # content (name, image, publication) is only the vendor's to change
        # while they are the sole owner. Otherwise their edit is confined to
        # their own offer and the template is left untouched.
        if offer.product_tmpl_id.sudo()._leaders_vendor_owns_content(vendor):
            tmpl_vals = {
                "name": name,
                "description_sale": post.get("description_sale") or False,
                "type": "consu",
                "is_storable": True,
                "marketplace_state": "draft",
                "is_published": False,
            }
            if image_b64:
                tmpl_vals["image_1920"] = image_b64
            if category_id:
                tmpl_vals["public_categ_ids"] = [(6, 0, [category_id])]
            offer.product_tmpl_id.sudo().write(tmpl_vals)
        offer.sudo().write({
            "seller_sku": seller_sku,
            "price": price,
            "currency_id": currency_id,
            "fulfillment_type": fulfillment_type,
            "market_ids": [(6, 0, website_ids)],
            "state": "draft",
            "reject_reason": False,
        })
        location = vendor._marketplace_stock_location(fulfillment_type)
        if offer.stock_location_id != location:
            offer.sudo().write({"stock_location_id": location.id})
        return request.redirect(f"/my/vendor/products/{offer.id}/edit?saved=1")

    @http.route("/my/vendor/products/<int:offer_id>/submit", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_product_submit(self, offer_id, **post):
        vendor = self._require_vendor()
        offer = self._offer_owned_by_vendor(offer_id, vendor)
        # Unpublishing a shared template would pull other sellers' offers off
        # the website too; only the sole owner may send it back for review.
        if offer.product_tmpl_id.sudo()._leaders_vendor_owns_content(vendor):
            offer.product_tmpl_id.sudo().write({"marketplace_state": "pending", "is_published": False})
        offer.sudo().action_submit()
        return request.redirect("/my/vendor/products")

    def _vendor_stock_error(self, message):
        return request.redirect(f"/my/vendor/products?error={quote(str(message))}")

    @http.route("/my/vendor/products/<int:offer_id>/stock", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_product_stock(self, offer_id, **post):
        vendor = self._require_vendor()
        offer = self._offer_owned_by_vendor(offer_id, vendor)
        if offer.fulfillment_type != "fbm":
            raise UserError(_("FBL stock is controlled by warehouse inbound receipts, not direct vendor adjustments."))
        if not offer.product_id.is_storable:
            raise UserError(_("Marketplace stock updates require a goods product, not a service."))
        mode = post.get("mode") or "add"
        if mode not in ("add", "set"):
            raise UserError(_("Unknown stock update mode."))
        qty = self._to_float(post.get("quantity"), 0.0)
        location = vendor._marketplace_stock_location("fbm")
        if mode == "set":
            if qty < 0:
                return self._vendor_stock_error(_("The stock quantity cannot be negative."))
            rounding = offer.product_id.uom_id.rounding
            quants = request.env["stock.quant"].sudo().search([
                ("product_id", "=", offer.product_id.id),
                ("location_id", "=", location.id),
                ("owner_id", "=", vendor.id),
                ("lot_id", "=", False),
                ("package_id", "=", False),
            ])
            reserved = sum(quants.mapped("reserved_quantity"))
            if float_compare(qty, reserved, precision_rounding=rounding) < 0:
                return self._vendor_stock_error(_(
                    "You cannot set the stock of %(product)s to %(qty)s: %(reserved)s are already reserved for open orders.",
                    product=offer.product_id.display_name, qty=qty, reserved=reserved))
            delta = qty - sum(quants.mapped("quantity"))
            if not float_is_zero(delta, precision_rounding=rounding):
                if offer.stock_location_id != location:
                    offer.sudo().stock_location_id = location
                request.env["stock.quant"].sudo()._leaders_adjust_vendor_stock(
                    offer.product_id, location, vendor, delta,
                    reason=_("Vendor portal stock correction (%s)", offer.seller_sku))
            return request.redirect("/my/vendor/products")
        if qty < 0:
            raise UserError(_("The added stock quantity cannot be negative."))
        if not float_is_zero(qty, precision_rounding=offer.product_id.uom_id.rounding):
            # Real inventory move into the vendor's own warehouse, owned by the vendor.
            if offer.stock_location_id != location:
                offer.sudo().stock_location_id = location
            request.env["stock.quant"].sudo()._leaders_adjust_vendor_stock(
                offer.product_id, location, vendor, qty,
                reason=_("Vendor portal stock update (%s)", offer.seller_sku))
        return request.redirect("/my/vendor/products")

    # -------------------------------------------------------------------------
    # Orders / fulfillment
    # -------------------------------------------------------------------------
    @http.route("/my/vendor/orders", type="http", auth="user", website=True)
    def vendor_orders(self, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        orders = request.env["leaders.vendor.order"].sudo().search([
            ("vendor_id", "=", vendor.id)
        ], order="id desc")
        return self._vendor_render("leaders_marketplace.portal_vendor_orders", {"orders": orders, "vendor": vendor})

    @http.route("/my/vendor/orders/<int:order_id>", type="http", auth="user", website=True)
    def vendor_order_detail(self, order_id, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        order = request.env["leaders.vendor.order"].sudo().search([
            ("id", "=", order_id), ("vendor_id", "=", vendor.id)
        ], limit=1)
        if not order:
            raise AccessError(_("You cannot access this vendor order."))
        return self._vendor_render("leaders_marketplace.portal_vendor_order_detail", {
            "order": order, "vendor": vendor, "error": kw.get("error"),
            "vendor_can_cancel": request.env["ir.config_parameter"].sudo().get_param(
                "leaders_marketplace.vendor_can_cancel_orders") in ("True", "true", "1"),
        })

    @http.route("/my/vendor/orders/<int:order_id>/confirm", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_order_confirm(self, order_id, **post):
        vendor = self._require_vendor(allow_frozen=True)
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can confirm orders."))
        order = request.env["leaders.vendor.order"].sudo().search([
            ("id", "=", order_id), ("vendor_id", "=", vendor.id),
        ], limit=1)
        if not order:
            raise AccessError(_("You cannot access this vendor order."))
        try:
            with request.env.cr.savepoint():
                order.action_confirm()
        except UserError as error:
            return request.redirect(f"/my/vendor/orders/{order.id}?error={quote(str(error.args[0]))}")
        return request.redirect(f"/my/vendor/orders/{order.id}")

    @http.route("/my/vendor/orders/<int:order_id>/cancel", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_order_cancel(self, order_id, **post):
        vendor = self._require_vendor(allow_frozen=True)
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can cancel orders."))
        order = request.env["leaders.vendor.order"].sudo().search([
            ("id", "=", order_id), ("vendor_id", "=", vendor.id),
        ], limit=1)
        if not order:
            raise AccessError(_("You cannot access this vendor order."))
        url = f"/my/vendor/orders/{order.id}"
        try:
            with request.env.cr.savepoint():
                order.action_vendor_cancel(post.get("reason"), request.env.user)
        except UserError as error:
            return request.redirect(f"{url}?error={quote(str(error.args[0]))}")
        return request.redirect(url)

    @http.route("/my/vendor/orders/<int:order_id>/confirm-cod-payment", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_order_confirm_cod_payment(self, order_id, **post):
        vendor = self._require_vendor(allow_frozen=True)
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can confirm cash collection."))
        order = request.env["leaders.vendor.order"].sudo().search([
            ("id", "=", order_id), ("vendor_id", "=", vendor.id),
        ], limit=1)
        if not order:
            raise AccessError(_("You cannot access this vendor order."))
        try:
            with request.env.cr.savepoint():
                order.action_confirm_cod_payment()
        except UserError as error:
            return request.redirect(f"/my/vendor/orders/{order.id}?error={quote(str(error.args[0]))}")
        return request.redirect(f"/my/vendor/orders/{order.id}")

    @http.route("/my/vendor/fulfillment/<int:group_id>/ship", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_fulfillment_ship(self, group_id, **post):
        vendor = self._require_vendor(allow_frozen=True)
        group = request.env["leaders.fulfillment.group"].sudo().search([
            ("id", "=", group_id), ("vendor_id", "=", vendor.id)
        ], limit=1)
        if not group:
            raise AccessError(_("You cannot access this fulfillment group."))
        if group.fulfillment_type != "fbm":
            raise AccessError(_("FBL shipments are handled by the Leaders warehouse."))
        url = f"/my/vendor/orders/{group.vendor_order_id.id}"
        try:
            with request.env.cr.savepoint():
                group.sudo().write({
                    "tracking_ref": (post.get("tracking_ref") or "").strip(),
                    "carrier_id": int(post.get("carrier_id") or 0) or False,
                })
                group.sudo().action_ship()
        except UserError as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(url)

    @http.route("/my/vendor/fulfillment/<int:group_id>/delivered", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_fulfillment_delivered(self, group_id, **post):
        vendor = self._require_vendor(allow_frozen=True)
        group = request.env["leaders.fulfillment.group"].sudo().search([
            ("id", "=", group_id), ("vendor_id", "=", vendor.id), ("fulfillment_type", "=", "fbm"),
        ], limit=1)
        if not group:
            raise AccessError(_("You cannot access this fulfillment group."))
        url = f"/my/vendor/orders/{group.vendor_order_id.id}"
        proof_file = request.httprequest.files.get("delivery_proof")
        proof = proof_name = False
        if proof_file and proof_file.filename:
            proof_data = proof_file.read()
            if len(proof_data) > self._vendor_image_max_bytes():
                return request.redirect("%s?error=%s" % (url, quote(
                    _("The delivery proof is too large."))))
            proof = base64.b64encode(proof_data)
            proof_name = proof_file.filename
        try:
            with request.env.cr.savepoint():
                # Completes the delivery outright in "vendor" mode (the default),
                # or waits for the customer in "customer_confirm" mode.
                group.action_report_delivery(
                    reported_by=request.env.user, proof=proof, proof_name=proof_name)
        except UserError as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(url)

    @http.route("/my/vendor/orders/<int:order_id>/invoice", type="http", auth="user", website=True)
    def vendor_order_invoice_pdf(self, order_id, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        order = request.env["leaders.vendor.order"].sudo().search([
            ("id", "=", order_id), ("vendor_id", "=", vendor.id), ("invoice_id", "!=", False),
        ], limit=1)
        if not order:
            raise AccessError(_("You cannot access this invoice."))
        return self._render_vendor_invoice_pdf(order.invoice_id)

    def _render_vendor_invoice_pdf(self, invoice):
        report = request.env["ir.actions.report"].sudo()
        pdf, _type = report._render_qweb_pdf("leaders_marketplace.action_report_vendor_invoice", invoice.ids)
        filename = invoice._get_report_base_filename() + ".pdf"
        return request.make_response(pdf, headers=[
            ("Content-Type", "application/pdf"),
            ("Content-Length", len(pdf)),
            ("Content-Disposition", f'inline; filename="{filename}"'),
        ])

    # -------------------------------------------------------------------------
    # Finance
    # -------------------------------------------------------------------------
    @http.route("/my/vendor/finance", type="http", auth="user", website=True)
    def vendor_finance(self, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can access financial information."))
        settlements = request.env["leaders.settlement"].sudo().search([
            ("vendor_id", "=", vendor.id)
        ], order="id desc")
        txs = request.env["leaders.wallet.transaction"].sudo().search([
            ("vendor_id", "=", vendor.id)
        ], order="id desc", limit=100)
        Plan = request.env["leaders.commission.rule"].sudo()
        plans = Plan.search([
            ("active", "=", True), ("state", "in", ("approved", "done")),
            "|", ("vendor_plan_ids.vendor_id", "=", vendor.id),
            ("vendor_ids", "in", vendor.id),
        ], order="date_from desc, id desc")
        achievements = request.env["leaders.commission.achievement"].sudo().search([
            ("vendor_id", "=", vendor.id),
        ], order="period_to desc, id desc", limit=50)
        orders = request.env["leaders.vendor.order"].sudo().search([
            ("vendor_id", "=", vendor.id),
        ], order="date_order desc", limit=100)
        fulfillment_groups = request.env["leaders.fulfillment.group"].sudo().search([
            ("vendor_id", "=", vendor.id),
        ], order="id desc", limit=100)
        Settlement = request.env["leaders.settlement"].sudo()
        no_plan_mode = Settlement._leaders_no_plan_commission_mode()
        Reserve = request.env["leaders.vendor.reserve"].sudo()
        held_reserves = Reserve.search(
            [("vendor_id", "=", vendor.id), ("state", "=", "held")], order="id desc")
        return self._vendor_render("leaders_marketplace.portal_vendor_finance", {
            "no_plan_commission_mode": no_plan_mode,
            "reserve_enforced": Reserve._leaders_reserve_enforced(),
            "held_reserves": held_reserves,
            "reserved_total": sum(
                r.amount - r.consumed_amount for r in held_reserves),
            # A partner's company_currency_id can be empty, which would blow up
            # the monetary widget; fall back to the company's own currency.
            "reserved_currency": (
                held_reserves[:1].currency_id
                or vendor.company_currency_id
                or request.env.company.currency_id),
            "no_plan_commission_percent": (
                Plan._marketplace_default_percent(vendor) if no_plan_mode == "rate" else 0.0),
            "settlements": settlements,
            "transactions": txs,
            "plans": plans,
            "achievements": achievements,
            "orders": orders,
            "fulfillment_groups": fulfillment_groups,
            "vendor": vendor,
            "settlement_error": request.params.get("settlement_error"),
            "settlement_requested": request.params.get("settlement_requested"),
        })

    @http.route("/my/vendor/finance/settlement/<int:settlement_id>/invoice", type="http", auth="user", website=True)
    def vendor_settlement_invoice_pdf(self, settlement_id, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can access financial information."))
        settlement = request.env["leaders.settlement"].sudo().search([
            ("id", "=", settlement_id), ("vendor_id", "=", vendor.id),
            ("commission_invoice_id", "!=", False),
        ], limit=1)
        if not settlement:
            raise AccessError(_("You cannot access this invoice."))
        invoice = settlement.commission_invoice_id
        pdf, _type = request.env["ir.actions.report"].sudo()._render_qweb_pdf(
            "account.account_invoices", invoice.ids)
        return request.make_response(pdf, headers=[
            ("Content-Type", "application/pdf"),
            ("Content-Length", len(pdf)),
            ("Content-Disposition", 'inline; filename="%s.pdf"' % invoice._get_report_base_filename()),
        ])

    @http.route("/my/vendor/finance/settlement/request", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_request_settlement(self, **post):
        vendor = self._require_vendor(allow_frozen=True)
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can request a settlement."))
        url = "/my/vendor/finance"
        try:
            with request.env.cr.savepoint():
                period_from = fields.Date.from_string(post.get("period_from"))
                period_to = fields.Date.from_string(post.get("period_to"))
                if not period_from or not period_to or period_from > period_to:
                    raise UserError(_("Please select a valid period."))
                if period_to > fields.Date.today():
                    raise UserError(_("The period cannot end in the future."))
                overlap = request.env["leaders.settlement"].sudo().search_count([
                    ("vendor_id", "=", vendor.id), ("state", "!=", "cancelled"),
                    ("period_from", "<=", period_to), ("period_to", ">=", period_from),
                ])
                if overlap:
                    raise UserError(_("A settlement already exists for an overlapping period."))
                settlement = request.env["leaders.settlement"].sudo().create({
                    "vendor_id": vendor.id, "period_from": period_from, "period_to": period_to,
                })
                settlement.action_collect_transactions()
                if not settlement.transaction_ids:
                    raise UserError(_("No available earnings were found for this period."))
                settlement.message_post(body=_("Settlement requested by the vendor from the portal."))
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?settlement_error={quote(str(e.args[0]))}")
        return request.redirect(f"{url}?settlement_requested=1")

    # -------------------------------------------------------------------------
    # Delivery zones
    # -------------------------------------------------------------------------
    @http.route("/my/vendor/delivery-zones", type="http", auth="user", website=True)
    def vendor_delivery_zones(self, **kw):
        vendor = self._require_vendor()
        zones = request.env["leaders.vendor.delivery.zone"].sudo().search([
            ("vendor_id", "=", vendor.id)
        ], order="country_id, state_id")
        countries = request.env["res.country"].sudo().search([], order="name")
        states = request.env["res.country.state"].sudo().search([], order="name")
        return self._vendor_render("leaders_marketplace.portal_vendor_delivery_zones", {
            "vendor": vendor,
            "zones": zones,
            "countries": countries,
            "states": states,
            "error": kw.get("error"),
        })

    @http.route("/my/vendor/delivery-zones/new", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_delivery_zone_create(self, **post):
        vendor = self._require_vendor()
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can manage delivery zones."))
        url = "/my/vendor/delivery-zones"
        try:
            with request.env.cr.savepoint():
                state_id = int(post.get("state_id") or 0)
                price = self._to_float(post.get("price"), -1.0)
                if not state_id or price < 0:
                    raise UserError(_("Please select a governorate and a valid price."))
                request.env["leaders.vendor.delivery.zone"].sudo().create({
                    "vendor_id": vendor.id, "state_id": state_id, "price": price,
                })
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(url)

    def _delivery_zone_owned_by_vendor(self, zone_id, vendor):
        zone = request.env["leaders.vendor.delivery.zone"].sudo().search([
            ("id", "=", zone_id), ("vendor_id", "=", vendor.id),
        ], limit=1)
        if not zone:
            raise AccessError(_("You cannot access this delivery zone."))
        return zone

    @http.route("/my/vendor/delivery-zones/<int:zone_id>/update", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_delivery_zone_update(self, zone_id, **post):
        vendor = self._require_vendor()
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can manage delivery zones."))
        zone = self._delivery_zone_owned_by_vendor(zone_id, vendor)
        url = "/my/vendor/delivery-zones"
        try:
            with request.env.cr.savepoint():
                price = self._to_float(post.get("price"), -1.0)
                if price < 0:
                    raise UserError(_("Please enter a valid price."))
                zone.write({"price": price})
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(url)

    @http.route("/my/vendor/delivery-zones/<int:zone_id>/delete", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_delivery_zone_delete(self, zone_id, **post):
        vendor = self._require_vendor()
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can manage delivery zones."))
        zone = self._delivery_zone_owned_by_vendor(zone_id, vendor)
        zone.unlink()
        return request.redirect("/my/vendor/delivery-zones")

    # -------------------------------------------------------------------------
    # Returns / disputes / performance
    # -------------------------------------------------------------------------
    @http.route("/my/vendor/returns", type="http", auth="user", website=True)
    def vendor_returns(self, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        records = request.env["leaders.vendor.return"].sudo().search([("vendor_id", "=", vendor.id)], order="id desc")
        return self._vendor_render("leaders_marketplace.portal_vendor_returns", {"records": records, "vendor": vendor})

    def _vendor_return(self, vendor, return_id):
        record = request.env["leaders.vendor.return"].sudo().search([
            ("id", "=", return_id), ("vendor_id", "=", vendor.id)], limit=1)
        if not record:
            raise AccessError(_("You cannot access this return."))
        return record

    @http.route("/my/vendor/returns/<int:return_id>", type="http", auth="user", website=True)
    def vendor_return_detail(self, return_id, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        record = self._vendor_return(vendor, return_id)
        return self._vendor_render("leaders_marketplace.portal_vendor_return_detail", {
            "record": record, "vendor": vendor, "error": kw.get("error"),
            "vendor_can_receive": record.state == "approved" and all(
                p.location_dest_id._child_of(vendor.vendor_warehouse_id.view_location_id)
                for p in record.picking_ids.filtered(lambda p: p.state != "cancel")),
        })

    @http.route("/my/vendor/returns/<int:return_id>/<string:decision>", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def vendor_return_decide(self, return_id, decision, **post):
        vendor = self._require_vendor(allow_frozen=True)
        record = self._vendor_return(vendor, return_id)
        url = f"/my/vendor/returns/{record.id}"
        note = (post.get("note") or "").strip() or None
        try:
            with request.env.cr.savepoint():
                if decision == "approve":
                    record.action_vendor_approve(note=note)
                elif decision == "reject":
                    record.action_vendor_reject(note=note)
                elif decision == "receive":
                    # FBL returns come back to the Leaders warehouse, which confirms them.
                    if any(not p.location_dest_id._child_of(vendor.vendor_warehouse_id.view_location_id)
                           for p in record.picking_ids.filtered(lambda p: p.state != "cancel")):
                        raise UserError(_("This return is received by the Leaders warehouse."))
                    record.action_receive()
                else:
                    raise AccessError(_("Unknown action."))
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(url)

    @http.route("/my/vendor/disputes", type="http", auth="user", website=True)
    def vendor_disputes(self, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        records = request.env["leaders.vendor.dispute"].sudo().search([("vendor_id", "=", vendor.id)], order="id desc")
        return self._vendor_render("leaders_marketplace.portal_vendor_disputes", {"records": records, "vendor": vendor})

    def _vendor_dispute(self, vendor, dispute_id):
        record = request.env["leaders.vendor.dispute"].sudo().search([
            ("id", "=", dispute_id), ("vendor_id", "=", vendor.id)], limit=1)
        if not record:
            raise AccessError(_("You cannot access this dispute."))
        return record

    @http.route("/my/vendor/disputes/<int:dispute_id>", type="http", auth="user", website=True)
    def vendor_dispute_detail(self, dispute_id, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        record = self._vendor_dispute(vendor, dispute_id)
        return self._vendor_render("leaders_marketplace.portal_vendor_dispute_detail", {
            "record": record, "vendor": vendor,
            "messages": self._dispute_messages(record),
            "error": kw.get("error"), "replied": kw.get("replied"),
        })

    @http.route("/my/vendor/disputes/<int:dispute_id>/reply", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def vendor_dispute_reply(self, dispute_id, **post):
        vendor = self._require_vendor(allow_frozen=True)
        record = self._vendor_dispute(vendor, dispute_id)
        url = "/my/vendor/disputes/%s" % record.id
        message = (post.get("message") or "").strip()
        if not message:
            return request.redirect("%s?error=%s" % (url, quote(
                _("Please write a reply."))))
        try:
            with request.env.cr.savepoint():
                record.action_vendor_reply(message=message)
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(f"{url}?replied=1")

    @staticmethod
    def _dispute_messages(dispute):
        """The conversation, oldest first, without the system tracking noise."""
        return dispute.sudo().message_ids.filtered(
            lambda m: m.body and m.message_type in ("comment", "notification")
        ).sorted("id")

    @http.route("/my/vendor/performance", type="http", auth="user", website=True)
    def vendor_performance(self, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        kpis = request.env["leaders.vendor.kpi"].sudo().search([("vendor_id", "=", vendor.id)], order="period desc", limit=12)
        penalties = request.env["leaders.vendor.penalty"].sudo().search([("vendor_id", "=", vendor.id)], order="id desc", limit=30)
        return self._vendor_render("leaders_marketplace.portal_vendor_performance", {"kpis": kpis, "penalties": penalties, "vendor": vendor})

    # -------------------------------------------------------------------------
    # Vendor staff management (owner only)
    # -------------------------------------------------------------------------
    @http.route(["/my/vendor/staff"], type="http", auth="user", website=True, methods=["GET", "POST"], csrf=True)
    def vendor_staff(self, **post):
        vendor = self._require_vendor()
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can manage staff."))
        error = False
        warning = False
        if request.httprequest.method == "POST":
            name = (post.get("name") or "").strip()
            email = email_normalize(post.get("email") or "")
            if not name or not email:
                error = _("A name and a valid email address are required.")
            else:
                # Archived users keep their login and the unique index with it,
                # so the check has to see them or the create blows up on the
                # constraint instead of telling the owner what to do.
                existing = request.env["res.users"].sudo().with_context(
                    active_test=False).search([("login", "=", email)], limit=1)
                if existing and not existing.active:
                    error = _("This email belongs to a deactivated account; "
                              "reactivate it from the list.")
                elif existing:
                    error = _("A user with this email already exists.")
            if not error:
                contact = request.env["res.partner"].sudo().create({
                    "name": name,
                    "email": email,
                    "parent_id": vendor.id,
                    "type": "contact",
                })
                group = request.env.ref("leaders_marketplace.group_vendor_staff")
                user = request.env["res.users"].sudo().with_context(no_reset_password=True).create({
                    "name": name,
                    "login": email,
                    "email": email,
                    "partner_id": contact.id,
                    "groups_id": [(6, 0, [group.id])],
                })
                if self._send_staff_invitation(user):
                    return request.redirect("/my/vendor/staff?invited=1")
                return request.redirect("/my/vendor/staff?mail_failed=1")
        return self._vendor_render("leaders_marketplace.portal_vendor_staff", {
            "vendor": vendor,
            "staff": self._vendor_staff_users(vendor),
            "error": error,
            "warning": warning,
            "invited": request.params.get("invited"),
            "mail_failed": request.params.get("mail_failed"),
            "resent": request.params.get("resent"),
            "updated": request.params.get("updated"),
        })

    def _vendor_staff_users(self, vendor):
        """Every user of this vendor except the one looking, archived included."""
        return request.env["res.users"].sudo().with_context(active_test=False).search([
            ("partner_id.commercial_partner_id", "=", vendor.id),
            ("id", "!=", request.env.user.id),
        ], order="name")

    @staticmethod
    def _send_staff_invitation(user):
        """Send the password invitation; a mail failure must not lose the user.

        Same shape as ``_ensure_vendor_owner_user``: the account is worth more
        than the email, and the owner can resend from the list.
        """
        try:
            with request.env.cr.savepoint():
                user.sudo().with_context(create_user=True).action_reset_password()
        except Exception:
            return False
        return True

    def _vendor_staff_member(self, user_id, vendor):
        """A staff user of this vendor the owner is allowed to act on."""
        user = request.env["res.users"].sudo().with_context(active_test=False).search([
            ("id", "=", user_id),
            ("partner_id.commercial_partner_id", "=", vendor.id),
        ], limit=1)
        if not user or user.id == request.env.user.id:
            raise AccessError(_("You cannot manage this user."))
        if user.has_group("leaders_marketplace.group_vendor_owner"):
            raise AccessError(_("Vendor owners cannot be managed from the portal."))
        return user

    @http.route("/my/vendor/staff/<int:user_id>/deactivate", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def vendor_staff_deactivate(self, user_id, **post):
        vendor = self._require_vendor()
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can manage staff."))
        self._vendor_staff_member(user_id, vendor).write({"active": False})
        return request.redirect("/my/vendor/staff?updated=1")

    @http.route("/my/vendor/staff/<int:user_id>/reactivate", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def vendor_staff_reactivate(self, user_id, **post):
        vendor = self._require_vendor()
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can manage staff."))
        self._vendor_staff_member(user_id, vendor).write({"active": True})
        return request.redirect("/my/vendor/staff?updated=1")

    @http.route("/my/vendor/staff/<int:user_id>/resend", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def vendor_staff_resend(self, user_id, **post):
        vendor = self._require_vendor()
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can manage staff."))
        user = self._vendor_staff_member(user_id, vendor)
        if self._send_staff_invitation(user):
            return request.redirect("/my/vendor/staff?resent=1")
        return request.redirect("/my/vendor/staff?mail_failed=1")

    # -------------------------------------------------------------------------
    # Customer marketplace portal
    # -------------------------------------------------------------------------
    @http.route("/my/marketplace/orders", type="http", auth="user", website=True)
    def customer_marketplace_orders(self, **kw):
        partner = request.env.user.partner_id.commercial_partner_id
        orders = request.env["leaders.vendor.order"].sudo().search([
            ("partner_id", "child_of", partner.id),
        ], order="date_order desc, id desc")
        return request.render("leaders_marketplace.portal_customer_marketplace_orders", {"orders": orders})

    def _customer_vendor_order(self, order_id):
        partner = request.env.user.partner_id.commercial_partner_id
        order = request.env["leaders.vendor.order"].sudo().search([
            ("id", "=", order_id), ("partner_id", "child_of", partner.id),
        ], limit=1)
        if not order:
            raise AccessError(_("You cannot access this order."))
        return order

    @http.route(["/my/marketplace/orders/<int:order_id>", "/my/marketplace/vendor-orders/<int:order_id>"],
                type="http", auth="user", website=True)
    def customer_marketplace_order_detail(self, order_id, **kw):
        order = self._customer_vendor_order(order_id)
        window = int(request.env["ir.config_parameter"].sudo().get_param("leaders_marketplace.return_window_days", 14) or 0)
        returnable = [(line, line._qty_returnable()) for line in order.line_ids.filtered(
            lambda l: not l.is_delivery and l._qty_returnable() > 0)]
        in_window = bool(order.date_shipped) and (
            not window or fields.Datetime.now() <= order.date_shipped + timedelta(days=window))
        catalog_on = request.env["res.partner"]._marketplace_catalog_enabled()
        rating = request.env["rating.rating"].sudo().search([
            ("res_model", "=", "leaders.vendor.order"), ("res_id", "=", order.id),
            ("consumed", "=", True)], limit=1) if catalog_on else False
        return request.render("leaders_marketplace.portal_customer_marketplace_order_detail", {
            "lm_can_rate": bool(catalog_on and order.state == "completed" and not rating),
            "lm_rating": rating,
            "rated": kw.get("rated"),
            "order": order,
            "returnable_lines": returnable if in_window and order.state in ("fulfilled", "completed", "partially_fulfilled") else [],
            "error": kw.get("error"),
            "return_requested": kw.get("return_requested"),
            "delivery_confirmed": kw.get("delivery_confirmed"),
            "delivery_disputed": kw.get("delivery_disputed"),
            "disputes": request.env["leaders.vendor.dispute"].sudo().search(
                [("vendor_order_id", "=", order.id)], order="id desc"),
            "dispute_messages": {
                d.id: self._dispute_messages(d)
                for d in request.env["leaders.vendor.dispute"].sudo().search(
                    [("vendor_order_id", "=", order.id)])},
            "dispute_opened": kw.get("dispute_opened"),
            "dispute_updated": kw.get("dispute_updated"),
        })

    @http.route("/my/marketplace/orders/<int:order_id>/rate", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def customer_marketplace_order_rate(self, order_id, **post):
        order = self._customer_vendor_order(order_id)
        if not request.env["res.partner"]._marketplace_catalog_enabled():
            raise NotFound()
        url = f"/my/marketplace/orders/{order.id}"
        try:
            with request.env.cr.savepoint():
                order._marketplace_submit_rating(post.get("rating"), post.get("feedback"))
        except UserError as error:
            return request.redirect(f"{url}?error={quote(str(error.args[0]))}")
        return request.redirect(f"{url}?rated=1")

    @http.route("/my/marketplace/orders/<int:order_id>/return", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def customer_request_return(self, order_id, **post):
        order = self._customer_vendor_order(order_id)
        url = f"/my/marketplace/orders/{order.id}"
        lines = []
        for line in order.line_ids:
            qty = self._to_float(post.get(f"qty_{line.id}"), 0.0)
            if qty > 0:
                lines.append((0, 0, {"vendor_line_id": line.id, "quantity": qty}))
        reason = (post.get("reason") or "").strip()
        try:
            if not reason:
                raise UserError(_("Please tell us why you are returning the products."))
            with request.env.cr.savepoint():
                record = request.env["leaders.vendor.return"].sudo().create({
                    "vendor_order_id": order.id, "reason": reason, "line_ids": lines,
                })
                record.message_post(body=_("Return requested by the customer from the portal."))
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(f"{url}?return_requested=1")

    def _customer_reported_group(self, group_id):
        """A group of this customer's own order that is waiting on their answer."""
        partner = request.env.user.partner_id.commercial_partner_id
        group = request.env["leaders.fulfillment.group"].sudo().search([
            ("id", "=", group_id),
            ("state", "=", "delivery_reported"),
            ("vendor_order_id.partner_id", "child_of", partner.id),
        ], limit=1)
        if not group:
            raise AccessError(_("You cannot access this shipment."))
        return group

    @http.route("/my/marketplace/fulfillment/<int:group_id>/confirm-delivery", type="http",
                auth="user", website=True, methods=["POST"], csrf=True)
    def customer_confirm_delivery(self, group_id, **post):
        group = self._customer_reported_group(group_id)
        url = f"/my/marketplace/orders/{group.vendor_order_id.id}"
        try:
            with request.env.cr.savepoint():
                group.action_delivered(confirmed_by=request.env.user)
        except UserError as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(f"{url}?delivery_confirmed=1")

    @http.route("/my/marketplace/fulfillment/<int:group_id>/not-received", type="http",
                auth="user", website=True, methods=["POST"], csrf=True)
    def customer_delivery_not_received(self, group_id, **post):
        group = self._customer_reported_group(group_id)
        url = f"/my/marketplace/orders/{group.vendor_order_id.id}"
        try:
            with request.env.cr.savepoint():
                group.action_delivery_disputed(
                    reason=post.get("reason"), raised_by=request.env.user)
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(f"{url}?delivery_disputed=1")

    @http.route("/my/marketplace/returns/<int:return_id>/escalate", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def customer_escalate_return(self, return_id, **post):
        partner = request.env.user.partner_id.commercial_partner_id
        record = request.env["leaders.vendor.return"].sudo().search([
            ("id", "=", return_id), ("partner_id", "child_of", partner.id)], limit=1)
        if not record:
            raise AccessError(_("You cannot access this return."))
        url = f"/my/marketplace/orders/{record.vendor_order_id.id}"
        try:
            with request.env.cr.savepoint():
                record.action_escalate()
        except UserError as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(url)

    @http.route("/my/marketplace/orders/<int:order_id>/dispute", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def customer_open_dispute(self, order_id, **post):
        order = self._customer_vendor_order(order_id)
        url = f"/my/marketplace/orders/{order.id}"
        reason = (post.get("reason") or "").strip()
        if not reason:
            return request.redirect("%s?error=%s" % (url, quote(
                _("Please tell us what went wrong."))))
        try:
            with request.env.cr.savepoint():
                dispute = request.env["leaders.vendor.dispute"].sudo().create({
                    "vendor_order_id": order.id, "reason": reason,
                })
                dispute.message_post(
                    body=reason, subtype_xmlid="mail.mt_comment",
                    attachment_ids=self._dispute_attachment_ids())
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(f"{url}?dispute_opened=1")

    @staticmethod
    def _dispute_attachment_ids():
        """Attachments the customer added to the form, if any."""
        files = request.httprequest.files.getlist("attachments")
        attachments = request.env["ir.attachment"].sudo()
        created = []
        for upload in files:
            if not upload or not upload.filename:
                continue
            created.append(attachments.create({
                "name": upload.filename,
                "datas": base64.b64encode(upload.read()),
            }).id)
        return created

    def _customer_dispute(self, dispute_id):
        partner = request.env.user.partner_id.commercial_partner_id
        record = request.env["leaders.vendor.dispute"].sudo().search([
            ("id", "=", dispute_id), ("partner_id", "child_of", partner.id)], limit=1)
        if not record:
            raise AccessError(_("You cannot access this dispute."))
        return record

    @http.route("/my/marketplace/disputes/<int:dispute_id>/message", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def customer_dispute_message(self, dispute_id, **post):
        record = self._customer_dispute(dispute_id)
        url = f"/my/marketplace/orders/{record.vendor_order_id.id}"
        message = (post.get("message") or "").strip()
        if not message:
            return request.redirect("%s?error=%s" % (url, quote(_("Please write a message."))))
        try:
            with request.env.cr.savepoint():
                record.action_customer_message(message)
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(f"{url}?dispute_updated=1")

    @http.route("/my/marketplace/disputes/<int:dispute_id>/escalate", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def customer_escalate_dispute(self, dispute_id, **post):
        record = self._customer_dispute(dispute_id)
        url = f"/my/marketplace/orders/{record.vendor_order_id.id}"
        try:
            with request.env.cr.savepoint():
                record.action_escalate()
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(f"{url}?dispute_updated=1")

    @http.route("/my/marketplace/vendor-invoices/<int:invoice_id>", type="http", auth="user", website=True)
    def marketplace_invoice_pdf(self, invoice_id, **kw):
        """Vendor invoice or credit note, for its customer or its vendor."""
        partner = request.env.user.partner_id.commercial_partner_id
        invoice = request.env["leaders.vendor.invoice"].sudo().browse(invoice_id).exists()
        allowed = invoice and (
            invoice.partner_id.commercial_partner_id == partner
            or (invoice.vendor_id == partner and self._is_vendor_user()))
        if not allowed or (invoice.state != "posted" and invoice.vendor_id != partner):
            raise AccessError(_("You cannot access this document."))
        return self._render_vendor_invoice_pdf(invoice)

    @http.route("/my/marketplace/orders/<int:order_id>/invoice", type="http", auth="user", website=True)
    def customer_marketplace_invoice_pdf(self, order_id, **kw):
        order = self._customer_vendor_order(order_id)
        if not order.invoice_id or order.invoice_id.state != "posted":
            raise AccessError(_("This order has no vendor invoice."))
        return self._render_vendor_invoice_pdf(order.invoice_id)


class MarketplaceSaleOrdersPortal(SaleCustomerPortal):

    def _prepare_orders_domain(self, partner):
        domain = super()._prepare_orders_domain(partner)
        if request.env["sale.order"]._marketplace_vendor_only_presentation():
            # Vendor-only checkouts live under /my/marketplace/orders.
            domain = domain + [("marketplace_vendor_only", "=", False)]
        return domain
