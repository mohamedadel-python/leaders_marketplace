import re
import unicodedata

from odoo import models, fields, api
from odoo.exceptions import ValidationError
from odoo.tools.image import base64_to_image

from ..tools.html_cleaner import clean_post_html


EXCERPT_MIN_LENGTH = 3
EXCERPT_MAX_LENGTH = 160

IMAGE_MIN_WIDTH = 1200
IMAGE_MIN_HEIGHT = 630

def make_slug(text):
    if not text:
        return ""

    text = text.strip()
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = re.sub(r"[\s_/\\|]+", " ", text)
    text = re.sub(r"[^a-z0-9؀-ۿ\s-]+", "", text)
    text = re.sub(r"[-\s]+", "-", text)
    text = text.strip("-")
    return text


class SparkBlogPost(models.Model):
    _name = 'spark.blog.post'
    _description = 'Blog Post'
    _order = 'create_date desc'
    _rec_name = 'title'

    def _default_author_id(self):
        return self.env.user

    title = fields.Char(string='Title', required=True, translate=True)
    content = fields.Html(string='Content (ar)', required=True)
    content_en = fields.Html(string='Content (en)')
    tag_ids = fields.Many2many(
        'spark.blog.tag',
        'spark_blog_post_tag_rel',
        'post_id',
        'tag_id',
        string='Tags',
    )
    visits = fields.Integer(string='Visits', default=0, readonly=True)
    author_id = fields.Many2one(
        'res.users',
        string='Author',
        default=_default_author_id,
        required=True,
    )
    publish_date = fields.Datetime(string='Publish Date', default=fields.Datetime.now)
    is_published = fields.Boolean(string='Published', default=False)
    excerpt = fields.Text(string='Excerpt', required=True, translate=True)
    image = fields.Image(
        string='Image',
        help='Cover image, also used as the Open Graph image. At least 1200 x 630 px.',
    )
    type = fields.Selection(
        [
            ('blog', 'Blog'),
            ('news', 'News'),
        ],
        string='Type',
        required=True,
        default='blog',
    )
    active = fields.Boolean(string='Active', default=True)
    slug = fields.Char(string='Slug', index=True)
    is_featured = fields.Boolean(string='Featured', default=False)
    featured_order = fields.Integer(string='Featured Order', default=10)
    content_updated_at = fields.Datetime(string='Last Content Update', readonly=True)
    comment_ids = fields.One2many('spark.blog.comment', 'post_id', string='Comments')
    comment_count = fields.Integer(string='Comments Count', compute='_compute_comment_count')
    slug_history_ids = fields.One2many('spark.blog.slug.history', 'post_id', string='Old Slugs')

    _sql_constraints = [
        ('uniq_spark_blog_slug', 'unique(slug)', 'Slug must be unique.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('slug') and vals.get('title'):
                vals['slug'] = make_slug(vals['title'])
            if vals.get('content'):
                vals['content'] = clean_post_html(vals['content'])
            if vals.get('content_en'):
                vals['content_en'] = clean_post_html(vals['content_en'])
        return super().create(vals_list)

    def write(self, vals):
        content_fields = ['title', 'excerpt', 'content', 'content_en', 'image']
        for field_name in content_fields:
            if field_name in vals:
                vals['content_updated_at'] = fields.Datetime.now()

        if vals.get('content'):
            vals['content'] = clean_post_html(vals['content'])

        if vals.get('content_en'):
            vals['content_en'] = clean_post_html(vals['content_en'])

        if 'slug' in vals:
            self._save_old_slugs(vals['slug'])

        return super().write(vals)

    def _save_old_slugs(self, new_slug):
        history_model = self.env['spark.blog.slug.history']

        same_as_new = history_model.search([('slug', '=', new_slug)])
        same_as_new.unlink()

        for post in self:
            if not post.slug or post.slug == new_slug:
                continue
            old_history = history_model.search([('slug', '=', post.slug)], limit=1)
            if old_history:
                old_history.post_id = post.id
            else:
                history_model.create({'post_id': post.id, 'slug': post.slug})

    def _compute_comment_count(self):
        comment_model = self.env['spark.blog.comment']
        for post in self:
            top_level_count = comment_model.search_count([
                ('post_id', '=', post.id),
                ('parent_id', '=', False),
                ('active', '=', True),
                ('state', '=', 'published'),
                ('is_deleted', '=', False),
            ])
            reply_count = comment_model.search_count([
                ('post_id', '=', post.id),
                ('parent_id', '!=', False),
                ('parent_id.state', '=', 'published'),
                ('parent_id.active', '=', True),
                ('active', '=', True),
                ('state', '=', 'published'),
                ('is_deleted', '=', False),
            ])
            post.comment_count = top_level_count + reply_count

    @api.constrains('title')
    def _check_title_length(self):
        for record in self:
            if len(record.title) < 3:
                raise ValidationError("Title must be at least 3 characters long.")

    @api.constrains('excerpt')
    def _check_excerpt_length(self):
        for record in self:
            excerpt_length = len(record.excerpt or '')
            if excerpt_length < EXCERPT_MIN_LENGTH or excerpt_length > EXCERPT_MAX_LENGTH:
                raise ValidationError("Excerpt must be between 3 and 160 characters.")

    @api.constrains('image')
    def _check_image_size(self):
        for record in self:
            if not record.image:
                continue
            image = base64_to_image(record.image)
            width, height = image.size
            if width < IMAGE_MIN_WIDTH or height < IMAGE_MIN_HEIGHT:
                raise ValidationError("The cover image must be at least 1200 x 630 px.")

    @api.constrains('slug')
    def _check_slug(self):
        for record in self:
            if record.slug == 'featured':
                raise ValidationError("The slug 'featured' is reserved. Please choose another slug.")

    def increment_visits(self):
        for record in self:
            record.visits = record.visits + 1

    def action_publish(self):
        for record in self:
            record.is_published = True
            record.publish_date = fields.Datetime.now()

    def action_unpublish(self):
        for record in self:
            record.is_published = False


class SparkBlogTag(models.Model):
    _name = 'spark.blog.tag'
    _description = 'Blog Tag'
    _order = 'name'

    name = fields.Char(string='Tag Name', required=True)
    color = fields.Integer(string='Color')
    post_ids = fields.Many2many(
        'spark.blog.post',
        'spark_blog_post_tag_rel',
        'tag_id',
        'post_id',
        string='Posts',
    )
    post_count = fields.Integer(string='Number of Posts', compute='_compute_post_count')

    @api.depends('post_ids')
    def _compute_post_count(self):
        for tag in self:
            tag.post_count = len(tag.post_ids)

    @api.constrains('name')
    def _check_unique_name(self):
        for record in self:
            same_name_count = self.search_count([
                ('name', '=ilike', record.name),
                ('id', '!=', record.id),
            ])
            if same_name_count > 0:
                raise ValidationError("Tag name must be unique.")


class SparkBlogSlugHistory(models.Model):
    _name = 'spark.blog.slug.history'
    _description = 'Old Blog Post Slug'
    _order = 'create_date desc, id desc'
    _rec_name = 'slug'

    post_id = fields.Many2one('spark.blog.post', string='Post', required=True, ondelete='cascade')
    slug = fields.Char(string='Old Slug', required=True, index=True)

    _sql_constraints = [
        ('uniq_old_slug', 'unique(slug)', 'This old slug is already saved.'),
    ]
