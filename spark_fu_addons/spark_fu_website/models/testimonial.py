import base64

from odoo import models, fields, api
from odoo.exceptions import ValidationError
from odoo.tools.image import binary_to_image
from odoo.tools.mimetypes import guess_mimetype

from .res_config_settings import DRIVE_HELP, VIDEO_LINK_ERROR, drive_sharing_warning, get_video_embed_link, to_video_asset

QUOTE_MAX_LENGTH = 160
IMAGE_ALLOWED_TYPES = ['image/jpeg', 'image/webp']
IMAGE_MAX_SIZE = 300 * 1024
IMAGE_MIN_WIDTH = 630
IMAGE_MIN_HEIGHT = 914


class SparkTestimonial(models.Model):
    _name = 'spark.testimonial'
    _description = 'Customer Testimonial'
    _order = 'sequence, id'

    sequence = fields.Integer(string='Sequence', default=10)
    name = fields.Char(string='Name', required=True, translate=True)
    role = fields.Char(string='Role', required=True, translate=True)
    company = fields.Char(string='Company', translate=True)
    quote = fields.Text(
        string='Quote',
        required=True,
        translate=True,
        help='Up to 160 characters, so it fits on the card in both languages.',
    )
    image = fields.Binary(
        string='Picture',
        required=True,
        attachment=True,
        help='Portrait, at least 630 x 914 px, JPG or WebP, under 300 KB. Keep the face above the centre.',
    )
    video_url = fields.Char(
        string='Video Link',
        help='Optional. Paste a YouTube or Google Drive video link: it is changed to the embed link when you save. '
             'Portrait 9:16 is best. ' + DRIVE_HELP,
    )
    customer_permission = fields.Boolean(
        string='Customer Gave Permission',
        help='The customer agreed to show their name, picture and words in public. Required before publishing.',
    )
    permission_user_id = fields.Many2one(
        'res.users',
        string='Permission Ticked By',
        compute='_compute_permission_user_id',
        store=True,
        readonly=True,
    )
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('published', 'Published'),
        ],
        string='Status',
        default='draft',
        required=True,
    )

    @api.depends('customer_permission')
    def _compute_permission_user_id(self):
        for testimonial in self:
            if testimonial.customer_permission:
                testimonial.permission_user_id = self.env.user
            else:
                testimonial.permission_user_id = False

    @api.onchange('video_url')
    def _onchange_video_url(self):
        if self.video_url and not to_video_asset(self.video_url):
            return {'warning': {'title': 'Video link', 'message': VIDEO_LINK_ERROR}}
        message = drive_sharing_warning(self.video_url)
        if message:
            return {'warning': {'title': 'Google Drive sharing', 'message': message}}

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'video_url' in vals:
                vals['video_url'] = get_video_embed_link(vals['video_url'])
        return super().create(vals_list)

    def write(self, vals):
        if 'video_url' in vals:
            vals['video_url'] = get_video_embed_link(vals['video_url'])
        return super().write(vals)

    def update_field_translations(self, field_name, translations, source_lang=None):
        if field_name == 'quote':
            for translated_quote in translations.values():
                self._check_quote_length(translated_quote)
        return super().update_field_translations(field_name, translations, source_lang=source_lang)

    def _check_quote_length(self, quote):
        if quote and len(quote) > QUOTE_MAX_LENGTH:
            raise ValidationError('The quote must be 160 characters or less.')

    @api.constrains('quote')
    def _check_quote(self):
        for testimonial in self:
            self._check_quote_length(testimonial.quote)

    def action_publish(self):
        for testimonial in self:
            testimonial.state = 'published'

    def action_draft(self):
        for testimonial in self:
            testimonial.state = 'draft'

    @api.constrains('state', 'customer_permission')
    def _check_customer_permission(self):
        for testimonial in self:
            if testimonial.state == 'published' and not testimonial.customer_permission:
                raise ValidationError('Tick Customer Gave Permission before publishing a testimonial.')

    @api.constrains('image')
    def _check_image(self):
        for testimonial in self:
            image = testimonial.with_context(bin_size=False).image
            if not image:
                continue

            image_data = base64.b64decode(image)

            if guess_mimetype(image_data) not in IMAGE_ALLOWED_TYPES:
                raise ValidationError('The picture must be a JPG or WebP image.')

            if len(image_data) > IMAGE_MAX_SIZE:
                raise ValidationError('The picture must be under 300 KB.')

            width, height = binary_to_image(image_data).size
            if width < IMAGE_MIN_WIDTH or height < IMAGE_MIN_HEIGHT:
                raise ValidationError('The picture must be at least 630 x 914 px.')
