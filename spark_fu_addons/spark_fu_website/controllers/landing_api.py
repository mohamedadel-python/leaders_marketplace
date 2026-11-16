import logging

from odoo import http
from odoo.http import request

from odoo.addons.leaders_marketplace.controllers.storefront_common import rate_limit
from odoo.addons.leaders_marketplace.tools.auth_errors import AuthApiError

from ..models.res_config_settings import to_video_asset

_logger = logging.getLogger(__name__)

DEFAULT_CORS_ORIGINS = 'http://localhost:3000,https://spark-fu.com'

CACHE_LANDING = 'public, max-age=300'

ERROR_MESSAGES = {
    'validation_error': {
        'en': 'Validation failed.',
        'ar': 'البيانات غير صحيحة.',
    },
    'invalid_limit': {
        'en': 'Limit must be a number from 1 to 12.',
        'ar': 'يجب أن يكون الحد رقماً من 1 إلى 12.',
    },
    'rate_limited': {
        'en': 'Too many requests. Wait a moment.',
        'ar': 'طلبات كثيرة جداً. انتظر قليلاً.',
    },
    'server_error': {
        'en': 'Something went wrong. Try again.',
        'ar': 'حدث خطأ ما. حاول مرة أخرى.',
    },
}


class SparkLandingApiController(http.Controller):

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
            return [('Vary', 'Origin')]

        return [
            ('Access-Control-Allow-Origin', request_origin),
            ('Access-Control-Allow-Credentials', 'true'),
            ('Access-Control-Allow-Methods', 'GET, OPTIONS'),
            ('Access-Control-Allow-Headers', 'Authorization, Content-Type'),
            ('Vary', 'Origin'),
        ]

    def _preflight_response(self):
        return request.make_response('', headers=self._get_cors_headers(), status=204)

    def _get_cache_control(self):
        no_cache = request.env['ir.config_parameter'].sudo().get_param('leaders_marketplace.api_no_cache', '0')
        if no_cache.strip() == '1':
            return 'no-store'
        return CACHE_LANDING

    def _json_response(self, data):
        headers = self._get_cors_headers()
        headers.append(('Cache-Control', self._get_cache_control()))
        return request.make_json_response(data, headers=headers, status=200)

    def _is_arabic_request(self):
        accept_language = request.httprequest.headers.get('Accept-Language', '')
        first_language = accept_language.split(',')[0].strip().lower()
        return first_language.startswith('ar')

    def _get_message(self, message_key):
        if self._is_arabic_request():
            return ERROR_MESSAGES[message_key]['ar']
        return ERROR_MESSAGES[message_key]['en']

    def _error_response(self, status, code, field_errors=None):
        data = {
            'code': code,
            'message': self._get_message(code),
        }
        if field_errors:
            data['fields'] = field_errors
        headers = self._get_cors_headers()
        headers.append(('Cache-Control', 'no-store'))
        return request.make_json_response(data, headers=headers, status=status)

    def _image_not_found_response(self):
        headers = self._get_cors_headers()
        headers.append(('Cache-Control', 'no-store'))
        return request.make_response('Not Found', headers=headers, status=404)

    def _get_company_story_video_url(self):
        config = request.env['ir.config_parameter'].sudo()
        arabic_url = config.get_param('spark_fu.company_story_video_url_ar')
        english_url = config.get_param('spark_fu.company_story_video_url_en')

        if self._is_arabic_request():
            return arabic_url
        if english_url:
            return english_url
        return arabic_url

    def _get_company_story_poster_attachment(self):
        attachment_id = request.env['ir.config_parameter'].sudo().get_param(
            'spark_fu.company_story_poster_attachment_id',
        )
        if not attachment_id:
            return request.env['ir.attachment']
        return request.env['ir.attachment'].sudo().browse(int(attachment_id)).exists()

    def _get_testimonials_limit(self):
        limit_text = request.params.get('limit')
        if limit_text is None:
            return 4
        if not limit_text.isdigit():
            return False

        limit = int(limit_text)
        if limit < 1 or limit > 12:
            return False
        return limit

    def _get_testimonial_data(self, testimonial):
        base_url = request.env['ir.config_parameter'].sudo().get_param('web.base.url')
        data = {
            'id': str(testimonial.id),
            'name': testimonial.name,
            'role': testimonial.role,
            'quote': testimonial.quote,
            'image': base_url + '/testimonials/' + str(testimonial.id) + '/image',
        }
        if testimonial.company:
            data['company'] = testimonial.company
        video = to_video_asset(testimonial.video_url)
        if video:
            data['video'] = video
        return data

    @http.route('/company-story', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def get_company_story(self, **kwargs):
        """
        The company film of the landing page.

        URL: GET /company-story
        Auth: none
        Header: Accept-Language chooses the video (ar or en, default en).

        Cache-Control: no-store while leaders_marketplace.api_no_cache is 1, else public, max-age=300

        Rules:
        - The video of the requested language is returned. An English request with
          no English video gets the Arabic video.
        - "video" is omitted when there is no video.
        - "poster" is omitted when no picture was uploaded.

        Response 200:
        {
            "video": {"kind": "youtube" | "drive", "url": "<embed link>"},
            "poster": "https://odooapp.mdarj.org/company-story/poster"
        }

        Errors, body is always {"code": "...", "message": "..."}:
        - 429 rate_limited
        - 500 server_error
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        try:
            rate_limit('sf_company_story')
        except AuthApiError:
            return self._error_response(429, 'rate_limited')

        try:
            result = {}

            video = to_video_asset(self._get_company_story_video_url())
            if video:
                result['video'] = video

            attachment = self._get_company_story_poster_attachment()
            if attachment:
                base_url = request.env['ir.config_parameter'].sudo().get_param('web.base.url')
                result['poster'] = base_url + '/company-story/poster'

            return self._json_response(result)

        except Exception as error:
            _logger.exception(f"Company story failure: {str(error)}")
            return self._error_response(500, 'server_error')

    @http.route('/company-story/poster', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def get_company_story_poster(self, **kwargs):
        """
        Serve the company story poster publicly.

        URL: GET /company-story/poster
        Auth: none

        Response 200: the image itself, not JSON.
        Response 404: no picture was uploaded.
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        attachment = self._get_company_story_poster_attachment()
        if not attachment:
            return self._image_not_found_response()

        image_stream = request.env['ir.binary']._get_stream_from(attachment)
        response = image_stream.get_response()

        response.headers['Cache-Control'] = self._get_cache_control()
        for header_name, header_value in self._get_cors_headers():
            response.headers[header_name] = header_value
        return response

    @http.route('/testimonials', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def get_testimonials(self, **kwargs):
        """
        The published customer testimonials of the landing page.

        URL: GET /testimonials?limit=4
        Auth: none
        Header: Accept-Language chooses the language of the text (ar or en, default en).

        Cache-Control: no-store while leaders_marketplace.api_no_cache is 1, else public, max-age=300

        Parameters:
        - limit: how many testimonials to return, 1 to 12 (default: 4).

        Rules:
        - Only published testimonials, in the order the admin sets.
        - A missing translation falls back to the source text.
        - "company" and "video" are omitted when empty.
        - No testimonials: 200 with [].

        Response 200:
        [
            {
                "id": "12",
                "name": "...",
                "role": "...",
                "company": "...",
                "quote": "...",
                "image": "https://odooapp.mdarj.org/testimonials/12/image",
                "video": {"kind": "youtube", "url": "https://www.youtube-nocookie.com/embed/VIDEO_ID"}
            }
        ]

        Errors, body is always {"code": "...", "message": "..."}:
        - 422 validation_error: a bad limit. Adds "fields": {"limit": "..."}.
        - 429 rate_limited
        - 500 server_error
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        try:
            rate_limit('sf_testimonials')
        except AuthApiError:
            return self._error_response(429, 'rate_limited')

        limit = self._get_testimonials_limit()
        if not limit:
            field_errors = {'limit': self._get_message('invalid_limit')}
            return self._error_response(422, 'validation_error', field_errors)

        try:
            lang = 'en_US'
            if self._is_arabic_request():
                lang = 'ar_001'

            testimonials = request.env['spark.testimonial'].sudo().with_context(lang=lang).search(
                [('state', '=', 'published')],
                limit=limit,
            )

            result = []
            for testimonial in testimonials:
                result.append(self._get_testimonial_data(testimonial))

            return self._json_response(result)

        except Exception as error:
            _logger.exception(f"Testimonials failure: {str(error)}")
            return self._error_response(500, 'server_error')

    @http.route('/testimonials/<int:testimonial_id>/image', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def get_testimonial_image(self, testimonial_id, **kwargs):
        """
        Serve the picture of a published testimonial publicly.

        URL: GET /testimonials/<testimonial_id>/image
        Auth: none

        Response 200: the image itself, not JSON.
        Response 404: unknown or unpublished testimonial, or no picture.
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        testimonial = request.env['spark.testimonial'].sudo().search([
            ('id', '=', testimonial_id),
            ('state', '=', 'published'),
        ])
        if not testimonial or not testimonial.image:
            return self._image_not_found_response()

        image_stream = request.env['ir.binary']._get_image_stream_from(testimonial, 'image') # get image from field
        response = image_stream.get_response() # photo

        response.headers['Cache-Control'] = self._get_cache_control()
        for header_name, header_value in self._get_cors_headers():
            response.headers[header_name] = header_value
        return response
