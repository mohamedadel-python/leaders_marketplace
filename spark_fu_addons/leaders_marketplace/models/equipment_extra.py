"""Equipment details of the project guide (feature 4): warranty terms, used-equipment proof photos,
possible duplicate listings and a periodic check of the operation video links."""
import logging
from datetime import timedelta

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .catalog_spec import USED_LIKE

_logger = logging.getLogger(__name__)
WARRANTY_PROVIDERS = [("vendor", "Vendor"), ("manufacturer", "Manufacturer"), ("platform", "Platform")]


class ProductTemplateEquipment(models.Model):
    _inherit = "product.template"

    leaders_warranty_provider = fields.Selection(WARRANTY_PROVIDERS, string="Warranty provided by")
    leaders_warranty_scope = fields.Text("Warranty scope", translate=True,
                                         help="What the warranty covers and what it excludes; shown on the product card.")
    leaders_meter_photo = fields.Image("Hour meter photo", max_width=1920, max_height=1920,
                                       help="Required for used equipment before it is published.")
    leaders_plate_photo = fields.Image("Data plate photo", max_width=1920, max_height=1920,
                                       help="Required for used equipment before it is published.")
    leaders_duplicate_ids = fields.Many2many(
        "product.template", compute="_compute_leaders_duplicate_ids", string="Possible duplicates",
        help="Other listings with the same model, capacity, year and running hours: maybe the same machine "
             "listed by another vendor. Check them before publishing.")
    leaders_video_broken = fields.Boolean("Video link not working", copy=False, readonly=True,
                                          help="Set by the weekly check of the operation video links.")
    leaders_video_checked_at = fields.Datetime("Video link checked", copy=False, readonly=True)

    @api.depends("leaders_model_id", "leaders_capacity", "leaders_year", "leaders_hours", "leaders_condition")
    def _compute_leaders_duplicate_ids(self):
        for tmpl in self:
            if tmpl.leaders_field_set != "equipment" or not tmpl.leaders_model_id or tmpl.leaders_condition not in USED_LIKE:
                tmpl.leaders_duplicate_ids = False
                continue
            tmpl.leaders_duplicate_ids = self.sudo().search([
                ("id", "!=", tmpl._origin.id or 0), ("leaders_model_id", "=", tmpl.leaders_model_id.id),
                ("leaders_capacity", "=", tmpl.leaders_capacity), ("leaders_year", "=", tmpl.leaders_year),
                ("leaders_hours", "=", tmpl.leaders_hours), ("leaders_condition", "in", USED_LIKE),
                ("marketplace_state", "!=", "rejected")])

    def _leaders_listing_problems(self):
        """What equipment needs before the reviewer publishes it (the guide's mandatory equipment data)."""
        self.ensure_one()
        problems = []
        if self.leaders_field_set != "equipment":
            return problems
        if not self.leaders_warranty_months or not self.leaders_warranty_provider:
            problems.append(_("Warranty: its length and who provides it are required for equipment."))
        if self.leaders_condition in USED_LIKE and not (self.leaders_meter_photo and self.leaders_plate_photo):
            problems.append(_("Used equipment needs a photo of the hour meter and of the data plate."))
        return problems

    def action_marketplace_approve(self):
        missing = ["%s: %s" % (t.display_name, p) for t in self for p in t._leaders_listing_problems()]
        if missing:
            raise UserError(_("These products cannot be published yet:\n- %s", "\n- ".join(missing)))
        return super().action_marketplace_approve()

    def action_marketplace_submit(self):
        res = super().action_marketplace_submit()
        for tmpl in self.filtered("leaders_duplicate_ids"):  # tell the reviewers on the record itself
            tmpl.sudo().message_post(body=_(
                "Possible duplicate: the same model, capacity, year and running hours as %s.",
                ", ".join(tmpl.leaders_duplicate_ids.mapped("display_name"))))
        return res

    @api.model
    def cron_check_video_links(self, limit=200):
        """Weekly: flag operation videos whose link stopped working (the source can delete them)."""
        stale = fields.Datetime.now() - timedelta(days=7)
        templates = self.sudo().search([("leaders_video_url", "!=", False), ("is_published", "=", True), "|",
                                        ("leaders_video_checked_at", "=", False), ("leaders_video_checked_at", "<", stale)],
                                       limit=limit, order="leaders_video_checked_at asc nulls first, id")
        for tmpl in templates:
            ok = True
            try:
                r = requests.get(tmpl.leaders_video_url, timeout=15, allow_redirects=True, stream=True,
                                 headers={"User-Agent": "Mozilla/5.0 (link check)"})
                ok = r.status_code < 400
                r.close()
            except requests.RequestException:
                ok = False
            if not ok and not tmpl.leaders_video_broken:
                tmpl.message_post(body=_("The operation video link does not work any more: %s", tmpl.leaders_video_url))
            tmpl.write({"leaders_video_broken": not ok, "leaders_video_checked_at": fields.Datetime.now()})
        return len(templates)
