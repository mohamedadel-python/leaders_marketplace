from odoo import models, fields


class ResUsers(models.Model):
    _inherit = 'res.users'

    blog_comment_trusted = fields.Boolean(
        string='Trusted Blog Commenter',
        help='Comments of this user are published at once, without waiting for approval. Internal users are always trusted.',
    )
