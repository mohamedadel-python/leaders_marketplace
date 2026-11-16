"""Storefront services (/services): a service is an admin-owned product of type service.

A product is a service when its category sits under the services group; it is
entered on the normal product form, whose "Service page" tab holds the page
text, and published with the normal publish button. A sub-category of the
services group is a landing tile of /categories and opens the one service it
holds, so that service carries the sub-category's slug. The checklist is its
own ordered, translated lines.
"""
import base64
import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .catalog_spec import slugify

_logger = logging.getLogger(__name__)
SERVICE_ICON_PATHS = {  # 24x24 outline glyphs, the style of the category icons
    "leaders_service_load_bank": '<rect x="3" y="6" width="18" height="12" rx="2"/>'
                                 '<path d="M6 12h2l1.5-3 2 6 2-6 2 6 1.5-3h1"/>',
    "leaders_service_temporary_power": '<path d="M9 3v4M15 3v4"/><path d="M7 7h10v3a5 5 0 0 1-10 0z"/>'
                                       '<path d="M12 15v3a3 3 0 0 0 3 3h2"/>',
}
EQUIPMENT_TYPES = ("generator", "loadBank", "lightingTower", "loader", "forklift", "weldingMachine", "solar", "other")


SERVICE_SUFFIX = "-service"  # a services tile whose slug is taken by a category of another section

class ProductPublicCategoryService(models.Model):
    _inherit = "product.public.category"

    def _leaders_is_services(self):
        """True when the category sits under the services group (the group root included)."""
        self.ensure_one()
        return self._leaders_top().leaders_group == "services"

    def write(self, vals):
        if "leaders_slug" not in vals:
            return super().write(vals)
        tiles = self.filtered(lambda c: c.parent_id and c._leaders_is_services())
        services = self.env["product.template"].sudo().with_context(active_test=False).search(
            [("leaders_service_page", "=", True), ("public_categ_ids", "in", tiles.ids)])
        # only a service that shares its tile's slug follows it ("<slug>-service" tiles keep the service's slug)
        followers = services.filtered(lambda t: t.leaders_slug == t._leaders_service_tile().leaders_slug)
        res = super().write(vals)
        for tmpl in followers:  # the tile opens /services/<slug>: the service follows its tile
            tmpl.leaders_slug = tmpl._leaders_service_tile().leaders_slug
        return res


