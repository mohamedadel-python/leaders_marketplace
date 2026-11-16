import base64
import logging
import re
from datetime import timedelta

from odoo import http, fields
from odoo.http import request
from odoo.tools.mimetypes import guess_mimetype

from odoo.addons.leaders_marketplace.tools.auth_tokens import InvalidAccessToken, decode_access_token

_logger = logging.getLogger(__name__)

ISSUE_TYPES = ['general', 'order', 'technical', 'billing', 'other']
SUBJECTS = ['sales', 'rental', 'parts', 'services', 'partnership', 'other']
LOCALES = ['ar', 'en']
ALLOWED_FILE_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'application/pdf']

MAX_FILES = 3
MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_BODY_SIZE = 16 * 1024 * 1024

EMAIL_PATTERN = r'^[^@\s]+@[^@\s]+\.[^@\s]+$'
PHONE_PATTERN = r'^\+[1-9][0-9]{7,14}$'

DEFAULT_CORS_ORIGINS = 'http://localhost:3000,https://spark-fu.com'

ERROR_MESSAGES = {
    'file_too_large': {
        'en': 'A file is larger than 5 MB.',
        'ar': 'حجم أحد الملفات أكبر من 5 ميجابايت.',
    },
    'validation_failed': {
        'en': 'Validation failed.',
        'ar': 'البيانات غير صحيحة.',
    },
    'name_length': {
        'en': 'Name must be 2 to 120 characters.',
        'ar': 'يجب أن يكون الاسم بين 2 و120 حرفاً.',
    },
    'invalid_email': {
        'en': 'Invalid email.',
        'ar': 'البريد الإلكتروني غير صحيح.',
    },
    'invalid_phone': {
        'en': 'Phone must be in E.164 format, e.g. +201001234567.',
        'ar': 'يجب أن يكون رقم الهاتف بصيغة E.164، مثال: +201001234567.',
    },
    'invalid_issue_type': {
        'en': 'Choose a valid issue type.',
        'ar': 'اختر نوع مشكلة صحيحاً.',
    },
    'invalid_subject': {
        'en': 'Choose a valid subject.',
        'ar': 'اختر موضوعاً صحيحاً.',
    },
    'message_length': {
        'en': 'Message must be 10 to 2000 characters.',
        'ar': 'يجب أن تكون الرسالة بين 10 و2000 حرف.',
    },
    'invalid_locale': {
        'en': 'Locale must be ar or en.',
        'ar': 'يجب أن تكون اللغة ar أو en.',
    },
    'too_many_files': {
        'en': 'You can attach up to 3 files.',
        'ar': 'يمكنك إرفاق 3 ملفات كحد أقصى.',
    },
    'unsupported_file_type': {
        'en': 'Allowed files: images or PDF.',
        'ar': 'الملفات المسموحة: الصور أو PDF.',
    },
    'rate_limited': {
        'en': 'Too many messages. Wait a few minutes.',
        'ar': 'رسائل كثيرة جداً. انتظر بضع دقائق.',
    },
    'server_error': {
        'en': 'Could not send the message. Try again.',
        'ar': 'تعذّر إرسال الرسالة. حاول مرة أخرى.',
    },
}


