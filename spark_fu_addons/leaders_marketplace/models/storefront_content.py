"""Storefront content managed in Odoo: equipment listings, site settings, legal pages, the company film,
customer testimonials and the wishlist."""
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

YOUTUBE_RE = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/)|youtu\.be/)([\w-]{11})")
VIDEO_KINDS = [("youtube", "YouTube"), ("file", "MP4 file")]


def video_asset(kind, url):
    """``VideoAsset`` of the storefront, or ``None``: a YouTube link becomes its youtube-nocookie embed URL."""
    url = (url or "").strip()
    if not kind or not url:
        return None
    if kind == "youtube":
        match = YOUTUBE_RE.search(url)
        if not match:
            return None
        url = "https://www.youtube-nocookie.com/embed/%s" % match.group(1)
    return {"kind": kind, "url": url}


def _check_video(kind, url):
    if kind and (url or "").strip():
        if kind == "youtube" and not YOUTUBE_RE.search(url):
            raise ValidationError(_("Enter a YouTube video link, e.g. https://www.youtube.com/watch?v=..."))
        if kind == "file" and not url.strip().lower().startswith("https://"):
            raise ValidationError(_("The MP4 file must be an https:// link."))


class LeadersEquipmentListing(models.Model):
    """A customer offering a machine for sale or rent (SL-), sent from the storefront."""
    _name = "leaders.equipment.listing"
    _description = "Equipment Listing Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char("Reference", readonly=True, copy=False, index=True, default="New")
    state = fields.Selection([("new", "New"), ("contacted", "Contacted"), ("done", "Done"), ("rejected", "Rejected")],
                             default="new", required=True, tracking=True)
    contact_name = fields.Char("Name", required=True)
    phone = fields.Char(required=True)
    intent = fields.Selection([("sell", "Sell"), ("rent", "Rent")], required=True)
    equipment_type = fields.Char("Equipment", required=True)
    condition = fields.Selection([("new", "New"), ("used", "Used"), ("refurbished", "Refurbished")], required=True)
    quantity = fields.Integer(default=1, required=True)
    description = fields.Text()
    locale = fields.Selection([("ar", "Arabic"), ("en", "English")], default="ar")
    user_id = fields.Many2one("res.users", "Customer account", ondelete="set null", index=True,
                              help="Set when the customer was signed in.")
    request_ip = fields.Char("Sent from IP", readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code("leaders.equipment.listing") or "New"
        return super().create(vals_list)

    def notify_sales(self):
        """A to-do for every sales user, so a listing does not sit unseen."""
        group = self.env.ref("leaders_marketplace.group_equipment_listing_sales", raise_if_not_found=False)
        users = group.sudo().users.filtered("active") if group else self.env["res.users"]
        for rec in self:
            for user in users:
                summary = self.with_context(lang=user.lang or "en_US").env._("New equipment listing %s", rec.name)
                rec.sudo().activity_schedule("mail.mail_activity_data_todo", user_id=user.id, summary=summary,
                                             date_deadline=fields.Date.context_today(rec))


class LeadersSiteSettings(models.Model):
    """The company details printed on every page, and the company film of the landing page (one record)."""
    _name = "leaders.site.settings"
    _description = "Storefront Site Settings"

    name = fields.Char(default="Storefront", required=True)
    phone = fields.Char(help="E.164, e.g. +201001234567.")
    whatsapp = fields.Char("WhatsApp", help="E.164, e.g. +201001234567.")
    email = fields.Char()
    address = fields.Char(translate=True)
    legal_name = fields.Char("Legal name", translate=True)
    commercial_register = fields.Char()
    app_store_url = fields.Char("App Store link")
    google_play_url = fields.Char("Google Play link")
    story_video_kind = fields.Selection(VIDEO_KINDS, "Company film")
    story_video_url = fields.Char("Company film link", help="A YouTube link, or an https:// link to an MP4 file.")
    story_poster = fields.Image("Company film poster", max_width=1920, max_height=1080)

    @api.constrains("story_video_kind", "story_video_url")
    def _check_story_video(self):
        for rec in self:
            _check_video(rec.story_video_kind, rec.story_video_url)

    @api.model
    def get_settings(self):
        return self.sudo().search([], limit=1) or self.sudo().create({})

    @api.model
    def action_open(self):
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.get_settings().id,
                "view_mode": "form", "target": "current", "name": _("Storefront Site Settings")}


