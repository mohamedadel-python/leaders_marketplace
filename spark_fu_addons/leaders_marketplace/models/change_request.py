"""A vendor edit of a live listing waits for the platform: the published version stays on the website
until a reviewer approves the change (project guide, feature 4: every edit needs the admin's approval)."""
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError

LABELS = {"price": "Price", "seller_sku": "Seller SKU", "fulfillment_type": "Fulfillment", "name": "Name",
          "description_sale": "Description", "public_categ_ids": "Category", "image_1920": "Image",
          "currency_id": "Currency", "market_ids": "Markets"}


class LeadersChangeRequest(models.Model):
    _name = "leaders.change.request"
    _description = "Listing Change to Review"
    _inherit = ["mail.thread", "leaders.backoffice.mixin"]
    _order = "id desc"

    offer_id = fields.Many2one("leaders.marketplace.offer", required=True, ondelete="cascade", index=True, readonly=True)
    product_tmpl_id = fields.Many2one(related="offer_id.product_tmpl_id", store=True, string="Product")
    vendor_id = fields.Many2one(related="offer_id.vendor_id", store=True, string="Vendor")
    offer_values = fields.Text(readonly=True, help="JSON: the new values of the offer.")
    template_values = fields.Text(readonly=True, help="JSON: the new values of the product content.")
    image_1920 = fields.Image("New image", readonly=True, max_width=1920, max_height=1920)
    summary = fields.Text("Changes", readonly=True)
    state = fields.Selection([("pending", "To review"), ("approved", "Approved"), ("rejected", "Rejected")],
                             default="pending", required=True, index=True, tracking=True)
    reject_reason = fields.Text("Rejection reason", tracking=True)
    reviewer_id = fields.Many2one("res.users", readonly=True)

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s (%s)" % (rec.product_tmpl_id.display_name, rec.vendor_id.display_name)

    @api.model
    def leaders_propose(self, offer, offer_vals, template_vals, image=False):
        """Queue the vendor's edit of a live offer; returns the request, or nothing when nothing changed."""
        offer = offer.sudo()
        offer_vals = {k: v for k, v in offer_vals.items() if self._differs(offer, k, v)}
        tmpl = offer.product_tmpl_id
        template_vals = {k: v for k, v in (template_vals or {}).items() if self._differs(tmpl, k, v)}
        if not offer_vals and not template_vals and not image:
            return self.browse()
        lines = []
        for record, vals in ((offer, offer_vals), (tmpl, template_vals)):
            for key, value in vals.items():
                lines.append("%s: %s → %s" % (LABELS.get(key) or record._fields[key].string, self._show(record, key, record[key]), self._show(record, key, value)))
        if image:
            lines.append(_("Image: a new image"))
        # one open request per offer: a new edit replaces the pending one
        self.sudo().search([("offer_id", "=", offer.id), ("state", "=", "pending")]).unlink()
        return self.sudo().create({
            "offer_id": offer.id, "offer_values": json.dumps(offer_vals), "template_values": json.dumps(template_vals),
            "image_1920": image or False, "summary": "\n".join(lines)})

    @staticmethod
    def _differs(record, key, value):
        field = record._fields[key]
        current = record[key]
        if field.type == "many2one":
            return (current.id or False) != (value or False)
        if field.type == "many2many":
            ids = value[0][2] if value and isinstance(value[0], (list, tuple)) else value
            return set(current.ids) != set(ids or [])
        if field.type == "float" or field.type == "monetary":
            return round(current or 0.0, 2) != round(value or 0.0, 2)
        return (current or False) != (value or False)

    @staticmethod
    def _show(record, key, value):
        field = record._fields[key]
        if field.type == "many2one":
            return record.env[field.comodel_name].browse(value.id if hasattr(value, "id") else value).display_name or "-"
        if field.type == "many2many":
            ids = value.ids if hasattr(value, "ids") else (value[0][2] if value and isinstance(value[0], (list, tuple)) else value)
            return ", ".join(record.env[field.comodel_name].browse(ids or []).mapped("display_name")) or "-"
        if field.type == "selection":
            return dict(field._description_selection(record.env)).get(value, value or "-")
        if field.type == "binary":  # a photo: never print its data
            return _("a new image") if value else "-"
        return "%s" % (value if value not in (False, None, "") else "-")

    def action_approve(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self.filtered(lambda r: r.state == "pending"):
            offer = rec.offer_id.sudo()
            offer.write(json.loads(rec.offer_values or "{}"))
            tmpl_vals = json.loads(rec.template_values or "{}")
            if rec.image_1920:
                tmpl_vals["image_1920"] = rec.image_1920
            if tmpl_vals:
                offer.product_tmpl_id.write(tmpl_vals)
            rec.write({"state": "approved", "reviewer_id": self.env.user.id})
            offer.message_post(body=_("Approved change:\n%s", rec.summary or ""))

    def action_reject(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self:
            if not rec.reject_reason:
                raise UserError(_("Please write why the change is rejected; the vendor sees it."))
            rec.write({"state": "rejected", "reviewer_id": self.env.user.id})
            rec.offer_id.message_post(body=_("Rejected change: %s", rec.reject_reason))
