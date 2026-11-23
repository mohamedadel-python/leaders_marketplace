"""Who carries out a service: the platform itself, and the vendors who chose it from the platform's services.

A vendor can still write a service of his own (``leaders.vendor.service``). He can also say "I provide this one of the
platform's services" (inspection, installation, maintenance...): that is a ``leaders.service.provider``. The marketplace
approves it, and the vendor then shows on the providers page of the service (``GET /services/<slug>/providers``). The
customer may pick a provider when he asks for the service; the request is then the provider's.
"""
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .catalog_spec import slug_ascii
from .vendor_service import STATES

BANDS = ((10, {"min": 1, "max": 10}), (50, {"min": 10, "max": 50}))  # 1-10, 10-50, then 50+
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class LeadersServiceSpecialty(models.Model):
    """What a provider is good at (generators, load banks...): a list the back office keeps, in Arabic and English."""
    _name = "leaders.service.specialty"
    _description = "Service Specialty"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    slug = fields.Char("Code", index=True, copy=False, help="Its stable id in the API; made from the English name.")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [("slug_unique", "unique(slug)", "This code is already used by another specialty.")]

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records.filtered(lambda r: not r.slug):
            base = slug_ascii(rec.with_context(lang="en_US").name or rec.name)[:50].strip("-") or "specialty"
            slug, n = base, 1
            while self.sudo().with_context(active_test=False).search_count([("slug", "=", slug), ("id", "!=", rec.id)]):
                n += 1
                slug = "%s-%s" % (base, n)
            rec.slug = slug
        return records

    @api.constrains("slug")
    def _check_slug(self):
        for rec in self:
            if rec.slug and not SLUG_RE.match(rec.slug):
                raise ValidationError(_("The code is lower-case letters, digits and dashes only."))


