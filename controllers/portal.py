import base64
import os
from datetime import timedelta
from urllib.parse import quote

from werkzeug.exceptions import HTTPException, NotFound

from odoo import fields, http, _
from odoo.http import request
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import email_normalize
from odoo.tools.float_utils import float_compare, float_is_zero
from odoo.tools.mimetypes import guess_mimetype
from odoo.addons.leaders_marketplace.models.product_question import ANSWER_MAX_LENGTH
from odoo.addons.leaders_marketplace.models.returns import MAX_RETURN_PHOTOS
from odoo.addons.leaders_marketplace.controllers.vendor_apply import VendorApplyController
from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.sale.controllers.portal import CustomerPortal as SaleCustomerPortal


# D2: filter chips of the returns center: chip -> (return states, dispute states)
CENTER_FILTERS = {
    "active": (("requested", "rejected", "escalated", "approved", "received"), ("open", "vendor_reply", "escalated")),
    "completed": (("refunded", "exchanged"), ("resolved",)),
    "closed": (("closed", "cancelled"), ("closed",)),
}


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

    def _require_vendor(self, allow_frozen=False, check_terms=True):
        """The vendor behind the current user, or an AccessError.

        A frozen vendor still has obligations -- orders to ship, returns to
        answer, money to be settled -- so the pages that carry those pass
        ``allow_frozen=True``. Everything that puts new goods on sale stays
        closed until the freeze is lifted. A banned vendor has no active user
        at all, so they never reach here.

        While an active vendor terms version is unsigned by the vendor, the
        owner is redirected to /my/vendor/terms and staff get a notice page
        (raised as an HTTP response, so no route needs to know). Frozen vendors
        are held to the terms as well: accepting is one click, and the terms
        page itself is reachable. ``check_terms=False`` is for that page only.
        With no active terms record nothing is checked.
        """
        if not self._is_vendor_user(allow_frozen=allow_frozen):
            vendor = self._vendor_partner()
            if vendor.is_marketplace_vendor and vendor.vendor_state == "frozen":
                raise AccessError(_(
                    "Your vendor account is frozen, so you cannot list or change "
                    "products. You can still handle your existing orders and returns."))
            raise AccessError(_("Your account is not an approved marketplace vendor."))
        vendor = self._vendor_partner()
        if check_terms:
            terms = request.env["leaders.vendor.terms"]._pending_for(vendor)
            if terms:
                self._raise_terms_required(terms, vendor)
        return vendor

    def _raise_terms_required(self, terms, vendor):
        if self._is_vendor_owner():
            raise HTTPException(response=request.redirect("/my/vendor/terms"))
        html = request.render("leaders_marketplace.portal_vendor_terms_staff_notice", {
            "vendor": vendor, "terms": terms}, lazy=False)
        raise HTTPException(response=request.make_response(html, status=403))

    def _terms_page_values(self, vendor, **extra):
        Terms = request.env["leaders.vendor.terms"]
        terms = Terms._active_terms()
        contract = request.env["leaders.vendor.contract"].sudo().search([
            ("vendor_id", "=", vendor.id), ("terms_id", "=", terms.id), ("state", "=", "signed")],
            limit=1) if terms else request.env["leaders.vendor.contract"]
        values = {"vendor": vendor, "terms": terms, "contract": contract,
                  "is_owner": self._is_vendor_owner(), "error": False, "freeze_notice": False}
        values.update(extra)
        return values

    @http.route("/my/vendor/terms", type="http", auth="user", website=True)
    def vendor_terms(self, **kw):
        vendor = self._require_vendor(allow_frozen=True, check_terms=False)
        return request.render("leaders_marketplace.portal_vendor_terms", self._terms_page_values(vendor))

    @http.route("/my/vendor/terms/accept", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_terms_accept(self, **post):
        vendor = self._require_vendor(allow_frozen=True, check_terms=False)
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can accept the terms."))
        terms = request.env["leaders.vendor.terms"]._active_terms()
        if not terms:
            return request.redirect("/my/vendor")
        if not post.get("accept"):
            return request.render("leaders_marketplace.portal_vendor_terms", self._terms_page_values(
                vendor, error=_("You must tick the box to accept the terms.")))
        terms.action_accept(vendor, request.env.user, request.httprequest.remote_addr)
        return request.redirect("/my/vendor")

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
        values = {
            "vendor": vendor,
            "offer": offer,
            "vendor_owns_content": owns_content,
            "fbl_enabled": request.env["leaders.marketplace.offer"]._leaders_fbl_allowed(),
            "forced_fulfillment": forced_policy.forced_fulfillment_type or False,
            "forced_fulfillment_name": forced_policy.name or "",
            "discounts_enabled": request.env["leaders.marketplace.offer"]._leaders_discounts_enabled(),
            "discount_start_local": self._dt_to_local(offer.discount_start) if offer else "",
            "discount_end_local": self._dt_to_local(offer.discount_end) if offer else "",
            "categories": request.env["product.public.category"].sudo().search([], order="name"),
            "websites": request.env["website"].sudo().search([]),
            "currencies": request.env["res.currency"].sudo().search([("active", "=", True)], order="name"),
            "error": error,
        }
        values.update(self._vendor_variants_form_values(offer))
        return values

    def _vendor_variants_form_values(self, offer=None):
        """B3 form data (options, extra images, specifications); inert while the switch is off."""
        Template = request.env["product.template"]
        enabled = Template._leaders_variants_enabled()
        values = {"variants_enabled": enabled}
        if not enabled:
            return values
        tmpl = offer.product_tmpl_id.sudo() if offer else Template.browse()
        values.update({
            "attr_options": Template._leaders_pickable_attributes(),
            "attr_selected": tmpl._leaders_current_selection() if tmpl else {},
            "allow_new_attrs": Template._leaders_allow_vendor_attributes(),
            "max_extra_images": Template._leaders_max_extra_images(),
            "extra_images": tmpl.product_template_image_ids if tmpl else request.env["product.image"],
            "tmpl_specs": tmpl.leaders_specs if tmpl else "",
            "variant_tmpl": tmpl,
            "variant_count": len(tmpl.product_variant_ids) if tmpl else 0,
        })
        return values

    def _vendor_over_offer_limit(self, vendor, count=1):
        tier = vendor.vendor_tier_id
        if not (tier and tier.product_limit):
            return False
        current = request.env["leaders.marketplace.offer"].sudo().search_count([
            ("vendor_id", "=", vendor.id),
            ("active", "=", True),
        ])
        return current + count > tier.product_limit

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

    # -- B3: variants, extra images, specifications ----------------------------
    def _vendor_b3_parse(self, post):
        """Read the B3 part of the product form; raise UserError on anything invalid.

        Nothing is written here. Returns None when the form did not carry the B3 card.
        """
        form = request.httprequest.form
        if not form.get("lm_variants_form"):
            return None
        Template = request.env["product.template"]
        selection = {}
        for key in form:
            if key.startswith("attr_values_") and key[len("attr_values_"):].isdigit():
                selection[int(key[len("attr_values_"):])] = [
                    int(v) for v in form.getlist(key) if str(v).isdigit()]
        new_values = {}
        for key in form:
            if key.startswith("new_values_") and key[len("new_values_"):].isdigit() and form.get(key, "").strip():
                new_values[int(key[len("new_values_"):])] = form.get(key).split(",")
        new_attr = None
        if (form.get("new_attr_name") or "").strip():
            new_attr = {"name": form.get("new_attr_name"), "values": (form.get("new_attr_values") or "").split(","),
                        "spec": form.get("new_attr_kind") == "spec"}
        if (new_values or new_attr) and not Template._leaders_allow_vendor_attributes():
            raise UserError(_("Adding new attribute values is not allowed."))
        images = []
        for image_file in request.httprequest.files.getlist("extra_images"):
            image_b64, image_error = self._read_vendor_image(image_file)
            if image_error:
                raise UserError(image_error)
            if image_b64:
                images.append((image_b64, image_file.filename))
        remove_ids = [int(v) for v in form.getlist("remove_image_ids") if str(v).isdigit()]
        return {
            "selection": selection, "new_values": new_values, "new_attr": new_attr,
            "images": images, "remove_ids": remove_ids,
            "specs": Template._leaders_clean_specs(form.get("leaders_specs")),
        }

    def _vendor_b3_apply(self, tmpl, parsed):
        """Write the parsed B3 data on an owned template (atomic: one savepoint)."""
        if not parsed:
            return
        Template = request.env["product.template"]
        tmpl = tmpl.sudo()
        with request.env.cr.savepoint():
            tmpl._leaders_check_extra_images(len(parsed["images"]), parsed["remove_ids"])
            selection = {k: list(v) for k, v in parsed["selection"].items()}
            for attribute_id, names in parsed["new_values"].items():
                attribute = Template._leaders_pickable_attributes().filtered(lambda a: a.id == attribute_id)
                if not attribute:
                    raise UserError(_("This attribute cannot be used."))
                created = Template._leaders_create_vendor_attribute_values(attribute, names)
                selection.setdefault(attribute_id, []).extend(created.ids)
            if parsed["new_attr"]:
                attribute, created = Template._leaders_create_vendor_attribute(
                    parsed["new_attr"]["name"], parsed["new_attr"]["values"], as_spec=parsed["new_attr"]["spec"])
                if attribute not in Template._leaders_pickable_attributes():
                    raise UserError(_("This attribute cannot be used."))
                selection.setdefault(attribute.id, []).extend(created.ids)
            tmpl._leaders_apply_attribute_selection(selection)
            tmpl._leaders_remove_extra_images(parsed["remove_ids"])
            for image_b64, filename in parsed["images"]:
                tmpl._leaders_add_extra_image(image_b64, name=filename)
            tmpl.leaders_specs = parsed["specs"] or False

    def _template_owned_by_vendor(self, tmpl_id, vendor):
        """Template the vendor owns outright (variants pages); feature off -> 404, else AccessError."""
        Template = request.env["product.template"]
        if not Template._leaders_variants_enabled():
            raise NotFound()
        tmpl = Template.sudo().browse(tmpl_id).exists()
        if not tmpl or not tmpl._leaders_vendor_owns_template(vendor):
            raise AccessError(_("You cannot access this product."))
        return tmpl

    def _variants_page_values(self, vendor, tmpl, **extra):
        Offer = request.env["leaders.marketplace.offer"].sudo()
        offers = Offer.search([("vendor_id", "=", vendor.id), ("product_tmpl_id", "=", tmpl.id)])
        missing = tmpl._leaders_variants_without_offer(vendor)
        args = request.params
        values = dict(self._vendor_form_values(vendor), **{
            "tmpl": tmpl, "offers": offers, "missing": missing,
            "form": {k: args.get(k, "") for k in ("sku", "price", "fulfillment_type", "currency_id")},
            "form_markets": [int(x) for x in (args.get("markets") or "").split(",") if x.isdigit()]
                            or offers[:1].market_ids.ids,
            "form_fulfillment": args.get("fulfillment_type") or offers[:1].fulfillment_type or "fbm",
            "form_currency": int(args.get("currency_id") or 0) if str(args.get("currency_id") or "").isdigit()
                             else (offers[:1].currency_id.id or request.env.company.currency_id.id),
            "discounts_enabled": Offer._leaders_discounts_enabled(),
            "error": False, "notice": args.get("notice"),
        })
        values.update(extra)
        return values

    @http.route("/my/vendor/templates/<int:tmpl_id>/variants", type="http", auth="user", website=True,
                methods=["GET", "POST"], csrf=True)
    def vendor_template_variants(self, tmpl_id, **post):
        """Offers of the variants of an owned template: table of existing offers, one row per
        variant still without an offer (POST creates one draft offer per ticked row)."""
        vendor = self._require_vendor()
        tmpl = self._template_owned_by_vendor(tmpl_id, vendor)
        if request.httprequest.method == "GET":
            return self._vendor_render("leaders_marketplace.portal_vendor_variants",
                                       self._variants_page_values(vendor, tmpl))
        form = request.httprequest.form
        Offer = request.env["leaders.marketplace.offer"]
        missing = tmpl._leaders_variants_without_offer(vendor)
        rows = []
        for product in missing:
            if not form.get("create_%s" % product.id):
                continue
            row = {
                "product": product,
                "sku": (form.get("sku_%s" % product.id) or "").strip(),
                "price": self._to_float(form.get("price_%s" % product.id)),
                "stock": self._to_float(form.get("stock_%s" % product.id)),
            }
            discount = (form.get("discount_%s" % product.id) or "").strip()
            if discount:
                row["discount_percent"] = discount
            rows.append(row)
        fulfillment_type = form.get("fulfillment_type") or "fbm"
        currency = request.env["res.currency"].sudo().browse(
            int(form.get("currency_id") or 0) if str(form.get("currency_id") or "").isdigit() else 0
        ).exists() or request.env.company.currency_id
        markets = request.env["website"].sudo().browse(
            [int(x) for x in form.getlist("market_ids") if x.isdigit()]).exists()
        try:
            if rows and any(r.get("discount_percent") for r in rows) and not Offer._leaders_discounts_enabled():
                raise UserError(_("Vendor discounts are not enabled."))
            with request.env.cr.savepoint():
                Offer._leaders_create_variant_offers(vendor, tmpl, rows, fulfillment_type, currency, markets)
        except (UserError, ValidationError) as exc:
            return self._vendor_render("leaders_marketplace.portal_vendor_variants",
                                       self._variants_page_values(vendor, tmpl, error=str(exc)))
        return request.redirect("/my/vendor/templates/%s/variants?notice=%s" % (
            tmpl.id, quote(_("%s offer(s) created as drafts.", len(rows)))))

    @http.route("/my/vendor/templates/<int:tmpl_id>/submit", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_template_submit(self, tmpl_id, **post):
        """Send every draft/rejected variant offer of an owned template for review."""
        vendor = self._require_vendor()
        tmpl = self._template_owned_by_vendor(tmpl_id, vendor)
        offers = request.env["leaders.marketplace.offer"].sudo().search([
            ("vendor_id", "=", vendor.id), ("product_tmpl_id", "=", tmpl.id),
            ("state", "in", ("draft", "rejected"))])
        for offer in offers:
            offer._leaders_vendor_submit(vendor)
        return request.redirect("/my/vendor/templates/%s/variants?notice=%s" % (
            tmpl.id, quote(_("%s offer(s) submitted for review.", len(offers)))))

    @staticmethod
    def _to_float(value, default=0.0):
        try:
            return float(value or default)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_days(value):
        """Non-negative whole days from a form value (empty/invalid = 0)."""
        try:
            return max(int(float(value or 0)), 0)
        except (TypeError, ValueError):
            return 0

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
            "orders_late": VendorOrder.search_count(vendor_domain + [("is_late", "=", True)]),
            "returns_to_answer": Return.search_count(vendor_domain + [("state", "=", "requested")]),
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
            "fbl_enabled": request.env["leaders.marketplace.offer"]._leaders_fbl_allowed(),
            "discounts_enabled": request.env["leaders.marketplace.offer"]._leaders_discounts_enabled(),
            "bulk_import_enabled": request.env["leaders.vendor.import"]._leaders_import_enabled(),
            "offers": offers,
            "vendor": vendor,
            "error": kw.get("error"),
            "notice": kw.get("notice"),
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

        if fulfillment_type == "fbl" and not request.env["leaders.marketplace.offer"]._leaders_fbl_allowed():
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._vendor_form_values(vendor, error=request.env["res.partner"]._leaders_fbl_disabled_message()),
            )
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

        try:
            b3 = self._vendor_b3_parse(post) if request.env["product.template"]._leaders_variants_enabled() else None
            if b3 and len(b3["images"]) > request.env["product.template"]._leaders_max_extra_images():
                raise UserError(_("A product can have at most %s extra images.",
                                  request.env["product.template"]._leaders_max_extra_images()))
        except UserError as exc:
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._vendor_form_values(vendor, error=str(exc)))

        try:
            with request.env.cr.savepoint():
                tmpl = request.env["product.template"].sudo().create(template_vals)
                if b3:
                    self._vendor_b3_apply(tmpl, b3)
                    if len(tmpl.product_variant_ids) > 1 and self._vendor_over_offer_limit(
                            vendor, len(tmpl.product_variant_ids)):
                        raise UserError(_("Your vendor tier product limit has been reached."))
        except (UserError, ValidationError) as exc:
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._vendor_form_values(vendor, error=str(exc)))
        if b3 and len(tmpl.product_variant_ids) > 1:
            return request.redirect("/my/vendor/templates/%s/variants?created=1&sku=%s&price=%s&fulfillment_type=%s&currency_id=%s&markets=%s" % (
                tmpl.id, quote(seller_sku), price, quote(fulfillment_type), currency_id,
                ",".join(str(w) for w in website_ids)))
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
        if fulfillment_type == "fbl" and not Offer._leaders_fbl_allowed():
            error = request.env["res.partner"]._leaders_fbl_disabled_message()
        elif not product or product.product_tmpl_id.marketplace_state != "published" or not product.sale_ok:
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
            values["discount_result"] = request.params.get("discount")
            return self._vendor_render("leaders_marketplace.portal_vendor_product_form", values)

        name = (post.get("name") or "").strip()
        seller_sku = (post.get("seller_sku") or "").strip()
        price = self._to_float(post.get("price"))
        fulfillment_type = post.get("fulfillment_type") or offer.fulfillment_type
        category_id = int(post.get("public_category_id") or 0)
        currency_id = int(post.get("currency_id") or offer.currency_id.id)
        website_ids = [int(x) for x in request.httprequest.form.getlist("market_ids") if x]
        if fulfillment_type == "fbl" and not request.env["leaders.marketplace.offer"]._leaders_fbl_allowed():
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._vendor_form_values(vendor, offer=offer, error=request.env["res.partner"]._leaders_fbl_disabled_message()),
            )
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

        b3 = None
        if request.env["product.template"]._leaders_variants_enabled() \
                and offer.product_tmpl_id.sudo()._leaders_vendor_owns_content(vendor):
            try:
                b3 = self._vendor_b3_parse(post)
                self._vendor_b3_apply(offer.product_tmpl_id, b3)
            except (UserError, ValidationError) as exc:
                return self._vendor_render(
                    "leaders_marketplace.portal_vendor_product_form",
                    self._vendor_form_values(vendor, offer=offer, error=str(exc)))

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

    # -------------------------------------------------------------------------
    # Vendor discounts (B1)
    # -------------------------------------------------------------------------
    @staticmethod
    def _user_tz():
        import pytz
        try:
            return pytz.timezone(request.env.user.tz or "UTC")
        except pytz.UnknownTimeZoneError:
            return pytz.UTC

    def _dt_from_local(self, value):
        """A ``datetime-local`` form value (the user's time zone) as a UTC datetime, or False."""
        import pytz
        from datetime import datetime
        value = (value or "").strip()
        if not value:
            return False
        try:
            local = datetime.strptime(value.replace("T", " ")[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            raise UserError(_("The discount dates are not valid."))
        return self._user_tz().localize(local).astimezone(pytz.UTC).replace(tzinfo=None)

    def _dt_to_local(self, value):
        import pytz
        if not value:
            return ""
        return pytz.UTC.localize(value).astimezone(self._user_tz()).strftime("%Y-%m-%dT%H:%M")

    def _discount_values_from_post(self, post, percent_only=False):
        kind = "percent" if percent_only else post.get("discount_type")
        return {
            "type": kind,
            "percent": post.get("discount_percent"),
            "price": post.get("discount_price"),
            "start": self._dt_from_local(post.get("discount_start")),
            "end": self._dt_from_local(post.get("discount_end")),
        }

    @http.route("/my/vendor/products/<int:offer_id>/discount", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_product_discount(self, offer_id, **post):
        """Set or remove the discount of one own offer (same service as bulk import / API)."""
        vendor = self._require_vendor()
        offer = self._offer_owned_by_vendor(offer_id, vendor)
        if post.get("action") == "clear":
            offer._leaders_clear_discount()
            return request.redirect(f"/my/vendor/products/{offer.id}/edit?discount=cleared")
        try:
            state = offer._leaders_set_discount(self._discount_values_from_post(post))
        except (UserError, ValidationError) as exc:
            return self._vendor_render(
                "leaders_marketplace.portal_vendor_product_form",
                self._vendor_form_values(vendor, offer=offer, error=str(exc)))
        return request.redirect(f"/my/vendor/products/{offer.id}/edit?discount={state}")

    @http.route("/my/vendor/products/discount-bulk", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_products_discount_bulk(self, **post):
        """Apply one percentage (and dates) to several own offers, or remove their discounts."""
        vendor = self._require_vendor()
        ids = []
        for raw in request.httprequest.form.getlist("offer_ids"):
            if str(raw).isdigit():
                ids.append(int(raw))
        if not ids:
            return request.redirect("/my/vendor/products?error=%s" % quote(_("Select at least one offer.")))
        # Ownership first: a single foreign id refuses the whole request.
        offers = request.env["leaders.marketplace.offer"].sudo()
        for offer_id in ids:
            offers |= self._offer_owned_by_vendor(offer_id, vendor)
        if post.get("action") == "clear":
            offers._leaders_clear_discount()
            return request.redirect("/my/vendor/products?notice=%s" % quote(
                _("Discount removed from %s offer(s).", len(offers))))
        try:
            values = self._discount_values_from_post(post, percent_only=True)
        except UserError as exc:
            return request.redirect("/my/vendor/products?error=%s" % quote(str(exc)))
        applied, failed = 0, []
        for offer in offers:
            try:
                with request.env.cr.savepoint():
                    offer._leaders_set_discount(values)
                applied += 1
            except (UserError, ValidationError) as exc:
                failed.append("%s: %s" % (offer.seller_sku, exc))
        if failed and not applied:
            return request.redirect("/my/vendor/products?error=%s" % quote("; ".join(failed)))
        notice = _("Discount applied to %s offer(s).", applied)
        if failed:
            notice += " " + _("Skipped: %s", "; ".join(failed))
        return request.redirect("/my/vendor/products?notice=%s" % quote(notice))

    @http.route("/my/vendor/products/<int:offer_id>/submit", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_product_submit(self, offer_id, **post):
        vendor = self._require_vendor()
        offer = self._offer_owned_by_vendor(offer_id, vendor)
        # Unpublishing a shared template would pull other sellers' offers off
        # the website too; only the sole owner may send it back for review.
        offer._leaders_vendor_submit(vendor)
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
            try:
                offer._leaders_set_fbm_stock(qty)
            except UserError as exc:
                return self._vendor_stock_error(str(exc))
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
    # Bulk import (B2)
    # -------------------------------------------------------------------------
    def _import_vendor(self):
        """The vendor allowed to import (same rule as product creation); 404 when the feature is off."""
        if not request.env["leaders.vendor.import"]._leaders_import_enabled():
            raise NotFound()
        return self._require_vendor()

    def _import_job(self, job_id, vendor):
        job = request.env["leaders.vendor.import"].sudo().search([
            ("id", "=", job_id), ("vendor_id", "=", vendor.id)], limit=1)
        if not job:
            raise NotFound()
        return job

    def _import_kinds(self):
        kinds = request.env["leaders.vendor.import"]._fields["kind"]._description_selection(request.env)
        if not request.env["res.partner"]._marketplace_catalog_enabled():
            kinds = [k for k in kinds if k[0] != "catalog_offers"]
        return kinds

    def _import_index_values(self, vendor, **extra):
        values = {
            "vendor": vendor, "kinds": self._import_kinds(), "error": False,
            "jobs": request.env["leaders.vendor.import"].sudo().search(
                [("vendor_id", "=", vendor.id)], limit=20),
        }
        values.update(extra)
        return values

    @http.route("/my/vendor/import", type="http", auth="user", website=True)
    def vendor_import(self, **kw):
        vendor = self._import_vendor()
        return self._vendor_render("leaders_marketplace.portal_vendor_import", self._import_index_values(vendor))

    @http.route("/my/vendor/import/template/<any(new_products,catalog_offers,update):kind>.<any(xlsx,csv):fmt>",
                type="http", auth="user", website=True)
    def vendor_import_template(self, kind, fmt, **kw):
        self._import_vendor()
        if kind not in [k[0] for k in self._import_kinds()]:
            raise NotFound()
        content, mimetype, filename = request.env["leaders.vendor.import"]._leaders_build_template(kind, fmt)
        return request.make_response(content, headers=[
            ("Content-Type", mimetype),
            ("Content-Disposition", "attachment; filename=%s" % filename)])

    @http.route("/my/vendor/import/upload", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_import_upload(self, **post):
        vendor = self._import_vendor()
        kind = post.get("kind")
        upload = request.httprequest.files.get("file")
        error = False
        if kind not in [k[0] for k in self._import_kinds()]:
            error = _("Choose what you want to import.")
        elif not upload or not upload.filename:
            error = _("Choose a file to upload.")
        elif not upload.filename.lower().endswith((".xlsx", ".csv")):
            error = _("Upload an .xlsx or .csv file.")
        data = b"" if error else upload.read(10 * 1024 * 1024 + 1)
        if not error and (not data or len(data) > 10 * 1024 * 1024):
            error = _("The file is empty or larger than 10 MB.")
        if error:
            return self._vendor_render("leaders_marketplace.portal_vendor_import",
                                       self._import_index_values(vendor, error=error))
        Import = request.env["leaders.vendor.import"].sudo()
        job = Import.create({
            "vendor_id": vendor.id, "user_id": request.env.user.id, "kind": kind,
            "file": base64.b64encode(data), "filename": upload.filename,
            "submit_after_import": bool(post.get("submit_after_import")),
        })
        if job._leaders_count_rows() <= Import._LEADERS_SYNC_ROWS:
            job.action_validate()
        else:
            Import._leaders_trigger_cron()
        return request.redirect("/my/vendor/import/%s" % job.id)

    @http.route("/my/vendor/import/<int:job_id>", type="http", auth="user", website=True)
    def vendor_import_job(self, job_id, **kw):
        vendor = self._import_vendor()
        job = self._import_job(job_id, vendor)
        preview = [r for r in job._leaders_error_rows()][:30]
        return self._vendor_render("leaders_marketplace.portal_vendor_import_job", {
            "vendor": vendor, "job": job, "error_preview": preview,
            "error_count": len(job._leaders_error_rows()),
            "error": kw.get("error"),
        })

    @http.route("/my/vendor/import/<int:job_id>/confirm", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_import_confirm(self, job_id, **post):
        vendor = self._import_vendor()
        job = self._import_job(job_id, vendor)
        try:
            job.action_confirm()
        except UserError as exc:
            return request.redirect("/my/vendor/import/%s?error=%s" % (job.id, quote(str(exc))))
        return request.redirect("/my/vendor/import/%s" % job.id)

    @http.route("/my/vendor/import/<int:job_id>/errors.<any(csv,xlsx):fmt>", type="http", auth="user", website=True)
    def vendor_import_errors(self, job_id, fmt, **kw):
        vendor = self._import_vendor()
        job = self._import_job(job_id, vendor)
        content, mimetype, filename = job._leaders_build_error_file(fmt)
        return request.make_response(content, headers=[
            ("Content-Type", mimetype),
            ("Content-Disposition", "attachment; filename=%s" % filename)])

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
            "lm_is_vendor_owner": self._is_vendor_owner(),
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
                tracking = (post.get("tracking_ref") or "").strip()
                # One field per order line ("qty_<line id>"); the form defaults to
                # the full available quantity. No such field: ship everything.
                quantities = {}
                for key, value in post.items():
                    if key.startswith("qty_") and key[4:].isdigit():
                        try:
                            quantities[int(key[4:])] = float(value or 0.0)
                        except ValueError:
                            raise UserError(_("Invalid shipment quantity."))
                vals = {"carrier_id": int(post.get("carrier_id") or 0) or False}
                if not group.picking_ids.filtered(lambda p: p.state == "done"):
                    vals["tracking_ref"] = tracking
                group.sudo().write(vals)
                group.sudo().action_ship(
                    quantities=quantities if quantities else None, tracking_ref=tracking)
        except UserError as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(url)

    @http.route("/my/vendor/fulfillment/<int:group_id>/create-shipment", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_fulfillment_create_shipment(self, group_id, **post):
        vendor = self._require_vendor(allow_frozen=True)
        Provider = request.env["leaders.shipping.provider"]
        if not Provider._leaders_enabled():
            raise NotFound()
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
                provider = Provider._leaders_portal_providers().filtered(
                    lambda p: str(p.id) == str(post.get("provider_id") or ""))
                if not provider:
                    raise UserError(_("Choose a shipping provider."))
                group.sudo().shipping_provider_id = provider
                group.sudo().action_create_shipment()
        except UserError as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(url)

    @http.route("/my/vendor/fulfillment/<int:group_id>/label", type="http", auth="user", website=True, methods=["GET"])
    def vendor_fulfillment_label(self, group_id, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        if not request.env["leaders.shipping.provider"]._leaders_enabled():
            raise NotFound()
        group = request.env["leaders.fulfillment.group"].sudo().search([
            ("id", "=", group_id), ("vendor_id", "=", vendor.id)
        ], limit=1)
        attachment = group.label_attachment_id
        if not group or not attachment:
            raise NotFound()
        return request.make_response(attachment.raw, headers=[
            ("Content-Type", attachment.mimetype or "application/pdf"),
            ("Content-Disposition", 'attachment; filename="%s"' % (attachment.name or "label.pdf").replace('"', "")),
            ("X-Content-Type-Options", "nosniff"),
        ])

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

    @http.route("/my/vendor/fulfillment/<int:group_id>/refused", type="http", auth="user", website=True, methods=["POST"], csrf=True)
    def vendor_fulfillment_refused(self, group_id, **post):
        """FBM + Cash on Delivery: the customer refused the parcel at the door."""
        vendor = self._require_vendor(allow_frozen=True)
        group = request.env["leaders.fulfillment.group"].sudo().search([
            ("id", "=", group_id), ("vendor_id", "=", vendor.id), ("fulfillment_type", "=", "fbm"),
        ], limit=1)
        if not group:
            raise AccessError(_("You cannot access this fulfillment group."))
        url = f"/my/vendor/orders/{group.vendor_order_id.id}"
        try:
            with request.env.cr.savepoint():
                group.action_customer_refused(
                    reason=(post.get("reason") or "")[:500], by=request.env.user)
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
    # Vendor statement of account (B5)
    # -------------------------------------------------------------------------
    def _statement_owner(self):
        vendor = self._require_vendor(allow_frozen=True)
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can access financial information."))
        return vendor

    def _statement_for(self, vendor, kw):
        """The statement of the current vendor (never another one) for the query dates."""
        return vendor._leaders_vendor_statement(kw.get("date_from"), kw.get("date_to"))

    def _statement_download(self, content, filename, mimetype):
        return request.make_response(content, headers=[
            ("Content-Type", mimetype),
            ("Content-Length", len(content)),
            ("Content-Disposition", 'attachment; filename="%s"' % filename),
        ])

    @http.route("/my/vendor/finance/statement", type="http", auth="user", website=True)
    def vendor_statement(self, **kw):
        vendor = self._statement_owner()
        error = False
        try:
            statement = self._statement_for(vendor, kw)
        except UserError as e:
            error = e.args[0]
            statement = vendor._leaders_vendor_statement()
        return self._vendor_render("leaders_marketplace.portal_vendor_statement", {
            "vendor": vendor, "st": statement, "error": error,
            "query": "date_from=%s&date_to=%s" % (statement["date_from"], statement["date_to"]),
        })

    @http.route("/my/vendor/finance/statement.pdf", type="http", auth="user", website=True)
    def vendor_statement_pdf(self, **kw):
        vendor = self._statement_owner()
        try:
            statement = self._statement_for(vendor, kw)
        except UserError as e:
            return request.make_response(e.args[0], headers=[("Content-Type", "text/plain")], status=400)
        report = request.env["ir.actions.report"].sudo()
        pdf, _type = report._render_qweb_pdf(
            "leaders_marketplace.action_report_vendor_statement", vendor.ids,
            data={"date_from": str(statement["date_from"]), "date_to": str(statement["date_to"])})
        filename = request.env["leaders.vendor.statement"]._filename(statement, "pdf")
        return request.make_response(pdf, headers=[
            ("Content-Type", "application/pdf"),
            ("Content-Length", len(pdf)),
            ("Content-Disposition", 'inline; filename="%s"' % filename),
        ])

    @http.route("/my/vendor/finance/statement.xlsx", type="http", auth="user", website=True)
    def vendor_statement_xlsx(self, **kw):
        vendor = self._statement_owner()
        try:
            statement = self._statement_for(vendor, kw)
        except UserError as e:
            return request.make_response(e.args[0], headers=[("Content-Type", "text/plain")], status=400)
        Statement = request.env["leaders.vendor.statement"]
        return self._statement_download(
            Statement._xlsx(statement), Statement._filename(statement, "xlsx"),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @http.route("/leaders/vendor_statement/xlsx", type="http", auth="user")
    def backend_vendor_statement_xlsx(self, vendor_id=None, date_from=None, date_to=None, **kw):
        """Backend download (vendor form wizard): managers and finance only."""
        user = request.env.user
        if not (user.has_group("leaders_marketplace.group_marketplace_manager")
                or user.has_group("leaders_marketplace.group_marketplace_finance")):
            raise AccessError(_("Only marketplace managers and finance can print vendor statements."))
        vendor = request.env["res.partner"].sudo().browse(int(vendor_id or 0)).exists()
        if not vendor or not vendor.is_marketplace_vendor:
            raise NotFound()
        try:
            statement = vendor._leaders_vendor_statement(date_from, date_to)
        except UserError as e:
            return request.make_response(e.args[0], headers=[("Content-Type", "text/plain")], status=400)
        Statement = request.env["leaders.vendor.statement"]
        return self._statement_download(
            Statement._xlsx(statement), Statement._filename(statement, "xlsx"),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # -------------------------------------------------------------------------
    # Vendor API keys (B4)
    # -------------------------------------------------------------------------
    def _api_keys_owner(self):
        """The vendor owner allowed to manage API keys; 404 while the write API is off."""
        if not request.env["leaders.vendor.api.key"]._leaders_api_enabled():
            raise NotFound()
        vendor = self._require_vendor(allow_frozen=True)
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can manage API keys."))
        return vendor

    def _api_keys_render(self, vendor, **extra):
        values = {
            "vendor": vendor, "error": False, "new_key": False, "revoked": False,
            "api_base": request.httprequest.host_url.rstrip("/"),
            "keys": request.env["leaders.vendor.api.key"].sudo().search(
                [("vendor_id", "=", vendor.id)]),
        }
        values.update(extra)
        return self._vendor_render("leaders_marketplace.portal_vendor_api", values)

    @http.route("/my/vendor/api", type="http", auth="user", website=True)
    def vendor_api_keys(self, **kw):
        vendor = self._api_keys_owner()
        return self._api_keys_render(vendor, revoked=bool(kw.get("revoked")))

    @http.route("/my/vendor/api/keys", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_api_key_create(self, **post):
        vendor = self._api_keys_owner()
        try:
            with request.env.cr.savepoint():
                _key, plaintext = request.env["leaders.vendor.api.key"]._leaders_generate(
                    vendor, request.env.user, post.get("name"), post.get("scope") or "read")
        except UserError as exc:
            return self._api_keys_render(vendor, error=str(exc))
        # The plaintext is rendered in this response only; nothing keeps it.
        response = self._api_keys_render(vendor, new_key=plaintext)
        response.headers["Cache-Control"] = "no-store"
        return response

    @http.route("/my/vendor/api/keys/<int:key_id>/revoke", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_api_key_revoke(self, key_id, **post):
        vendor = self._api_keys_owner()
        key = request.env["leaders.vendor.api.key"].sudo().search([
            ("id", "=", key_id), ("vendor_id", "=", vendor.id)], limit=1)
        if not key:
            raise NotFound()
        key.action_revoke()
        return request.redirect("/my/vendor/api?revoked=1")

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
                    "free_shipping_threshold": max(self._to_float(post.get("free_shipping_threshold"), 0.0), 0.0),
                    "lead_days_min": self._to_days(post.get("lead_days_min")),
                    "lead_days_max": self._to_days(post.get("lead_days_max")),
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
                vals = {"price": price}
                if "free_shipping_threshold" in post:
                    vals["free_shipping_threshold"] = max(
                        self._to_float(post.get("free_shipping_threshold"), 0.0), 0.0)
                for key in ("lead_days_min", "lead_days_max"):
                    if key in post:
                        vals[key] = self._to_days(post.get(key))
                zone.write(vals)
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

    # -------------------------------------------------------------------------
    # Product questions (D3): behind the master switch, 404 while it is off
    # -------------------------------------------------------------------------
    @staticmethod
    def _require_questions_enabled():
        if not request.env["leaders.product.question"]._leaders_enabled():
            raise NotFound()

    @http.route("/my/vendor/questions", type="http", auth="user", website=True)
    def vendor_questions(self, **kw):
        self._require_questions_enabled()
        vendor = self._require_vendor(allow_frozen=True)
        Question = request.env["leaders.product.question"].sudo()
        templates = Question._leaders_vendor_templates(vendor)
        records = Question.search([("product_tmpl_id", "in", templates.ids)], limit=300)
        flt = kw.get("filter") if kw.get("filter") in ("answered", "all") else "unanswered"
        return self._vendor_render("leaders_marketplace.portal_vendor_questions", {
            "records": records, "vendor": vendor, "lm_flt": flt,
            "editable_ids": [r.id for r in records if r._vendor_can_edit(vendor)],
            "answer_max_length": ANSWER_MAX_LENGTH,
            "error": kw.get("error"), "saved": kw.get("saved"),
        })

    @http.route("/my/vendor/questions/<int:question_id>/answer", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def vendor_question_answer(self, question_id, **post):
        self._require_questions_enabled()
        vendor = self._require_vendor(allow_frozen=True)
        record = request.env["leaders.product.question"].sudo().browse(question_id).exists()
        if not record:
            raise NotFound()
        # Ownership: only a vendor with an active offer on the product may act on the question.
        if not record._vendor_can_answer(vendor):
            raise AccessError(_("You cannot access this question."))
        url = "/my/vendor/questions"
        try:
            with request.env.cr.savepoint():
                record.action_vendor_answer(vendor, request.env.user, post.get("answer"))
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?filter=all&error={quote(str(e.args[0]))}")
        return request.redirect(f"{url}?filter=all&saved=1")

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
    # Vendor profile and payout account change (owner only)
    # -------------------------------------------------------------------------
    def _vendor_profile_values(self, vendor, **extra):
        Change = request.env["leaders.vendor.bank.change"].sudo()
        changes = Change.search([("vendor_id", "=", vendor.id)], order="id desc", limit=50)
        values = {
            "vendor": vendor,
            "payout_account": vendor._leaders_masked_payout_account(),
            "payout_bank_name": vendor.vendor_payout_bank_id.bank_id.name,
            "changes": changes,
            "pending_change": changes.filtered(lambda c: c.state == "pending")[:1],
            "form": {},
            "error": False,
            "requested": request.params.get("requested"),
        }
        values.update(extra)
        return values

    @http.route("/my/vendor/profile", type="http", auth="user", website=True)
    def vendor_profile(self, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can access the vendor profile."))
        return self._vendor_render("leaders_marketplace.portal_vendor_profile", self._vendor_profile_values(vendor))

    @http.route("/my/vendor/profile/bank-change", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def vendor_bank_change_request(self, **post):
        vendor = self._require_vendor(allow_frozen=True)
        if not self._is_vendor_owner():
            raise AccessError(_("Only the vendor owner can change the payout account."))
        form = {key: (post.get(key) or "").strip() for key in ("bank_name", "account_holder", "account_number")}
        Change = request.env["leaders.vendor.bank.change"].sudo()
        error = False
        proof, proof_name = False, False
        if not all(form.values()):
            error = _("Bank name, account holder and account number are required.")
        elif Change.search_count([("vendor_id", "=", vendor.id), ("state", "=", "pending")]):
            error = _("A payout account change is already waiting for review.")
        else:
            upload = request.httprequest.files.get("proof")
            if not upload or not upload.filename:
                error = _("A bank proof (statement or letter from the bank) is required.")
            else:
                proof, error = VendorApplyController()._document_error(upload)
                proof_name = os.path.basename(upload.filename)[:100]
        if not error:
            try:
                Change.create({
                    "vendor_id": vendor.id, "requested_by": request.env.user.id,
                    "bank_name": form["bank_name"][:100], "account_holder": form["account_holder"][:100],
                    "account_number": form["account_number"][:64],
                    "proof": proof, "proof_name": proof_name,
                })
            except (UserError, ValidationError) as exc:
                error = exc.args[0] if exc.args else str(exc)
        if error:
            return self._vendor_render("leaders_marketplace.portal_vendor_profile", self._vendor_profile_values(
                vendor, error=error, form=form))
        return request.redirect("/my/vendor/profile?requested=1")

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

    @http.route("/my/marketplace/returns", type="http", auth="user", website=True)
    def customer_marketplace_returns(self, **kw):
        partner = request.env.user.partner_id.commercial_partner_id
        domain = [("partner_id", "child_of", partner.id)]
        Return = request.env["leaders.vendor.return"].sudo()
        Dispute = request.env["leaders.vendor.dispute"].sudo()
        values = {
            "returns": Return.search(domain, order="id desc"),
            "disputes": Dispute.search(domain, order="id desc"),
            "lm_center": Return._leaders_center_enabled(),
        }
        if values["lm_center"]:
            # D2: filter chips (one filter for both lists), applied on the customer's own records.
            chip = kw.get("filter") if kw.get("filter") in CENTER_FILTERS else "all"
            if chip != "all":
                return_states, dispute_states = CENTER_FILTERS[chip]
                values["returns"] = values["returns"].filtered(lambda r: r.state in return_states)
                values["disputes"] = values["disputes"].filtered(lambda d: d.state in dispute_states)
            values["lm_filter"] = chip
        return request.render("leaders_marketplace.portal_customer_marketplace_returns", values)

    # D5: notification preferences (customer area; stored on the commercial partner).
    _NOTIFY_TOGGLES = (
        ("shipping", "leaders_notify_shipping"), ("returns", "leaders_notify_returns"),
        ("questions", "leaders_notify_questions"), ("promotions", "leaders_notify_promotions"),
    )

    @http.route("/my/marketplace/notifications", type="http", auth="user", website=True, methods=["GET"])
    def customer_notification_preferences(self, **kw):
        partner = request.env.user.partner_id.commercial_partner_id.sudo()
        return request.render("leaders_marketplace.portal_customer_notifications", {
            "lm_prefs": {cat: bool(partner[field]) for cat, field in self._NOTIFY_TOGGLES},
            "lm_saved": bool(kw.get("saved")),
        })

    @http.route("/my/marketplace/notifications", type="http", auth="user", website=True,
                methods=["POST"], csrf=True)
    def customer_notification_preferences_save(self, **post):
        partner = request.env.user.partner_id.commercial_partner_id.sudo()
        # An unchecked checkbox is not posted: absent = off. Only the current user's partner is written.
        partner.write({field: post.get(cat) in ("1", "on", "true", "True")
                       for cat, field in self._NOTIFY_TOGGLES})
        return request.redirect("/my/marketplace/notifications?saved=1")

    def _customer_return(self, return_id):
        partner = request.env.user.partner_id.commercial_partner_id
        record = request.env["leaders.vendor.return"].sudo().search([
            ("id", "=", return_id), ("partner_id", "child_of", partner.id)], limit=1)
        if not record:
            raise AccessError(_("You cannot access this return."))
        return record

    @http.route("/my/marketplace/returns/<int:return_id>", type="http", auth="user", website=True)
    def customer_marketplace_return_detail(self, return_id, **kw):
        if not request.env["leaders.vendor.return"]._leaders_center_enabled():
            raise NotFound()
        record = self._customer_return(return_id)
        return request.render("leaders_marketplace.portal_customer_marketplace_return_detail", {
            "ret": record, "error": kw.get("error"),
            "lm_can_escalate": record.state in ("requested", "rejected"),
        })

    @staticmethod
    def _return_photo_response(record, attachment_id):
        photo = record.photo_ids.filtered(lambda a: a.id == attachment_id)
        if not photo:
            raise NotFound()
        raw = photo.raw or b""
        mimetype = guess_mimetype(raw, default="")
        if not mimetype.startswith("image/") or "svg" in mimetype:
            raise NotFound()
        return request.make_response(raw, headers=[
            ("Content-Type", mimetype),
            ("Content-Length", len(raw)),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", "sandbox"),
            ("Cache-Control", "private, max-age=300"),
        ])

    @http.route("/my/marketplace/returns/<int:return_id>/photo/<int:attachment_id>",
                type="http", auth="user", website=True)
    def customer_marketplace_return_photo(self, return_id, attachment_id, **kw):
        return self._return_photo_response(self._customer_return(return_id), attachment_id)

    @http.route("/my/vendor/returns/<int:return_id>/photo/<int:attachment_id>",
                type="http", auth="user", website=True)
    def vendor_return_photo(self, return_id, attachment_id, **kw):
        vendor = self._require_vendor(allow_frozen=True)
        return self._return_photo_response(self._vendor_return(vendor, return_id), attachment_id)

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
        can_rate, rating = order._customer_rating_info()
        return request.render("leaders_marketplace.portal_customer_marketplace_order_detail", {
            "lm_can_rate": can_rate,
            "lm_rating": rating,
            "rated": kw.get("rated"),
            "order": order,
            "returnable_lines": order._customer_returnable_lines(),
            **self._center_form_values(order),
            "error": kw.get("error"),
            "return_requested": kw.get("return_requested"),
            "lm_cancel": order._customer_cancel_info(),
            "lm_cancel_reasons": request.env["leaders.cancel.reason"].sudo().search([
                ("applies_to", "in", ("customer", "both"))]),
            "lm_cancelled_now": kw.get("cancelled"),
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

    def _center_form_values(self, order):
        """Extra data of the return form (D2); nothing changes while the switch is off."""
        Return = request.env["leaders.vendor.return"].sudo()
        if not Return._leaders_center_enabled():
            return {"lm_center": False}
        returnable = order._customer_returnable_lines()
        today = fields.Date.context_today(Return)
        return {
            "lm_center": True,
            "lm_reasons": request.env["leaders.return.reason"].sudo().search([]),
            "lm_has_fbl": any(item[0].fulfillment_group_id.fulfillment_type == "fbl" for item in returnable),
            "lm_pickup_min": today,
            "lm_pickup_max": today + timedelta(days=Return._leaders_pickup_max_days()),
            "lm_max_photos": MAX_RETURN_PHOTOS,
            "lm_shipping_fee": Return._leaders_shipping_fee(),
        }

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

    @http.route("/my/marketplace/orders/<int:order_id>/cancel", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def customer_cancel_order(self, order_id, **post):
        """Cancel the whole checkout (scope=all) or just this seller's part (scope=vendor)."""
        order = self._customer_vendor_order(order_id)
        if not order._customer_cancel_enabled():
            raise NotFound()
        url = f"/my/marketplace/orders/{order.id}"
        scope = "all" if post.get("scope") == "all" else "vendor"
        try:
            with request.env.cr.savepoint():
                order.action_customer_cancel(
                    post.get("reason_id"), post.get("note"), scope=scope)
        except UserError as error:
            return request.redirect(f"{url}?error={quote(str(error.args[0]))}")
        return request.redirect(f"{url}?cancelled={scope}")

    @http.route("/my/marketplace/orders/<int:order_id>/return", type="http", auth="user",
                website=True, methods=["POST"], csrf=True)
    def customer_request_return(self, order_id, **post):
        order = self._customer_vendor_order(order_id)
        url = f"/my/marketplace/orders/{order.id}"
        Return = request.env["leaders.vendor.return"].sudo()
        lines = []
        for line in order.line_ids:
            qty = self._to_float(post.get(f"qty_{line.id}"), 0.0)
            if qty > 0:
                lines.append((0, 0, {"vendor_line_id": line.id, "quantity": qty}))
        reason = (post.get("reason") or "").strip()
        vals = {"vendor_order_id": order.id, "line_ids": lines}
        try:
            if Return._leaders_center_enabled():
                vals.update(self._center_return_vals(order, post, reason))
            else:
                if not reason:
                    raise UserError(_("Please tell us why you are returning the products."))
                vals["reason"] = reason
            with request.env.cr.savepoint():
                record = Return.create(vals)
                record.message_post(body=_("Return requested by the customer from the portal."))
        except (UserError, ValidationError) as e:
            return request.redirect(f"{url}?error={quote(str(e.args[0]))}")
        return request.redirect(f"{url}?return_requested=1")

    def _center_return_vals(self, order, post, comment):
        """D2 part of the return form: reason code, photos, resolution, pickup date."""
        reason = request.env["leaders.return.reason"].sudo().search([
            ("id", "=", int(post.get("reason_id") or 0))], limit=1)
        if not reason:
            raise UserError(_("Please choose why you are returning the products."))
        uploads = [u for u in request.httprequest.files.getlist("photos") if u and u.filename]
        if len(uploads) > MAX_RETURN_PHOTOS:
            raise UserError(_("You can attach at most %s photos.", MAX_RETURN_PHOTOS))
        photos = []
        for upload in uploads:
            data, error = self._read_vendor_image(upload)
            if error:
                raise UserError(error)
            photos.append((0, 0, {
                "name": os.path.basename(upload.filename)[:100] or "photo",
                "datas": data,
            }))
        resolution = "exchange" if post.get("resolution") == "exchange" else "refund"
        vals = {
            "reason_id": reason.id,
            "reason": "%s: %s" % (reason.name, comment) if comment else reason.name,
            "photo_ids": photos,
            "resolution": resolution,
        }
        pickup = (post.get("pickup_date") or "").strip()
        if pickup:
            fbl = False
            for key, value in post.items():
                if key.startswith("qty_") and key[4:].isdigit() and self._to_float(value) > 0:
                    line = order.line_ids.filtered(lambda l: l.id == int(key[4:]))
                    fbl = fbl or line.fulfillment_group_id.fulfillment_type == "fbl"
            if fbl:
                try:
                    vals["pickup_date"] = fields.Date.from_string(pickup)
                except (TypeError, ValueError):
                    raise UserError(_("Choose a valid pickup date."))
        return vals

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
        if post.get("back") == "detail" and record._leaders_center_enabled():
            url = f"/my/marketplace/returns/{record.id}"
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
