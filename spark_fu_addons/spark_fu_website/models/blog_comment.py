from odoo import models, fields, api
from odoo.exceptions import ValidationError


class SparkBlogComment(models.Model):
    _name = 'spark.blog.comment'
    _description = 'Blog Comment'
    _order = 'create_date desc, id desc'
    _rec_name = 'body'

    post_id = fields.Many2one('spark.blog.post', string='Post', required=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', string='Author', required=True)
    body = fields.Text(string='Comment', required=True)
    parent_id = fields.Many2one('spark.blog.comment', string='Reply To', ondelete='cascade')
    reply_ids = fields.One2many('spark.blog.comment', 'parent_id', string='Replies')
    like_ids = fields.One2many('spark.blog.comment.like', 'comment_id', string='Likes')
    like_count = fields.Integer(string='Likes Count', compute='_compute_like_count')
    is_liked_by_me = fields.Boolean(string='Liked By Me', compute='_compute_is_liked_by_me')
    active = fields.Boolean(string='Visible', default=True)
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('published', 'Published'),
        ],
        string='Status',
        default='published',
        required=True,
    )
    is_deleted = fields.Boolean(string='Deleted By Author', default=False)
    edited_at = fields.Datetime(string='Edited At', readonly=True)

    @api.depends('like_ids')
    def _compute_like_count(self):
        for comment in self:
            comment.like_count = len(comment.like_ids)

    @api.depends('like_ids')
    def _compute_is_liked_by_me(self):
        for comment in self:
            comment.is_liked_by_me = False
            for like in comment.like_ids:
                if like.user_id == self.env.user:
                    comment.is_liked_by_me = True

    # def action_approve(self):
    #     for comment in self:
    #         comment.state = 'published'

    # def action_reject(self):
    #     for comment in self:
    #         comment.active = False

    def action_toggle_like(self):
        like_model = self.env['spark.blog.comment.like']
        for comment in self:
            existing_like = like_model.search([
                ('comment_id', '=', comment.id),
                ('user_id', '=', self.env.user.id),
            ], limit=1)
            if existing_like:
                existing_like.unlink()
            else:
                like_model.create({
                    'comment_id': comment.id,
                    'user_id': self.env.user.id,
                })

    @api.constrains('parent_id', 'post_id')
    def _check_parent(self):
        for comment in self:
            if not comment.parent_id:
                continue
            if comment.parent_id.post_id != comment.post_id:
                raise ValidationError("A reply must be on the same post as its comment.")
            if comment.parent_id.parent_id:
                raise ValidationError("You can not reply to a reply.")


class SparkBlogCommentLike(models.Model):
    _name = 'spark.blog.comment.like'
    _description = 'Blog Comment Like'
    _order = 'create_date desc, id desc'

    comment_id = fields.Many2one('spark.blog.comment', string='Comment', required=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', string='User', required=True, ondelete='cascade')
    post_id = fields.Many2one(related='comment_id.post_id', string='Post', store=True)

    _sql_constraints = [
        ('uniq_comment_user', 'unique(comment_id, user_id)', 'A user can like a comment only once.'),
    ]
