"""Services a vendor offers: his own delivery, installation, a service contract, any service of his trade.

The vendor writes the service like a platform service page (name, card summary, what it includes, checklist, picture).
It goes to the back office for approval; once approved it becomes a real service page of the storefront: a card in
``/services``, a tile in ``/categories``, ``/services/<slug>`` and a service request like any other, so the app needs no
change. The request is the vendor's: he answers it from the vendor portal. A change the vendor makes later waits for
approval too, and the live page keeps its text until then. The vendor may also offer the service with his machines
(it is then in the machine's ``services`` list)."""
import base64
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .catalog_spec import slug_ascii


STATES = [("draft", "Draft"), ("submitted", "Waiting for approval"), ("approved", "Approved"), ("rejected", "Rejected")]
SERVICE_TEXTS = (  # (field here, field on the service page)
    ("name", "name"), ("summary", "leaders_service_summary"), ("description", "leaders_service_lead"),
    ("request_title", "leaders_service_request_title"), ("deliverable", "leaders_service_deliverable"),
    ("includes", "leaders_service_includes"), ("excludes", "leaders_service_excludes"),
    ("checklist_title", "leaders_service_checklist_title"))


class LeadersVendorService(models.Model):
    _name = "leaders.vendor.service"
    _description = "Vendor's Own Service"
    _inherit = ["mail.thread", "leaders.backoffice.mixin"]
    _order = "vendor_id, sequence, id"

    sequence = fields.Integer(default=10)
    name = fields.Char("Service", required=True, translate=True, tracking=True)
    summary = fields.Char("Card summary", translate=True, help="One sentence for the service card.")
    description = fields.Text("Description", translate=True,
                              help="The paragraph at the top of the service page.")
    request_title = fields.Char("Request form title", translate=True)
    deliverable = fields.Char("What the customer gets", translate=True, help="For example: a written report.")
    includes = fields.Text("What it includes", translate=True, help="One item per line.")
    excludes = fields.Text("What it excludes", translate=True, help="One item per line.")
    checklist_title = fields.Char("Checklist title", translate=True)
    checklist = fields.Text("Checklist", translate=True, help="One item per line.")
    coverage = fields.Integer("Governorates covered", help="How many governorates the service reaches; 0 leaves it off.")
    warranty_months = fields.Integer("Warranty after the service (months)", help="0: no warranty is shown.")
    icon = fields.Binary("Picture", attachment=True, help="Square picture or SVG shown on the service card.")
    state = fields.Selection(STATES, default="draft", required=True, tracking=True, index=True)
    reject_reason = fields.Char("Reason of the refusal", copy=False, tracking=True)
    service_tmpl_id = fields.Many2one("product.template", "Service page", readonly=True, copy=False, ondelete="set null",
                                      help="The page of the storefront: made when the service is approved.")
    submitted_on = fields.Datetime(readonly=True, copy=False)
    decided_on = fields.Datetime(readonly=True, copy=False)
    live = fields.Boolean(compute="_compute_live", help="The service page is on the storefront now.")
    vendor_id = fields.Many2one("res.partner", "Vendor", required=True, index=True, ondelete="cascade",
                                domain=[("is_marketplace_vendor", "=", True)])
    product_tmpl_ids = fields.Many2many(
        "product.template", "leaders_product_own_service_rel", "service_id", "product_tmpl_id",
        string="Offered with", help="The vendor's machines this service is offered with.")
    active = fields.Boolean(default=True)
    slug = fields.Char("Slug", readonly=True, copy=False, index=True,
                       help="Its name on the storefront, like a service's slug: ordered with the machine through "
                            "the same services list. Never the same as a product, a category or another service.")

    _sql_constraints = [("slug_unique", "unique(slug)", "This service slug is already used.")]

    @api.depends("service_tmpl_id.is_published")
    def _compute_live(self):
        for rec in self:
            rec.live = bool(rec.service_tmpl_id.sudo().is_published)

    def _leaders_free_slug(self, base):
        """A slug no product, category or service already uses."""
        taken = lambda slug: (  # noqa: E731
            self.env["product.template"].sudo().with_context(active_test=False).search_count([("leaders_slug", "=", slug)])
            or self.env["product.public.category"].sudo().search_count([("leaders_slug", "=", slug)])
            or self.sudo().with_context(active_test=False).search_count([("slug", "=", slug), ("id", "!=", self.id)]))
        slug, n = base, 1
        while taken(slug):
            n += 1
            slug = "%s-%s" % (base, n)
        return slug

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records.filtered(lambda r: not r.slug):
            name = rec.with_context(lang="en_US").name or rec.name
            base = "v%s-%s" % (rec.vendor_id.id, (slug_ascii(name)[:50].strip("-") or "service"))
            rec.sudo().slug = rec._leaders_free_slug(base)
        return records

    @api.constrains("name", "vendor_id")
    def _check_name_is_unique_per_vendor(self):
        for rec in self:
            if self.sudo().with_context(active_test=False).search_count(
                    [("vendor_id", "=", rec.vendor_id.id), ("name", "=ilike", rec.name), ("id", "!=", rec.id)]):
                raise ValidationError(_("You already have a service with this name."))

    @api.constrains("vendor_id", "product_tmpl_ids")
    def _check_products_are_the_vendors(self):
        for rec in self:
            foreign = rec.product_tmpl_ids.sudo().filtered(
                lambda t: t.marketplace_owner_vendor_id.commercial_partner_id != rec.vendor_id.commercial_partner_id)
            if foreign:
                raise ValidationError(_("A service can only be offered with the vendor's own products: %s.",
                                        ", ".join(foreign.mapped("name"))))

    def _leaders_card(self):
        """The entry of a product's ``services`` list: the same two keys as a platform service."""
        self.ensure_one()
        return {"slug": self.slug, "name": self.name}

    # ---- the vendor sends it, the back office decides ---------------------------------------------------------
    def _leaders_missing(self):
        self.ensure_one()
        return [label for value, label in ((self.name, _("Service")), (self.summary, _("Card summary")))
                if not (value or "").strip()]

    def _leaders_reviewers(self):
        groups = (self.env.ref("leaders_marketplace.group_marketplace_reviewer")
                  | self.env.ref("leaders_marketplace.group_marketplace_manager"))
        return self.env["res.users"].sudo().search([("groups_id", "in", groups.ids), ("share", "=", False)])

    def action_submit(self):
        """The vendor sends the service (or a change of it) for approval; the back office is told."""
        for rec in self:
            missing = rec._leaders_missing()
            if missing:
                raise UserError(_("Fill in: %s.", ", ".join(missing)))
            rec.sudo().write({"state": "submitted", "submitted_on": fields.Datetime.now(), "reject_reason": False})
            rec.sudo()._leaders_tell_staff()
        return True

    def _leaders_tell_staff(self):
        """The reviewers get a message (their bell and email) and a push on their phones."""
        self.ensure_one()
        self.message_post(
            body=_("%(vendor)s sent the service %(name)s for approval.", vendor=self.vendor_id.display_name, name=self.name),
            partner_ids=self._leaders_reviewers().partner_id.ids, message_type="comment", subtype_xmlid="mail.mt_comment")
        self.env["leaders.push.helpers"]._lp_staff(self, "staff_vendor_service", lambda r: (
            r.env._("A vendor service waits for approval"),
            r.env._("%(vendor)s: %(name)s", vendor=self.vendor_id.display_name, name=self.name)),
            {"model": self._name, "id": str(self.id)})

    def action_approve(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self:
            if rec.state not in ("draft", "submitted"):
                raise UserError(_("Only a service waiting for approval can be approved."))
            rec._leaders_publish()
            rec.write({"state": "approved", "decided_on": fields.Datetime.now(), "reject_reason": False})
            rec.sudo().message_post(body=_("The service was approved and is on the storefront."))
            self.env["leaders.notification"].notify_vendor("vendor_service_approved", rec, rec.vendor_id)
        return True

    def action_reject(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self:
            if rec.state not in ("draft", "submitted"):
                raise UserError(_("Only a service waiting for approval can be refused."))
            if not (rec.reject_reason or "").strip():
                raise UserError(_("Write the reason of the refusal first."))
            rec.write({"state": "rejected", "decided_on": fields.Datetime.now()})
            rec.sudo().message_post(body=_("The service was refused: %s", rec.reject_reason))
            self.env["leaders.notification"].notify_vendor("vendor_service_rejected", rec, rec.vendor_id)
        return True

    # ---- the service page ------------------------------------------------------------------------------------
    def _leaders_publish(self):
        """Make (or update) the storefront service page and its tile from what the vendor wrote, in every language."""
        Template, Categ = self.env["product.template"].sudo(), self.env["product.public.category"].sudo()
        root = self.env.ref("leaders_marketplace.leaders_categ_services")
        langs = self.env["res.lang"].sudo().search([("active", "=", True)]).mapped("code")
        for rec in self.sudo():
            common = {"type": "service", "sale_ok": False, "purchase_ok": False, "leaders_service_coverage": rec.coverage,
                      "leaders_service_warranty_months": rec.warranty_months, "is_published": True,
                      "leaders_service_vendor_id": rec.vendor_id.commercial_partner_id.id,
                      "leaders_service_icon": rec.icon or False}
            tmpl = rec.service_tmpl_id
            if tmpl:
                tmpl.write(common)
            else:
                categ = Categ.create({"name": rec.name, "parent_id": root.id, "leaders_slug": rec.slug,
                                      "leaders_can_buy": False, "leaders_can_rent": False, "leaders_is_service": True,
                                      "leaders_accepts_used": False, "sequence": 200 + rec.id})
                tmpl = Template.create(dict(common, name=rec.name, leaders_slug=rec.slug, sequence=1000 + rec.id,
                                            public_categ_ids=[(6, 0, categ.ids)]))
                rec.service_tmpl_id = tmpl
            tile = tmpl._leaders_service_tile()
            tmpl.leaders_service_checklist_ids.unlink()
            lines = {code: self._leaders_lines(rec.with_context(lang=code).checklist) for code in langs}
            base = lines.get("en_US") or next((v for v in lines.values() if v), [])
            items = [self.env["leaders.service.checklist.item"].sudo().create({"product_tmpl_id": tmpl.id, "sequence": 10 * (i + 1),
                                                                         "name": line}) for i, line in enumerate(base)]
            for code in langs:
                src = rec.with_context(lang=code)
                texts = {target: (getattr(src, field) or False) for field, target in SERVICE_TEXTS}
                texts["leaders_service_lead"] = src.description or src.summary or False
                texts["leaders_service_request_title"] = src.request_title or src.name
                texts["leaders_service_panel_title"] = src.name
                tmpl.with_context(lang=code).write(texts)
                if tile:
                    tile.with_context(lang=code).write({"name": src.name})
                for item, line in zip(items, lines[code]):
                    item.with_context(lang=code).name = line

    @staticmethod
    def _leaders_lines(text):
        return [line.strip(" -•\t") for line in (text or "").splitlines() if line.strip(" -•\t")]

    def _leaders_set_page_published(self, published):
        for rec in self.sudo().filtered("service_tmpl_id"):
            rec.service_tmpl_id.is_published = published

    def write(self, vals):
        res = super().write(vals)
        if "active" in vals:  # archived: off the storefront (the requests already made keep their page)
            self.filtered(lambda r: r.state == "approved")._leaders_set_page_published(bool(vals["active"]))
            if not vals["active"]:
                self.sudo().filtered(lambda r: r.state != "approved")._leaders_set_page_published(False)
        return res

    def unlink(self):
        self._leaders_set_page_published(False)
        return super().unlink()


class ProductTemplateOwnServices(models.Model):
    _inherit = "product.template"

    leaders_service_vendor_id = fields.Many2one(
        "res.partner", "Service of the vendor", copy=False, index=True,
        help="The vendor whose service this page is: the requests of the page are his.")
    leaders_own_service_ids = fields.Many2many(
        "leaders.vendor.service", "leaders_product_own_service_rel", "product_tmpl_id", "service_id",
        string="Vendor's own services", help="Services the vendor offers himself with this machine; the price is "
        "agreed with the buyer.")

    def _leaders_vendor_own_services(self):
        self.ensure_one()
        return self.sudo().leaders_own_service_ids.filtered(lambda s: s.active and s.state == "approved")
