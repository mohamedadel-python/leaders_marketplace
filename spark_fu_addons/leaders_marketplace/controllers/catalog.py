from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request

STORE_PAGE_SIZE = 12


class MarketplaceStorefront(http.Controller):

    @http.route(["/seller/<string:slug>", "/seller/<string:slug>/page/<int:page>"],
                type="http", auth="public", website=True, sitemap=False)
    def seller_store(self, slug, page=1, **kw):
        Partner = request.env["res.partner"]
        if not Partner._marketplace_catalog_enabled():
            raise NotFound()
        vendor = Partner.sudo().search([
            ("vendor_store_slug", "=", slug), ("is_marketplace_vendor", "=", True),
            ("vendor_state", "=", "approved")], limit=1)
        if not vendor:
            raise NotFound()
        domain = [
            ("vendor_id", "=", vendor.id), ("state", "=", "active"), ("active", "=", True),
            ("product_marketplace_state", "=", "published")]
        Offer = request.env["leaders.marketplace.offer"].sudo()
        total = Offer.search_count(domain)
        pager = request.website.pager(
            url="/seller/%s" % slug, total=total, page=page, step=STORE_PAGE_SIZE, scope=5)
        offers = Offer.search(domain, limit=STORE_PAGE_SIZE, offset=pager["offset"], order="id desc")
        return request.render("leaders_marketplace.seller_store_page", {
            "vendor": vendor, "offers": offers, "pager": pager,
        })