class ProductTemplateServicePage(models.Model):
    _inherit = "product.template"

    leaders_service_page = fields.Boolean(
        "Service", compute="_compute_leaders_service_page", store=True, index=True,
        help="Its category is under the services group: shown on the storefront services pages and "
             "requested through a service request, not the cart.")
    leaders_service_summary = fields.Char("Card summary", translate=True, help="One sentence for the service card.")
    leaders_service_lead = fields.Text(
        "Lead", translate=True, help="The hero paragraph, also the page's meta description.")
    leaders_service_panel_title = fields.Char("Panel title", translate=True)
    leaders_service_request_title = fields.Char("Request form title", translate=True)
    leaders_service_checklist_title = fields.Char("Checklist title", translate=True)
    leaders_service_checklist_lead = fields.Char("Checklist lead", translate=True)
    leaders_service_deliverable = fields.Char("Deliverable", translate=True, help="Fact in the request panel.")
    leaders_service_deliverable_short = fields.Char("Deliverable (short)", translate=True, help="Bullet in the hero.")
    leaders_service_icon = fields.Binary(
        "Service icon", attachment=True, help="Square, single-colour SVG (or PNG) shown at 36 px.")
    leaders_service_mandatory = fields.Boolean(
        "Required on purchase", help="Shows the 'required on purchase' badge and rule on the services pages.")
    leaders_service_checklist_ids = fields.One2many(
        "leaders.service.checklist.item", "product_tmpl_id", string="Checklist", copy=True)
    leaders_service_responsible_id = fields.Many2one(
        "res.users", "Service responsible", help="Gets a to-do activity when a request for this service arrives.")
    leaders_service_coverage = fields.Integer(
        "Governorates covered", help="How many governorates this service reaches; 0 leaves the number off the pages.")
    # the scope of the service, shown in the request screen so the customer and the platform agree on it
    leaders_service_includes = fields.Text(
        "What it includes", translate=True, help="One item per line; shown in the request screen.")
    leaders_service_excludes = fields.Text(
        "What it excludes", translate=True, help="One item per line; shown in the request screen.")
    leaders_service_warranty_months = fields.Integer(
        "Warranty after the service (months)", help="0: no warranty is shown.")

    @api.depends("public_categ_ids.leaders_group", "public_categ_ids.parent_id.leaders_group",
                 "public_categ_ids.parent_id.parent_id.leaders_group")
    def _compute_leaders_service_page(self):
        for tmpl in self:
            tmpl.leaders_service_page = any(c._leaders_is_services() for c in tmpl.public_categ_ids)

    def _leaders_service_tile(self):
        """The sub-category of the services group this service sits in: its landing tile (may be empty)."""
        self.ensure_one()
        return self.public_categ_ids.filtered(lambda c: c.parent_id and c._leaders_is_services())[:1]

    @api.onchange("public_categ_ids")
    def _onchange_leaders_service_categ(self):
        if not self.leaders_service_page:
            return
        self.type = "service"
        self.sale_ok = self.purchase_ok = False
        tile = self._leaders_service_tile()
        other = tile and self.with_context(active_test=False).search(
            [("leaders_service_page", "=", True), ("public_categ_ids", "in", tile.ids),
             ("id", "!=", self._origin.id or 0)], limit=1)
        if other:  # warned now rather than by the slug's unique index on save
            return {"warning": {"title": _("One service per category"), "message": _(
                "The category %(categ)s already holds the service %(service)s; a services category "
                "holds one service.", categ=tile.display_name, service=other.display_name)}}
        if tile.leaders_slug:
            self.leaders_slug = tile.leaders_slug
        elif not self.leaders_slug and self.name:
            self.leaders_slug = slugify(self.with_context(lang="en_US").name or self.name)

    @api.model
    def load_service_icons(self):
        """Icons of the shipped services that have no landing tile, only where the admin has not set one."""
        for xmlid, paths in SERVICE_ICON_PATHS.items():
            tmpl = self.env.ref("leaders_marketplace." + xmlid, raise_if_not_found=False)
            if tmpl and not tmpl.leaders_service_icon:
                svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="96" height="96" fill="none" '
                       'stroke="#1f2937" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">%s</svg>' % paths)
                tmpl.leaders_service_icon = base64.b64encode(svg.encode())

    @api.constrains("leaders_service_coverage", "leaders_service_warranty_months")
    def _check_leaders_service_coverage(self):
        if any(tmpl.leaders_service_coverage < 0 for tmpl in self):
            raise ValidationError(_("Governorates covered cannot be negative."))
        if any(tmpl.leaders_service_warranty_months < 0 for tmpl in self):
            raise ValidationError(_("The warranty after the service cannot be negative."))

    @api.constrains("leaders_service_page", "leaders_slug", "type", "public_categ_ids")
    def _check_leaders_service_page(self):
        for tmpl in self.filtered("leaders_service_page"):
            if len(tmpl.public_categ_ids) > 1:
                raise ValidationError(_("A service belongs to one services category only."))
            tile = tmpl._leaders_service_tile()
            other = tile and self.with_context(active_test=False).search(
                [("leaders_service_page", "=", True), ("public_categ_ids", "in", tile.ids), ("id", "!=", tmpl.id)],
                limit=1)
            if other:
                raise ValidationError(_(
                    "The category %(categ)s already holds the service %(service)s; a services category "
                    "holds one service.", categ=tile.display_name, service=other.display_name))
            if not tmpl.leaders_slug:
                raise ValidationError(_("A service needs a slug: it is the URL of its page (/services/slug)."))
            if tmpl.type != "service":
                raise ValidationError(_("A service must be a product of type Service."))
            if tile and tile.leaders_slug not in (tmpl.leaders_slug, tmpl.leaders_slug + SERVICE_SUFFIX):
                raise ValidationError(_(
                    "The service %(service)s must have the slug of its category (%(slug)s): "
                    "the category tile opens /services/%(slug)s.",
                    service=tmpl.display_name, slug=tile.leaders_slug))

    def _leaders_ensure_service_tile(self):
        """A published service always has its own sub-category under Services, so /categories lists every service of
        /services (the two lists cannot drift apart). The category takes the service's names, picture and slug."""
        Category = self.env["product.public.category"].sudo()
        langs = self.env["res.lang"].sudo().search([("active", "=", True)]).mapped("code")
        for tmpl in self.sudo().filtered(lambda t: t.leaders_service_page and t.is_published and t.leaders_slug):
            if tmpl._leaders_service_tile():
                continue
            root = tmpl.public_categ_ids.filtered(lambda c: not c.parent_id and c._leaders_is_services())[:1] \
                or self.env.ref("leaders_marketplace.leaders_categ_services", raise_if_not_found=False)
            if not root:
                continue
            # the service's own slug, or "<slug>-service" when a category of another section already uses it
            # (load-bank is also a power sub-category); the tile still opens /services/<service slug>
            slug = tmpl.leaders_slug
            tile = Category.search([("parent_id", "=", root.id), ("leaders_slug", "in", (slug, slug + SERVICE_SUFFIX))],
                                   limit=1)
            if not tile:
                if Category.with_context(active_test=False).search_count([("leaders_slug", "=", slug)]):
                    slug += SERVICE_SUFFIX
                tile = Category.create({"name": tmpl.with_context(lang="en_US").name, "parent_id": root.id,
                                        "leaders_slug": slug, "sequence": tmpl.sequence or 10,
                                        "image_1920": tmpl.image_1920 or False})
            # a service belongs to one services category: it moves from the root to its tile
            tmpl.with_context(leaders_service_tile_done=True).public_categ_ids = [(6, 0, tile.ids)]
            for lang in langs:  # after the move: the tile now holds the service, so its slug stays
                if lang != "en_US" and tmpl.with_context(lang=lang).name:
                    tile.with_context(lang=lang).name = tmpl.with_context(lang=lang).name

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._leaders_ensure_service_tile()
        return records

    def write(self, vals):
        if "leaders_slug" in vals:
            Request = self.env["leaders.customer.request"].sudo()
            for tmpl in self.filtered(lambda t: t.leaders_service_page and t.leaders_slug != vals["leaders_slug"]):
                if Request.search_count([("service", "=", tmpl.leaders_slug)], limit=1):
                    raise UserError(_("The slug of %s is used by service requests and links; it cannot change.",
                                      tmpl.display_name))
        res = super().write(vals)
        if not self.env.context.get("leaders_service_tile_done") and \
                {"is_published", "public_categ_ids", "leaders_slug", "website_published"} & set(vals):
            self._leaders_ensure_service_tile()
        return res