class LeadersServiceProvider(models.Model):
    _name = "leaders.service.provider"
    _description = "Service Provider"
    _inherit = ["mail.thread", "leaders.backoffice.mixin"]
    _order = "service_id, is_platform desc, id"

    service_id = fields.Many2one(
        "product.template", "Service", required=True, index=True, ondelete="cascade",
        domain=[("leaders_service_page", "=", True), ("leaders_service_vendor_id", "=", False)],
        help="One of the platform's services.")
    is_platform = fields.Boolean("The platform itself", index=True,
                                 help="The platform's own team carries the service out: its card comes first.")
    vendor_id = fields.Many2one("res.partner", "Vendor", index=True, ondelete="cascade",
                                domain=[("is_marketplace_vendor", "=", True)])
    name = fields.Char("Name shown", translate=True,
                       help="Leave empty to show the vendor's store name (the company's name for the platform).")
    state = fields.Selection(STATES, default="draft", required=True, tracking=True, index=True)
    reject_reason = fields.Char("Reason of the refusal", copy=False, tracking=True)
    submitted_on = fields.Datetime(readonly=True, copy=False)
    decided_on = fields.Datetime(readonly=True, copy=False)
    logo = fields.Image("Logo", max_width=256, max_height=256,
                        help="Shown on the card. Empty: the vendor's logo (the company's for the platform).")
    verified = fields.Boolean("Verified provider", default=True, tracking=True,
                              help="Shows the green 'verified' badge on the card.")
    response_hours = fields.Integer("Response time (hours)", default=24,
                                    help="How fast the provider answers a request: 'within N hours'.")
    all_governorates = fields.Boolean("All governorates", default=True, help="The provider covers every governorate.")
    governorate_ids = fields.Many2many("res.country.state", "leaders_service_provider_state_rel", "provider_id", "state_id",
                                       string="Governorates covered",
                                       domain=[("country_id.code", "=", "EG")])
    specialty_ids = fields.Many2many("leaders.service.specialty", "leaders_service_provider_specialty_rel",
                                     "provider_id", "specialty_id", string="Specialties")
    active = fields.Boolean(default=True)
    jobs = fields.Integer("Completed jobs", compute="_compute_jobs", help="Service requests this provider completed.")

    _sql_constraints = [("vendor_service_unique", "unique(vendor_id, service_id)",
                         "This vendor already provides this service.")]

    # ---- who is it -------------------------------------------------------------------------------------------
    @api.constrains("is_platform", "vendor_id", "service_id")
    def _check_who(self):
        for rec in self:
            if bool(rec.is_platform) == bool(rec.vendor_id):
                raise ValidationError(_("A provider is the platform itself or one vendor."))
            if rec.service_id.leaders_service_vendor_id:
                raise ValidationError(_("Choose one of the platform's services, not a vendor's own service."))
            if rec.is_platform and self.sudo().with_context(active_test=False).search_count(
                    [("is_platform", "=", True), ("service_id", "=", rec.service_id.id), ("id", "!=", rec.id)]):
                raise ValidationError(_("The platform is already a provider of this service."))

    @api.constrains("response_hours")
    def _check_hours(self):
        for rec in self:
            if rec.response_hours < 0 or rec.response_hours > 24 * 60:
                raise ValidationError(_("The response time is a number of hours, up to 1440."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("is_platform") and not vals.get("vendor_id"):
                vals.setdefault("state", "approved")  # the platform is its own approver
        records = super().create(vals_list)
        for rec in records.filtered(lambda r: r.is_platform and not r.name):
            rec.name = "Spark Fu"  # the card of the platform; the Arabic name is set in the translations
            rec.with_context(lang=self._leaders_arabic_code()).name = "سبارك فو" if self._leaders_arabic_code() else rec.name
        return records

    @api.model
    def _leaders_arabic_code(self):
        lang = self.env["res.lang"].sudo().search([("code", "like", "ar%"), ("active", "=", True)], limit=1)
        return lang.code or None

    def _leaders_provider_name(self):
        self.ensure_one()
        vendor = self.vendor_id.commercial_partner_id
        return self.name or ((vendor.vendor_store_name or vendor.name) if vendor else self.env.company.name) or ""

    def _compute_jobs(self):
        Request = self.env["leaders.customer.request"].sudo()
        for rec in self:
            domain = [("kind", "=", "service"), ("state", "=", "completed")]
            if rec.is_platform:  # the platform's: the requests nobody else was chosen for
                domain += [("service_id", "=", rec.service_id.id), "|", ("service_provider_id", "=", False),
                           ("service_provider_id", "=", rec.id)]
            else:
                domain += [("service_provider_id", "=", rec.id)]
            rec.jobs = Request.search_count(domain) if rec.id else 0

    @staticmethod
    def _leaders_band(count):
        """The number of jobs as a band, so a vendor's exact volume is not shown: ``{min, max}`` or ``{min}`` (50+)."""
        for top, band in BANDS:
            if count < top:
                return dict(band)
        return {"min": BANDS[-1][0]}

    # ---- the vendor sends it, the back office decides ----------------------------------------------------------
    def action_submit(self):
        for rec in self:
            if not rec.vendor_id:
                raise UserError(_("Only a vendor's offer is sent for approval."))
            if not rec.service_id or (not rec.all_governorates and not rec.governorate_ids):
                raise UserError(_("Choose the service and where you carry it out."))
            rec.sudo().write({"state": "submitted", "submitted_on": fields.Datetime.now(), "reject_reason": False})
            rec.sudo()._leaders_tell_staff()
        return True

    def _leaders_reviewers(self):
        groups = (self.env.ref("leaders_marketplace.group_marketplace_reviewer")
                  | self.env.ref("leaders_marketplace.group_marketplace_manager"))
        return self.env["res.users"].sudo().search([("groups_id", "in", groups.ids), ("share", "=", False)])

    def _leaders_tell_staff(self):
        self.ensure_one()
        service = self.service_id.with_context(lang="en_US").name
        self.message_post(
            body=_("%(vendor)s offered to provide the service %(name)s.", vendor=self.vendor_id.display_name, name=service),
            partner_ids=self._leaders_reviewers().partner_id.ids, message_type="comment", subtype_xmlid="mail.mt_comment")
        self.env["leaders.push.helpers"]._lp_staff(self, "staff_vendor_provider", lambda r: (
            r.env._("A vendor offers to provide a service"),
            r.env._("%(vendor)s: %(name)s", vendor=self.vendor_id.display_name, name=r.service_id.name)),
            {"model": self._name, "id": str(self.id)})

    def action_approve(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self:
            if rec.state not in ("draft", "submitted"):
                raise UserError(_("Only an offer waiting for approval can be approved."))
            rec.write({"state": "approved", "decided_on": fields.Datetime.now(), "reject_reason": False})
            rec.sudo().message_post(body=_("The offer was approved: the vendor is a provider of the service."))
            if rec.vendor_id:
                self.env["leaders.notification"].notify_vendor("vendor_provider_approved", rec, rec.vendor_id)
        return True

    def action_reject(self):
        self._leaders_check_backoffice("reviewer")
        for rec in self:
            if rec.state not in ("draft", "submitted"):
                raise UserError(_("Only an offer waiting for approval can be refused."))
            if not (rec.reject_reason or "").strip():
                raise UserError(_("Write the reason of the refusal first."))
            rec.write({"state": "rejected", "decided_on": fields.Datetime.now()})
            rec.sudo().message_post(body=_("The offer was refused: %s", rec.reject_reason))
            if rec.vendor_id:
                self.env["leaders.notification"].notify_vendor("vendor_provider_rejected", rec, rec.vendor_id)
        return True

    # ---- the public list -------------------------------------------------------------------------------------
    @api.model
    def _leaders_public(self, service):
        """The providers of ``service`` the storefront lists: approved, and a vendor must be an approved one that
        sells under his own name (a commission vendor sells under the platform's name: he is not listed)."""
        rows = self.sudo().search([("service_id", "=", service.id), ("state", "=", "approved"), ("active", "=", True)])
        return rows.filtered(lambda p: p.is_platform or (
            p.vendor_id.is_marketplace_vendor and p.vendor_id.vendor_state == "approved"
            and not p.vendor_id._leaders_name_hidden()))


class ProductTemplateProviders(models.Model):
    _inherit = "product.template"

    leaders_provider_ids = fields.One2many("leaders.service.provider", "service_id", string="Providers")
    leaders_service_coverage_text = fields.Char(
        "Coverage text", translate=True, help="The first fact of the service panel, e.g. '29 governorates'.")
    leaders_service_response_text = fields.Char(
        "Response text", translate=True, help="The third fact of the service panel, e.g. 'within 24 working hours'.")
    leaders_service_price_note = fields.Char(
        "Price note", translate=True, help="The note under the facts of the panel, about how the price is set.")
