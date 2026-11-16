"""Storefront search and filter log: what customers search and filter by (admin report).

One row per catalogue listing request that carries a search word or a filter, and one line per
chosen filter value, so the report can rank the most used filters and the searches that found nothing.
"""
from datetime import timedelta

from odoo import api, fields, models

# the listing parameters that are filters (paging, sorting and the landing rows are not)
FILTER_KEYS = ("subcategory", "categoryId", "section", "listing", "condition", "brand", "brandId", "powerSource",
               "powerSourceId", "source", "sourceId", "hours", "power", "location", "priceMin", "priceMax",
               "kvaMin", "kvaMax", "engineMin", "engineMax")
KEEP_DAYS = 365


class LeadersSearchLog(models.Model):
    _name = "leaders.search.log"
    _description = "Storefront Search Log"
    _order = "id desc"
    _rec_name = "query"

    query = fields.Char("Search words", index=True, readonly=True)
    kind = fields.Selection([("power", "Power"), ("equipment", "Equipment"), ("part", "Part")], readonly=True)
    lang = fields.Char("Language", readonly=True)
    result_count = fields.Integer("Results", readonly=True, aggregator="avg")
    no_result = fields.Boolean("No results", readonly=True, index=True)
    # filled by POST /products/search-click when the customer opens a product from these results
    clicked = fields.Boolean("Clicked a result", readonly=True, index=True)
    click_rate = fields.Float("Click rate (%)", readonly=True, aggregator="avg",
                              help="100 when the customer opened a result, else 0; averaged it gives the click rate.")
    click_count = fields.Integer("Clicks", readonly=True)
    clicked_product_ids = fields.Many2many("product.template", string="Opened products", readonly=True)
    filter_ids = fields.One2many("leaders.search.log.filter", "log_id", "Filters", readonly=True)

    @api.model
    def leaders_record(self, params, result_count, lang):
        """Log one listing request and return it; never raises (the page must not fail because of the log)."""
        query = (params.get("q") or "").strip()[:100]
        chosen = [(key, value.strip()) for key in FILTER_KEYS
                  for value in (params.get(key) or "").split(",") if value.strip()]
        if not query and not chosen:
            return self.browse()
        try:
            with self.env.cr.savepoint():
                return self.sudo().create({
                    "query": query or False, "kind": params.get("kind") if params.get("kind") in ("power", "equipment", "part") else False,
                    "lang": (lang or "")[:5], "result_count": result_count, "no_result": not result_count,
                    "filter_ids": [(0, 0, {"key": key, "value": value[:64]}) for key, value in chosen[:30]],
                })
        except Exception:  # noqa: BLE001 - logging is best effort
            return self.browse()

    @api.model
    def leaders_click(self, search_id, product_id):
        """Mark a search as clicked; unknown or older than a day: ignored (the call never fails the page)."""
        log = self.sudo().search([("id", "=", search_id),
                                  ("create_date", ">=", fields.Datetime.now() - timedelta(days=1))], limit=1)
        product = self.env["product.template"].sudo().browse(product_id).exists()
        if log and product:
            log.write({"clicked": True, "click_rate": 100.0, "click_count": log.click_count + 1,
                       "clicked_product_ids": [(4, product.id)]})
        return bool(log)

    @api.model
    def cron_purge(self):
        self.sudo().search([("create_date", "<", fields.Datetime.now() - timedelta(days=KEEP_DAYS))]).unlink()


class LeadersSearchLogFilter(models.Model):
    _name = "leaders.search.log.filter"
    _description = "Storefront Search Log Filter"
    _order = "id desc"

    log_id = fields.Many2one("leaders.search.log", required=True, ondelete="cascade", index=True)
    key = fields.Char("Filter", required=True, index=True)
    value = fields.Char("Value", required=True, index=True)
    no_result = fields.Boolean(related="log_id.no_result", store=True)
    kind = fields.Selection(related="log_id.kind", store=True)

    @api.depends("key", "value")
    def _compute_display_name(self):
        for line in self:
            line.display_name = "%s: %s" % (line.key, line.value)