class LeadersSitePage(models.Model):
    """A text page of the storefront (terms, privacy, about), edited in Arabic and English."""
    _name = "leaders.site.page"
    _description = "Storefront Page"
    _order = "slug"

    slug = fields.Char(required=True, index=True, help="The page URL on the storefront: terms, privacy, about.")
    title = fields.Char(required=True, translate=True)
    content = fields.Html(translate=True, sanitize=True)
    published = fields.Boolean(help="Unpublished pages answer 404.")

    _sql_constraints = [("slug_uniq", "unique(slug)", "A page with this slug already exists.")]

    @api.model
    def _leaders_seed_pages(self):
        """Give the legal pages (privacy, terms, rent-contract) their text, in English and Arabic, and publish
        them. Only a page with no text yet is filled, so what the admin wrote is never overwritten."""
        from .site_pages_seed import PAGES
        arabic = self.env["res.lang"].search([("code", "like", "ar%"), ("active", "=", True)], limit=1)
        for slug, page in PAGES.items():
            english = self.sudo().with_context(lang="en_US", active_test=False)  # English is the source text
            record = english.search([("slug", "=", slug)], limit=1)
            if record and record.content:
                continue
            title, content = page["title"], page["content"]
            if not record:
                record = english.create({"slug": slug, "title": title[0]})
            record.write({"title": title[0], "content": content[0], "published": True})
            if arabic:
                record.with_context(lang=arabic.code).write({"title": title[1], "content": content[1]})
        return True


class LeadersTestimonial(models.Model):
    """A customer quote of the landing page, with an optional video."""
    _name = "leaders.testimonial"
    _description = "Customer Testimonial"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, translate=True)
    role = fields.Char(required=True, translate=True)
    company = fields.Char(translate=True)
    quote = fields.Char(required=True, translate=True, help="At most 160 characters.")
    image = fields.Image(required=True, max_width=1024, max_height=1024, help="Portrait, also shown before the video plays.")
    video_kind = fields.Selection(VIDEO_KINDS, "Video")
    video_url = fields.Char("Video link", help="A YouTube link, or an https:// link to an MP4 file.")
    permission = fields.Boolean("Customer gave permission",
                                help="Required before publishing: the customer agreed to show their name, picture and words.")
    published = fields.Boolean()

    @api.constrains("quote", "permission", "published", "video_kind", "video_url")
    def _check_testimonial(self):
        for rec in self:
            if rec.published and not rec.permission:
                raise ValidationError(_("Tick \"Customer gave permission\" before publishing a testimonial."))
            for lang in self.env["res.lang"].get_installed():
                if len(rec.with_context(lang=lang[0]).quote or "") > 160:
                    raise ValidationError(_("The quote can be at most 160 characters."))
            _check_video(rec.video_kind, rec.video_url)


class LeadersWishlistItem(models.Model):
    _name = "leaders.wishlist.item"
    _description = "Storefront Wishlist Item"
    _order = "id desc"

    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    product_tmpl_id = fields.Many2one("product.template", required=True, ondelete="cascade")

    _sql_constraints = [("user_product_uniq", "unique(user_id, product_tmpl_id)", "Already in the wishlist.")]


LOGO_TYPES = ("image/png", "image/svg+xml", "image/jpeg", "image/webp")


class LeadersSitePartner(models.Model):
    """A partner or client logo of the scrolling strip on the home page and the how-it-works page (GET /partners)."""
    _name = "leaders.site.partner"
    _description = "Partner / Client Logo"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, translate=True,
                       help="The partner's name, in Arabic and English: the alt text of the logo for screen readers.")
    logo = fields.Binary(required=True, attachment=True,
                         help="A horizontal logo on a transparent background, SVG or PNG, about 240x108 px or larger. "
                              "Upload its normal colours: the website shows every logo in one colour itself.")
    logo_name = fields.Char()
    website = fields.Char(help="Optional: the logo opens this site in a new tab. Starts with https://.")
    active = fields.Boolean(default=True)

    @api.constrains("logo", "website")
    def _check_partner(self):
        import base64
        from odoo.tools.mimetypes import guess_mimetype
        for rec in self:
            if rec.logo:
                raw = base64.b64decode(rec.with_context(bin_size=False).logo)
                if guess_mimetype(raw, default="") not in LOGO_TYPES:
                    raise ValidationError(_("The logo must be an SVG, PNG, JPEG or WebP picture."))
            if rec.website and not rec.website.startswith(("https://", "http://")):
                raise ValidationError(_("The website must be a link that starts with https://."))
