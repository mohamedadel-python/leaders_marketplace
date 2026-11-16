from odoo import models, fields, api


ISSUE_TYPES = [
    ('general', 'General'),
    ('order', 'Order'),
    ('technical', 'Technical'),
    ('billing', 'Billing'),
    ('other', 'Other'),
]

SUBJECTS = [
    ('sales', 'Sales'),
    ('rental', 'Rental'),
    ('parts', 'Parts'),
    ('services', 'Services'),
    ('partnership', 'Partnership'),
    ('other', 'Other'),
]


class SparkContactMessage(models.Model):
    _name = 'spark.contact.message'
    _description = 'Contact Message'
    _order = 'create_date desc, id desc'
    _rec_name = 'reference'

    reference = fields.Char(string='Reference', readonly=True, copy=False, default='New')
    name = fields.Char(string='Name', required=True)
    email = fields.Char(string='Email', required=True)
    phone = fields.Char(string='Phone', required=True)
    issue_type = fields.Selection(ISSUE_TYPES, string='Issue Type', required=True)
    subject = fields.Selection(SUBJECTS, string='Subject', required=True)
    message = fields.Text(string='Message', required=True)
    locale = fields.Selection(
        [
            ('en', 'English'),
            ('ar', 'Arabic'),
        ],
        string='Language',
        required=True,
        default='en',
    )
    attachment_ids = fields.Many2many('ir.attachment', string='Attachments')
    user_id = fields.Many2one('res.users', string='Signed-in User', readonly=True)
    responsible_id = fields.Many2one('res.users', string='Responsible')
    ip_address = fields.Char(string='IP Address', readonly=True)
    state = fields.Selection(
        [
            ('new', 'New'),
            ('in_progress', 'In Progress'),
            ('done', 'Done'),
        ],
        string='Status',
        default='new',
        required=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('reference') or vals.get('reference') == 'New':
                vals['reference'] = self.env['ir.sequence'].sudo().next_by_code('spark.contact.message')
            if not vals.get('responsible_id') and vals.get('subject'):
                route = self.env['spark.contact.route'].sudo().search([
                    ('subject', '=', vals['subject']),
                ], limit=1)
                if route:
                    vals['responsible_id'] = route.user_id.id
        return super().create(vals_list)

    def get_confirmation_subject(self):
        self.ensure_one()
        if self.locale == 'ar':
            return 'تم استلام رسالتك - ' + self.reference
        return 'We received your message - ' + self.reference

    def action_start(self):
        for record in self:
            record.state = 'in_progress'

    def action_done(self):
        for record in self:
            record.state = 'done'

    def action_reset(self):
        for record in self:
            record.state = 'new'


class SparkContactRoute(models.Model):
    _name = 'spark.contact.route'
    _description = 'Contact Subject Routing'
    _rec_name = 'subject'

    subject = fields.Selection(SUBJECTS, string='Subject', required=True)
    user_id = fields.Many2one('res.users', string='Responsible', required=True)

    _sql_constraints = [
        ('uniq_subject', 'unique(subject)', 'Each subject can have only one routing line.'),
    ]