class SparkContactApiController(http.Controller):

    def _get_cors_headers(self):
        allowed_origins_text = request.env['ir.config_parameter'].sudo().get_param(
            'spark_fu.cors_allowed_origins',
            DEFAULT_CORS_ORIGINS,
        )
        allowed_origins = []
        for origin in allowed_origins_text.split(','):
            allowed_origins.append(origin.strip())

        request_origin = request.httprequest.headers.get('Origin', '')
        if request_origin not in allowed_origins:
            return []

        return [
            ('Access-Control-Allow-Origin', request_origin),
            ('Access-Control-Allow-Credentials', 'true'),
            ('Access-Control-Allow-Methods', 'POST, OPTIONS'),
            ('Access-Control-Allow-Headers', 'Content-Type, Authorization'),
            ('Vary', 'Origin'),
        ]

    def _json_response(self, data, status):
        return request.make_json_response(data, headers=self._get_cors_headers(), status=status)

    def _get_message(self, message_key):
        accept_language = request.httprequest.headers.get('Accept-Language', '')
        first_language = accept_language.split(',')[0].strip().lower()
        if first_language.startswith('ar'):
            return ERROR_MESSAGES[message_key]['ar']
        return ERROR_MESSAGES[message_key]['en']

    def _error_response(self, status, code, message_key, field_errors=None):
        data = {
            'code': code,
            'message': self._get_message(message_key),
        }
        if field_errors:
            data['fields'] = field_errors
        headers = self._get_cors_headers()
        headers.append(('Cache-Control', 'no-store'))
        return request.make_json_response(data, headers=headers, status=status)

    def _get_logged_in_user(self):
        header = request.httprequest.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return None

        token = header[7:].strip()
        try:
            user_id = decode_access_token(request.env, token)
        except InvalidAccessToken:
            return None

        user = request.env['res.users'].sudo().browse(user_id)
        if not user.exists():
            return None
        return user

    def _get_text_param(self, name):
        value = request.params.get(name, '')
        if not isinstance(value, str):
            return ''
        return value.strip()

    def _validate_fields(self, values):
        field_errors = {}

        name_length = len(values['name'])
        if name_length < 2 or name_length > 120:
            field_errors['name'] = self._get_message('name_length')

        if not re.match(EMAIL_PATTERN, values['email']):
            field_errors['email'] = self._get_message('invalid_email')

        if not re.match(PHONE_PATTERN, values['phone']):
            field_errors['phone'] = self._get_message('invalid_phone')

        if values['issue_type'] not in ISSUE_TYPES:
            field_errors['issueType'] = self._get_message('invalid_issue_type')

        if values['subject'] not in SUBJECTS:
            field_errors['subject'] = self._get_message('invalid_subject')

        message_length = len(values['message'])
        if message_length < 10 or message_length > 2000:
            field_errors['message'] = self._get_message('message_length')

        if values['locale'] not in LOCALES:
            field_errors['locale'] = self._get_message('invalid_locale')

        return field_errors

    def _read_attachments(self):
        files = request.httprequest.files.getlist('attachments')

        attachments = []
        for uploaded_file in files:
            if not uploaded_file.filename:
                continue
            file_data = uploaded_file.read()
            attachments.append({
                'name': uploaded_file.filename,
                'data': file_data,
                'size': len(file_data),
                'mimetype': guess_mimetype(file_data),
            })
        return attachments

    def _is_rate_limited(self, email, ip_address):
        message_model = request.env['spark.contact.message'].sudo()
        now = fields.Datetime.now()

        ten_minutes_ago = now - timedelta(minutes=10)
        ip_count = message_model.search_count([
            ('ip_address', '=', ip_address),
            ('create_date', '>=', ten_minutes_ago),
        ])
        if ip_count >= 5:
            return True

        one_hour_ago = now - timedelta(hours=1)
        email_count = message_model.search_count([
            ('email', '=', email),
            ('create_date', '>=', one_hour_ago),
        ])
        if email_count >= 3:
            return True

        return False

    def _send_confirmation_email(self, contact_message):
        try:
            template = request.env.ref('spark_fu_website.mail_template_contact_confirmation').sudo()
            template.send_mail(contact_message.id)
        except Exception as error:
            _logger.error(f"Could not send contact confirmation email: {str(error)}")

    @http.route('/contact', type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def submit_contact(self, **kwargs):
        """
        Contact us form: opens a support ticket and returns its reference.

        URL: POST /contact
        Auth: none. Bearer token is optional:
        - Valid token: the ticket is linked to that user.
        - Wrong or expired token: ignored. Never returns 401.
        Body: multipart/form-data (not JSON), because of the attachments.

        Parts:
        - name: required, 2 to 120 characters (trimmed).
        - email: required, valid email.
        - phone: required, E.164 format, e.g. +201001234567.
        - issueType: required, one of general, order, technical, billing, other.
        - subject: required, one of sales, rental, parts, services, partnership, other.
        - message: required, 10 to 2000 characters (trimmed).
        - locale: required, ar or en. The confirmation email uses this language.
        - attachments: optional, repeated part, 0 to 3 files.
          Each file max 5 MB. Types: image/jpeg, image/png, image/webp, application/pdf.
        - The keys (order, rental, ...) are stored, not the translated labels.

        After saving:
        - The ticket gets a reference like SC-10001.
        - The ticket is assigned to the responsible user of its subject (Subject Routing menu).
        - A confirmation email is queued to the sender in the sender language.

        Response 201:
        {"reference": "SC-10001"}

        Errors, body is always {"code": "...", "message": "..."}:
        - 422 validation_error: a field rule fails. Adds "fields": {"phone": "...", ...}.
        - 413 file_too_large: a file over 5 MB, or the whole body over 16 MB.
        - 415 unsupported_file_type: a file that is not an image or a PDF.
        - 422 too_many_files: more than 3 attachments.
        - 429 rate_limited: more than 5 messages in 10 minutes from one IP,
          or more than 3 messages in one hour from one email.
        The message language follows the Accept-Language header (ar or en, default en).

        CORS:
        - Allowed origins come from the system parameter spark_fu.cors_allowed_origins
          (comma separated). The origin is echoed back with credentials allowed.
        - OPTIONS returns 204.
        """
        if request.httprequest.method == 'OPTIONS':
            return request.make_response('', headers=self._get_cors_headers(), status=204)

        content_length = request.httprequest.content_length
        if content_length and content_length > MAX_BODY_SIZE:
            return self._error_response(413, 'file_too_large', 'file_too_large')

        values = {
            'name': self._get_text_param('name'),
            'email': self._get_text_param('email').lower(),
            'phone': self._get_text_param('phone'),
            'issue_type': self._get_text_param('issueType'),
            'subject': self._get_text_param('subject'),
            'message': self._get_text_param('message'),
            'locale': self._get_text_param('locale'),
        }

        field_errors = self._validate_fields(values)
        if field_errors:
            return self._error_response(422, 'validation_error', 'validation_failed', field_errors)

        attachments = self._read_attachments()

        if len(attachments) > MAX_FILES:
            return self._error_response(422, 'too_many_files', 'too_many_files')

        for attachment in attachments:
            if attachment['size'] > MAX_FILE_SIZE:
                return self._error_response(413, 'file_too_large', 'file_too_large')
            if attachment['mimetype'] not in ALLOWED_FILE_TYPES:
                return self._error_response(415, 'unsupported_file_type', 'unsupported_file_type')

        ip_address = request.httprequest.remote_addr or ''
        if self._is_rate_limited(values['email'], ip_address):
            return self._error_response(429, 'rate_limited', 'rate_limited')

        try:
            current_user = self._get_logged_in_user()

            message_values = {
                'name': values['name'],
                'email': values['email'],
                'phone': values['phone'],
                'issue_type': values['issue_type'],
                'subject': values['subject'],
                'message': values['message'],
                'locale': values['locale'],
                'ip_address': ip_address,
            }
            if current_user:
                message_values['user_id'] = current_user.id

            contact_message = request.env['spark.contact.message'].sudo().create(message_values)

            attachment_ids = []
            for attachment in attachments:
                new_attachment = request.env['ir.attachment'].sudo().create({
                    'name': attachment['name'],
                    'datas': base64.b64encode(attachment['data']),
                    'mimetype': attachment['mimetype'],
                    'res_model': 'spark.contact.message',
                    'res_id': contact_message.id,
                })
                attachment_ids.append(new_attachment.id)

            if attachment_ids:
                contact_message.write({'attachment_ids': [(6, 0, attachment_ids)]})

            self._send_confirmation_email(contact_message)

            return self._json_response({'reference': contact_message.reference}, 201)

        except Exception as error:
            _logger.exception(f"Contact form failure: {str(error)}")
            return self._error_response(500, 'server_error', 'server_error')