class LeadersServiceChecklistItem(models.Model):
    _name = "leaders.service.checklist.item"
    _description = "Service Page Checklist Item"
    _order = "sequence, id"

    product_tmpl_id = fields.Many2one("product.template", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char("Item", required=True, translate=True)


class LeadersCustomerRequestService(models.Model):
    _name = "leaders.customer.request"
    _inherit = ["leaders.customer.request", "mail.activity.mixin"]

    service_id = fields.Many2one(
        "product.template", "Service page", ondelete="set null", index=True,
        domain=[("leaders_service_page", "=", True)])

    def service_name(self):
        """The service's name in the context language; requests made before service pages find it by slug."""
        self.ensure_one()
        service = self.service_id
        if not service and self.service:
            service = self.env["product.template"].sudo().with_context(active_test=False).search(
                [("leaders_service_page", "=", True), ("leaders_slug", "=", self.service)], limit=1)
        return service.with_context(lang=self.env.lang).name if service else None

    def notify_service_responsible(self):
        """A to-do for the service's responsible user, so a request is answered within the promised time."""
        for rec in self.filtered(lambda r: r.service_id.leaders_service_responsible_id):
            user = rec.service_id.leaders_service_responsible_id
            summary = self.with_context(lang=user.lang or "en_US").env._("New service request %s", rec.name)
            rec.sudo().activity_schedule(
                "mail.mail_activity_data_todo", date_deadline=fields.Date.context_today(rec),
                summary=summary, user_id=user.id)

    def send_confirmation(self, lang):
        """Email the customer the reference of a service request, in ``lang``, when the form has an email."""
        template = self.env.ref("leaders_marketplace.mail_template_service_request_received", raise_if_not_found=False)
        for rec in self.filtered(lambda r: r.kind == "service"):
            email = json.loads(rec.service_details or "{}").get("email")
            if not template or not email:
                continue
            try:
                template.sudo().with_context(lang=lang).send_mail(
                    rec.id, email_values={"email_to": email, "recipient_ids": []})
            except Exception:  # the request is saved whatever happens to the email
                _logger.exception("Could not queue the confirmation email of service request %s", rec.name)

    @api.model
    def attach_guest_requests(self, user):
        """Requests sent as a guest with the account's email and phone join the account at sign-in.

        Both must match: neither is verified on sign-up, and one alone would show a stranger's request.
        """
        email = (user.email or user.login or "").strip().lower()
        phone = re.sub(r"\s", "", user.phone or "")
        if not email or not phone:
            return self.browse()
        guests = self.sudo().search([("partner_id.user_ids", "=", False), ("partner_id.email", "=ilike", email)])
        guests = guests.filtered(lambda r: (r.partner_id.email or "").strip().lower() == email  # =ilike reads _ as a wildcard
                                 and re.sub(r"\s", "", r.phone or "") == phone)
        if guests:
            guests.partner_id = user.partner_id.commercial_partner_id
        return guests
