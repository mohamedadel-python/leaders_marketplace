import json
from datetime import timedelta
from urllib.parse import quote

from bs4 import BeautifulSoup

from odoo import http, fields
from odoo.http import request

from odoo.addons.leaders_marketplace.tools.auth_tokens import InvalidAccessToken, decode_access_token

from ..tools.html_cleaner import clean_post_html


LOCALE_TO_LANG = {
    'en': 'en_US',
    'ar': 'ar_001',
}

DEFAULT_CORS_ORIGINS = 'http://localhost:3000,https://spark-fu.com'

CACHE_POSTS = 'public, max-age=60, stale-while-revalidate=300'
CACHE_SITEMAP = 'public, max-age=600'
CACHE_COMMENTS = 'private, no-store'
CACHE_AVATAR = 'public, max-age=86400'

ERROR_MESSAGES = {
    'post_not_found': {
        'en': 'Post not found.',
        'ar': 'المقال غير موجود.',
    },
    'comment_not_found': {
        'en': 'Comment not found.',
        'ar': 'التعليق غير موجود.',
    },
    'sign_in_first': {
        'en': 'Please sign in first.',
        'ar': 'يرجى تسجيل الدخول أولاً.',
    },
    'validation_failed': {
        'en': 'Validation failed.',
        'ar': 'البيانات غير صحيحة.',
    },
    'comment_length': {
        'en': 'Comment must be between 2 and 1000 characters.',
        'ar': 'يجب أن يكون التعليق بين 2 و1000 حرف.',
    },
    'reply_to_reply': {
        'en': 'You can not reply to a reply.',
        'ar': 'لا يمكن الرد على رد.',
    },
    'rate_limited': {
        'en': 'You are commenting too fast. Please wait a minute.',
        'ar': 'تعليقاتك كثيرة جداً. انتظر دقيقة ثم حاول مرة أخرى.',
    },
    'forbidden': {
        'en': 'You can only change your own comments.',
        'ar': 'يمكنك تعديل أو حذف تعليقاتك أنت فقط.',
    },
}


class SparkBlogApiController(http.Controller):

    def _get_base_url(self):
        return request.env['ir.config_parameter'].sudo().get_param('web.base.url')

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
            ('Access-Control-Allow-Methods', 'GET, POST, PATCH, DELETE, OPTIONS'),
            ('Access-Control-Allow-Headers', 'Authorization, Content-Type'),
            ('Vary', 'Origin'),
        ]

    def _preflight_response(self):
        return request.make_response('', headers=self._get_cors_headers(), status=204)

    def _get_cache_control(self, cache_control):
        no_cache = request.env['ir.config_parameter'].sudo().get_param('leaders_marketplace.api_no_cache', '0')
        if no_cache.strip() == '1' and cache_control in (CACHE_POSTS, CACHE_SITEMAP, CACHE_AVATAR):
            return 'no-store'
        return cache_control

    def _json_response(self, data, status=200, cache_control=None, extra_headers=None):
        headers = self._get_cors_headers()
        if cache_control:
            headers.append(('Cache-Control', self._get_cache_control(cache_control)))
        if extra_headers:
            headers = headers + extra_headers
        return request.make_json_response(data, headers=headers, status=status)

    def _no_content_response(self):
        return request.make_response('', headers=self._get_cors_headers(), status=204)

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
        return self._json_response(data, status, cache_control='no-store')

    def _image_not_found_response(self):
        headers = self._get_cors_headers()
        headers.append(('Cache-Control', 'no-store'))
        return request.make_response('Not Found', headers=headers, status=404)

    def _get_lang(self):
        locale = request.params.get('locale', 'en')
        if locale in LOCALE_TO_LANG:
            return LOCALE_TO_LANG[locale]
        return LOCALE_TO_LANG['en']

    def _get_int_param(self, name, default_value, max_value):
        raw_value = request.params.get(name)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return default_value
        
        if value < 1:
            return default_value
        if value > max_value:
            return max_value
        return value

    def _read_json_body(self):
        raw_body = request.httprequest.get_data(as_text=True)
        if not raw_body:
            return {}
        
        try:
            data = json.loads(raw_body) # trans JSON to py dict
        except ValueError:
            return {}
        
        if not isinstance(data, dict): # if req body not dict
            return {}
        
        return data

    def _format_date(self, value):
        if not value:
            return None
        return value.strftime('%Y-%m-%dT%H:%M:%S.000Z')

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

    def _get_comment_text(self, body_data):
        comment_text = body_data.get('body')
        if not isinstance(comment_text, str):
            comment_text = ''
        return comment_text.strip()

    def _comment_length_error(self):
        field_errors = {'body': self._get_message('comment_length')}
        return self._error_response(422, 'validation_error', 'validation_failed', field_errors)

    def _get_post_model(self, lang='en_US'):
        return request.env['spark.blog.post'].sudo().with_context(lang=lang)

    def _get_published_domain(self):
        return [
            ('is_published', '=', True),
            ('active', '=', True),
            ('publish_date', '<=', fields.Datetime.now()),
        ]

    def _find_post(self, slug, lang='en_US'):
        domain = self._get_published_domain()
        domain.append(('slug', '=', slug))
        return self._get_post_model(lang).search(domain, limit=1)

    def _find_moved_post(self, old_slug, lang):
        post_model = self._get_post_model(lang)
        history = request.env['spark.blog.slug.history'].sudo().search([('slug', '=', old_slug)], limit=1)
        if not history:
            return post_model.browse()
        domain = self._get_published_domain()
        domain.append(('id', '=', history.post_id.id))
        return post_model.search(domain, limit=1)

    def _moved_response(self, post):
        locale = request.params.get('locale', 'en')
        if locale != 'ar':
            locale = 'en'
        location = '/blog/posts/' + quote(post.slug) + '?locale=' + locale
        data = {'code': 'moved', 'redirectTo': post.slug}
        response = self._json_response(
            data,
            301,
            cache_control=CACHE_POSTS,
            extra_headers=[('Location', location)],
        )
        response.autocorrect_location_header = False
        return response

    def _get_author_data(self, user, base_url):
        data = {'name': user.name}
        if user.image_1920:
            data['avatar'] = f"{base_url}/blog/users/{user.id}/avatar"
        return data

    def _make_absolute_url(self, url, base_url):
        if not url:
            return url
        if url.startswith('//'):
            return url
        if url.startswith('/'):
            return base_url + url
        return url

    def _process_html_content(self, html_content, base_url):
        if not html_content:
            return ''

        soup = BeautifulSoup(clean_post_html(html_content), 'html.parser')

        for image in soup.find_all('img'):
            if image.get('src'):
                image['src'] = self._make_absolute_url(image.get('src'), base_url)

        for link in soup.find_all('a'):
            if link.get('href'):
                link['href'] = self._make_absolute_url(link.get('href'), base_url)

        return str(soup)

    def _get_post_summary(self, post, base_url):
        data = {
            'id': str(post.id),
            'slug': post.slug or '',
            'title': post.title or '',
            'excerpt': post.excerpt or '',
            'publishedAt': self._format_date(post.publish_date),
        }
        if post.image:
            data['image'] = f"{base_url}/web/image/spark.blog.post/{post.id}/image"
        return data

    # def _get_content_locale(self, post):
    #     if post.env.lang != 'ar_001':
    #         return 'en'
    #     english_content = post.with_context(lang='en_US').content
    #     if post.content == english_content:
    #         return 'en'
    #     return 'ar'

    def _get_content_and_locale(self, post):
        if post.env.lang == 'ar_001':
            return post.content, 'ar'
        if post.content_en:
            return post.content_en, 'en'
        return post.content, 'ar'

    def _get_post_details(self, post, base_url):
        data = self._get_post_summary(post, base_url)
        content, content_locale = self._get_content_and_locale(post)
        data['locale'] = content_locale
        data['content'] = self._process_html_content(content, base_url)
        data['author'] = self._get_author_data(post.author_id, base_url)
        data['commentCount'] = post.comment_count
        if post.content_updated_at:
            data['updatedAt'] = self._format_date(post.content_updated_at)
        return data

    def _is_liked_by_user(self, comment, user):
        if not user:
            return False
        like_count = request.env['spark.blog.comment.like'].sudo().search_count([
            ('comment_id', '=', comment.id),
            ('user_id', '=', user.id),
        ])
        if like_count > 0:
            return True
        return False

    def _get_comment_data(self, comment, current_user, base_url, with_replies):
        replies = []
        if with_replies:
            reply_domain = [('parent_id', '=', comment.id), ('state', '=', 'published')]
            reply_records = request.env['spark.blog.comment'].sudo().search(
                reply_domain,
                order='create_date asc, id asc',
            )
            for reply in reply_records:
                replies.append(self._get_comment_data(reply, current_user, base_url, False))

        data = {
            'id': str(comment.id),
            'body': comment.body,
            'createdAt': self._format_date(comment.create_date),
            'likes': len(comment.like_ids),
            'likedByMe': self._is_liked_by_user(comment, current_user),
            'replies': replies,
        }

        if comment.is_deleted:
            data['body'] = ''
            data['deleted'] = True
            return data

        data['author'] = self._get_author_data(comment.user_id, base_url)
        if comment.edited_at:
            data['editedAt'] = self._format_date(comment.edited_at)
        if current_user and comment.user_id.id == current_user.id:
            data['mine'] = True
            data['status'] = comment.state
        return data

    @http.route('/blog/posts', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def get_posts(self, **kwargs):
        """
        List published blog posts with pagination (newest first).

        URL: GET /blog/posts?locale=en&page=1&pageSize=4
        Auth: none

        Cache-Control: public, max-age=60, stale-while-revalidate=300

        Parameters:
        - locale: 'en' or 'ar' (default: 'en'). Any other value falls back to 'en'.
        - page: page number, starts from 1 (default: 1). Bad value falls back to 1.
        - pageSize: posts per page (default: 4, max: 50). Bad value falls back to 4.

        Sort:
        - publish_date descending (newest first).
        - Tie-break on id descending, so the order is stable between pages.

        Rules:
        - Published posts only: no drafts, no scheduled (future date) posts, no archived posts.
        - Featured posts are included in this list too.
        - Page past the end is not an error: returns 200 with items: [] and the real totalCount.
        - Post content is not returned here (cards only).

        Response 200:
        {
            "items": [
                {"id": "1", "slug": "...", "title": "...", "excerpt": "...",
                 "image": "https://...", "publishedAt": "2026-10-12T09:00:00.000Z"}
            ],
            "totalCount": 26,
            "page": 1,
            "pageSize": 4
        }
        - "image" is omitted when the post has no image.
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        base_url = self._get_base_url()
        lang = self._get_lang()
        page = self._get_int_param('page', 1, 100000)
        page_size = self._get_int_param('pageSize', 4, 50)
        offset = (page - 1) * page_size

        post_model = self._get_post_model(lang)
        domain = self._get_published_domain()

        total_count = post_model.search_count(domain)
        posts = post_model.search(
            domain,
            limit=page_size,
            offset=offset,
            order='publish_date desc, id desc',
        )

        items = []
        for post in posts:
            items.append(self._get_post_summary(post, base_url))

        result = {
            'items': items,
            'totalCount': total_count,
            'page': page,
            'pageSize': page_size,
        }
        return self._json_response(result, cache_control=CACHE_POSTS)

    @http.route('/blog/posts/featured', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def get_featured_posts(self, **kwargs):
        """
        List featured posts for the carousel at the top of the blog page.

        URL: GET /blog/posts/featured?locale=en&limit=3
        Auth: none

        Cache-Control: public, max-age=60, stale-while-revalidate=300

        Parameters:
        - locale: 'en' or 'ar' (default: 'en').
        - limit: number of posts (default: 3, max: 50). Bad value falls back to 3.

        Which posts:
        - Only posts marked as Featured (is_featured) by the admin.
        - Published posts only.

        Sort:
        - featured_order ascending (the admin order, smaller number first).
        - Then publish_date descending (newest first) when the order numbers are equal.
        - Then id descending.

        Response 200: a plain array (not paginated).
        [
            {"id": "1", "slug": "...", "title": "...", "excerpt": "...",
             "image": "https://...", "publishedAt": "2026-10-12T09:00:00.000Z"}
        ]
        - No featured posts: returns [].
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        base_url = self._get_base_url()
        lang = self._get_lang()
        limit = self._get_int_param('limit', 3, 50)

        domain = self._get_published_domain()
        domain.append(('is_featured', '=', True))

        posts = self._get_post_model(lang).search(
            domain,
            limit=limit,
            order='featured_order asc, publish_date desc, id desc',
        )

        result = []
        for post in posts:
            result.append(self._get_post_summary(post, base_url))
        return self._json_response(result, cache_control=CACHE_POSTS)

    @http.route('/blog/posts/<string:slug>', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def get_post_details(self, slug, **kwargs):
        """
        Get one published post by its slug (the post details page).

        URL: GET /blog/posts/<slug>?locale=en
        Auth: none

        Cache-Control: public, max-age=60, stale-while-revalidate=300

        Parameters:
        - slug (path): the post slug. One slug for all languages.
        - locale: 'en' or 'ar' (default: 'en'). Untranslated fields fall back to English.

        Rules:
        - Published posts only.
        - Every call adds 1 to the post visits counter.
        - The content in the response is cleaned: no editor wrapper, only the allowed tags
          and attributes, loose text inside <p>, h1 becomes h2 and h5/h6 become h4.
          The same cleaning also runs when the post is saved in Odoo.
        - Links and images inside the content become absolute URLs.
        - commentCount counts every visible comment, replies included. Draft, hidden
          and deleted comments are not counted.
        - locale is the language of the article content that is returned. ?locale=ar
          returns the Arabic content ('ar'). ?locale=en returns the English content
          ('en'), or the Arabic content with locale 'ar' when there is no English
          content yet.

        Response 200:
        {
            "id": "1", "slug": "...", "title": "...", "excerpt": "...",
            "image": "https://...", "publishedAt": "2026-10-12T09:00:00.000Z",
            "updatedAt": "2026-10-20T14:30:00.000Z",
            "locale": "en",
            "content": "<p>...</p><h2>...</h2>",
            "author": {"name": "...", "avatar": "https://..."},
            "commentCount": 16
        }
        - "image" is omitted when the post has no image.
        - "updatedAt" is omitted when the content was never edited.
        - "author.avatar" is omitted when the author has no picture.

        Response 301: the slug is an old slug of a published post that was renamed.
        Location: /blog/posts/<newSlug>?locale=<en or ar>
        {"code": "moved", "redirectTo": "<newSlug>"}

        Response 404: unknown, unpublished, scheduled or archived slug.
        {"code": "not_found", "message": "Post not found."}
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        base_url = self._get_base_url()
        lang = self._get_lang()

        post = self._find_post(slug, lang)
        if not post:
            moved_post = self._find_moved_post(slug, lang)
            if moved_post:
                return self._moved_response(moved_post)
            return self._error_response(404, 'not_found', 'post_not_found')

        post.increment_visits()

        result = self._get_post_details(post, base_url)
        return self._json_response(result, cache_control=CACHE_POSTS)

    @http.route('/blog/posts/<string:slug>/related', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def get_related_posts(self, slug, **kwargs):
        """
        List related posts for the "Related articles" carousel under a post.

        URL: GET /blog/posts/<slug>/related?locale=en&limit=3
        Auth: none

        Cache-Control: public, max-age=60, stale-while-revalidate=300

        Parameters:
        - slug (path): the post slug.
        - locale: 'en' or 'ar' (default: 'en').
        - limit: number of posts (default: 3, max: 50). Bad value falls back to 3.

        How posts are picked:
        1. Published posts that share at least one tag with this post, newest first.
        2. If there are not enough, fill up to the limit with the newest other published posts.
        - The post itself is never included.

        Response 200: a plain array (not paginated).
        [
            {"id": "2", "slug": "...", "title": "...", "excerpt": "...",
             "image": "https://...", "publishedAt": "2026-10-12T09:00:00.000Z"}
        ]
        - Unknown slug: returns 200 with [] (not 404).
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        base_url = self._get_base_url()
        lang = self._get_lang()
        limit = self._get_int_param('limit', 3, 50)

        post = self._find_post(slug, lang)
        if not post:
            return self._json_response([], cache_control=CACHE_POSTS)

        post_model = self._get_post_model(lang)
        related_posts = post_model.browse() # [] - recordest

        if post.tag_ids:
            tag_domain = self._get_published_domain()
            tag_domain.append(('id', '!=', post.id))
            tag_domain.append(('tag_ids', 'in', post.tag_ids.ids))
            related_posts = post_model.search(
                tag_domain,
                limit=limit,
                order='publish_date desc, id desc',
            )

        missing_count = limit - len(related_posts)
        if missing_count > 0:
            excluded_ids = [post.id]
            for related_post in related_posts:
                excluded_ids.append(related_post.id)

            other_domain = self._get_published_domain()
            other_domain.append(('id', 'not in', excluded_ids))
            other_posts = post_model.search(
                other_domain,
                limit=missing_count,
                order='publish_date desc, id desc',
            )
            related_posts = related_posts + other_posts

        result = []
        for related_post in related_posts:
            result.append(self._get_post_summary(related_post, base_url))
        return self._json_response(result, cache_control=CACHE_POSTS)

    @http.route('/blog/sitemap', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def get_sitemap(self, **kwargs):
        """
        List every published post for sitemap.xml.

        URL: GET /blog/sitemap
        Auth: none

        Cache-Control: public, max-age=600

        Parameters: none (no locale, the slug is the same in every language).

        Rules:
        - All published posts in one response (no pagination).
        - No drafts, no scheduled posts, no archived posts.
        - updatedAt = last content edit, or publish date if the post was never edited.
          It is never omitted.

        Response 200:
        [
            {"slug": "generator-sizing-guide", "updatedAt": "2026-10-20T14:30:00.000Z"}
        ]
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        posts = self._get_post_model().search(
            self._get_published_domain(),
            order='publish_date desc, id desc',
        )

        result = []
        for post in posts:
            updated_at = post.content_updated_at
            if not updated_at:
                updated_at = post.publish_date

            result.append({
                'slug': post.slug or '',
                'updatedAt': self._format_date(updated_at),
            })
        return self._json_response(result, cache_control=CACHE_SITEMAP)

    @http.route('/blog/users/<int:user_id>/avatar', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def get_user_avatar(self, user_id, **kwargs):
        """
        Serve a user picture publicly (used for post authors and comment authors).

        URL: GET /blog/users/<user_id>/avatar
        Auth: none

        Parameters:
        - user_id (path): the user id.

        Response 200: the image itself (128x128), not JSON.
        Response 404: unknown user.
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        user = request.env['res.users'].sudo().browse(user_id)
        if not user.exists():
            return self._image_not_found_response()

        image_stream = request.env['ir.binary']._get_image_stream_from(user, 'avatar_128')
        response = image_stream.get_response()
        response.headers['Cache-Control'] = self._get_cache_control(CACHE_AVATAR)
        for header_name, header_value in self._get_cors_headers():
            response.headers[header_name] = header_value
        return response

    @http.route('/blog/posts/<string:slug>/comments', type='http', auth='public', methods=['GET', 'POST'], csrf=False, save_session=False)
    def comments(self, slug, **kwargs):
        """
        One route for the comments path, so the browser preflight (OPTIONS)
        gets "GET, POST" in Access-Control-Allow-Methods.

        - GET: list the comments (see _get_comments).
        - POST: add a comment or a reply (see _add_comment).
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        if request.httprequest.method == 'POST':
            return self._add_comment(slug)
        return self._get_comments(slug)

    def _get_comments(self, slug):
        """
        List the top-level comments of a post, each one with its replies.

        URL: GET /blog/posts/<slug>/comments?page=1&pageSize=5&sort=newest
        Auth: optional. Header: Authorization: Bearer <accessToken>
        - With a valid token: likedByMe is filled for that user.
        - Without a token, or with a wrong/expired token: likedByMe is false everywhere.
          Never returns 401.

        Cache-Control: private, no-store

        Parameters:
        - slug (path): the post slug.
        - page: page number, starts from 1 (default: 1). Bad value falls back to 1.
        - pageSize: comments per page (default: 5, max: 50). Bad value falls back to 5.
        - sort: 'newest' or 'oldest' (default: 'newest'). Any other value falls back to 'newest'.

        Rules:
        - Pagination and sort apply to top-level comments only.
        - Replies are returned in full (not paginated) and always oldest first.
        - Replies are one level deep: a reply always has replies: [].
        - totalCount counts the top-level threads of the list, including a deleted comment
          that was kept because it has replies.
        - Draft and hidden comments are not returned and not counted.
        - A deleted top-level comment that has replies is returned as
          {"body": "", "deleted": true} without "author".
        - Comments written by the token user have "mine": true and "status".
        - An edited comment has "editedAt".

        Response 200:
        {
            "items": [
                {
                    "id": "101",
                    "author": {"name": "...", "avatar": "https://..."},
                    "body": "...",
                    "createdAt": "2026-10-28T20:15:00.000Z",
                    "likes": 3,
                    "likedByMe": false,
                    "replies": [
                        {"id": "102", "author": {"name": "..."}, "body": "...",
                         "createdAt": "...", "likes": 2, "likedByMe": true, "replies": []}
                    ]
                }
            ],
            "totalCount": 16,
            "page": 1,
            "pageSize": 5
        }

        Response 404: unknown post.
        {"code": "not_found", "message": "Post not found."}
        """
        base_url = self._get_base_url()
        current_user = self._get_logged_in_user()
        page = self._get_int_param('page', 1, 100000)
        page_size = self._get_int_param('pageSize', 5, 50)
        offset = (page - 1) * page_size

        sort = request.params.get('sort', 'newest')
        if sort == 'oldest':
            order = 'create_date asc, id asc'
        else:
            order = 'create_date desc, id desc'

        post = self._find_post(slug)
        if not post:
            return self._error_response(404, 'not_found', 'post_not_found')

        comment_model = request.env['spark.blog.comment'].sudo()
        domain = [
            ('post_id', '=', post.id),
            ('parent_id', '=', False),
            ('state', '=', 'published'),
        ]

        total_count = comment_model.search_count(domain)
        comments = comment_model.search(domain, limit=page_size, offset=offset, order=order)

        items = []
        for comment in comments:
            items.append(self._get_comment_data(comment, current_user, base_url, True))

        result = {
            'items': items,
            'totalCount': total_count,
            'page': page,
            'pageSize': page_size,
        }
        return self._json_response(result, cache_control=CACHE_COMMENTS)

    def _add_comment(self, slug):
        """
        Add a new comment, or a reply to a comment.

        URL: POST /blog/posts/<slug>/comments
        Auth: required. Header: Authorization: Bearer <accessToken>

        Request body (JSON):
        {
            "body": "Clear and practical.",
            "parentId": "101"
        }
        - body: required. Plain text. Trimmed, then must be 2 to 1000 characters.
        - parentId: optional. Omit it for a new top-level comment.
          When sent, it must be a top-level comment on this same post.
        - Any other field (author, likes, createdAt, ...) is ignored.
          The author is always the user from the token.

        Rules:
        - Rate limit: a user can add at most 5 comments per minute.
        - Every comment is published at once.
        - A draft, hidden or deleted comment cannot be replied to (404 not_found).

        Response 201: the created comment, with "mine": true and "status": "published".
        {
            "id": "117",
            "author": {"name": "...", "avatar": "https://..."},
            "body": "Clear and practical.",
            "createdAt": "2026-10-29T10:02:11.000Z",
            "likes": 0,
            "likedByMe": false,
            "replies": [],
            "mine": true,
            "status": "published"
        }

        Errors:
        - 401 session_expired: missing, wrong or expired token.
        - 404 not_found: post not found, or parentId not found on this post, or the parent
          comment is draft, hidden or deleted.
        - 422 validation_error: body too short or too long. Adds "fields": {"body": "..."}.
        - 422 reply_to_reply: parentId is a reply (no reply to a reply).
        - 429 rate_limited: more than 5 comments in the last minute.
        Every error body: {"code": "...", "message": "..."}
        The message language follows the Accept-Language header (ar or en, default en).
        """
        base_url = self._get_base_url()

        current_user = self._get_logged_in_user()
        if not current_user:
            return self._error_response(401, 'session_expired', 'sign_in_first')

        post = self._find_post(slug)
        if not post:
            return self._error_response(404, 'not_found', 'post_not_found')

        body_data = self._read_json_body()

        comment_text = self._get_comment_text(body_data)

        if len(comment_text) < 2 or len(comment_text) > 1000:
            return self._comment_length_error()

        comment_model = request.env['spark.blog.comment'].sudo()

        parent_comment = comment_model.browse()
        parent_id_value = body_data.get('parentId')
        if parent_id_value:
            try:
                parent_id = int(parent_id_value)
            except (TypeError, ValueError):
                return self._error_response(404, 'not_found', 'comment_not_found')

            parent_comment = comment_model.search([
                ('id', '=', parent_id),
                ('post_id', '=', post.id),
                ('state', '=', 'published'),
                ('is_deleted', '=', False),
            ], limit=1)
            if not parent_comment:
                return self._error_response(404, 'not_found', 'comment_not_found')
            if parent_comment.parent_id:
                return self._error_response(422, 'reply_to_reply', 'reply_to_reply')

        one_minute_ago = fields.Datetime.now() - timedelta(minutes=1) # 10:05:30 - 1Min = 10:04:30
        recent_comments_count = comment_model.search_count([
            ('user_id', '=', current_user.id),
            ('create_date', '>=', one_minute_ago),
        ])
        if recent_comments_count >= 5:
            return self._error_response(429, 'rate_limited', 'rate_limited')

        new_comment = comment_model.create({
            'post_id': post.id,
            'user_id': current_user.id,
            'body': comment_text,
            'parent_id': parent_comment.id,
            'state': 'published',
        })

        result = self._get_comment_data(new_comment, current_user, base_url, False)
        return self._json_response(result, 201)


    @http.route('/blog/posts/<string:slug>/comments/<int:comment_id>/like', type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def toggle_comment_like(self, slug, comment_id, **kwargs):
        """
        Like or unlike a comment (toggle). Works on comments and replies.

        URL: POST /blog/posts/<slug>/comments/<comment_id>/like
        Auth: required. Header: Authorization: Bearer <accessToken>
        Body: none

        Rules:
        - If the user has not liked the comment yet: add the like.
        - If the user already liked it: remove the like.
        - One like per user per comment, enforced in the database (unique comment + user).

        Response 200: the real new state.
        {"likes": 4, "likedByMe": true}

        Errors:
        - 401 session_expired: missing, wrong or expired token.
        - 404 not_found: post not found, or the comment does not belong to this post,
          or the comment is draft, hidden or deleted.
        Every error body: {"code": "...", "message": "..."}
        The message language follows the Accept-Language header (ar or en, default en).
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        current_user = self._get_logged_in_user()
        if not current_user:
            return self._error_response(401, 'session_expired', 'sign_in_first')

        post = self._find_post(slug)
        if not post:
            return self._error_response(404, 'not_found', 'post_not_found')

        comment = request.env['spark.blog.comment'].sudo().search([
            ('id', '=', comment_id),
            ('post_id', '=', post.id),
            ('state', '=', 'published'),
            ('is_deleted', '=', False),
        ], limit=1)
        if not comment:
            return self._error_response(404, 'not_found', 'comment_not_found')

        like_model = request.env['spark.blog.comment.like'].sudo()
        existing_like = like_model.search([
            ('comment_id', '=', comment.id),
            ('user_id', '=', current_user.id),
        ], limit=1)

        if existing_like:
            existing_like.unlink()
            liked_by_me = False
        else:
            like_model.create({
                'comment_id': comment.id,
                'user_id': current_user.id,
            })
            liked_by_me = True

        result = {
            'likes': len(comment.like_ids),
            'likedByMe': liked_by_me,
        }
        return self._json_response(result)

    @http.route('/blog/posts/<string:slug>/comments/<int:comment_id>', type='http', auth='public', methods=['PATCH', 'DELETE'], csrf=False, save_session=False)
    def comment_detail(self, slug, comment_id, **kwargs):
        """
        One route for the path of a single comment, so the browser preflight (OPTIONS)
        gets "PATCH, DELETE" in Access-Control-Allow-Methods.

        - PATCH: edit your own comment (see _edit_comment).
        - DELETE: delete your own comment (see _delete_comment).
        """
        if request.httprequest.method == 'OPTIONS':
            return self._preflight_response()

        if request.httprequest.method == 'DELETE':
            return self._delete_comment(slug, comment_id)
        return self._edit_comment(slug, comment_id)

    def _edit_comment(self, slug, comment_id):
        """
        Edit your own comment or reply.

        URL: PATCH /blog/posts/<slug>/comments/<comment_id>
        Auth: required. Header: Authorization: Bearer <accessToken>

        Request body (JSON):
        {"body": "Corrected text"}
        - body: required. Plain text. Trimmed, then must be 2 to 1000 characters.

        Rules:
        - The author is the only one who can edit.
        - The comment gets "editedAt".
        - An edited comment stays published.

        Response 200: the updated comment, with "mine": true, "status" and "editedAt".

        Errors:
        - 401 session_expired: missing, wrong or expired token.
        - 403 forbidden: the comment is not yours.
        - 404 not_found: post not found, or no such comment on this post. Draft, hidden
          and deleted comments count as not found.
        - 422 validation_error: body too short or too long. Adds "fields": {"body": "..."}.
        Every error body: {"code": "...", "message": "..."}
        The message language follows the Accept-Language header (ar or en, default en).
        """
        base_url = self._get_base_url()

        current_user = self._get_logged_in_user()
        if not current_user:
            return self._error_response(401, 'session_expired', 'sign_in_first')

        post = self._find_post(slug)
        if not post:
            return self._error_response(404, 'not_found', 'post_not_found')

        comment_domain = [
            ('id', '=', comment_id),
            ('post_id', '=', post.id),
            ('is_deleted', '=', False),
            ('state', '=', 'published'),
        ]
        comment = request.env['spark.blog.comment'].sudo().search(comment_domain, limit=1)
        if not comment:
            return self._error_response(404, 'not_found', 'comment_not_found')

        if comment.user_id.id != current_user.id:
            return self._error_response(403, 'forbidden', 'forbidden')

        body_data = self._read_json_body()
        comment_text = self._get_comment_text(body_data)
        if len(comment_text) < 2 or len(comment_text) > 1000:
            return self._comment_length_error()

        new_values = {
            'body': comment_text,
            'edited_at': fields.Datetime.now(),
        }
        
        comment.write(new_values)

        result = self._get_comment_data(comment, current_user, base_url, True)
        return self._json_response(result)

    def _delete_comment(self, slug, comment_id):
        """
        Delete your own comment or reply.

        URL: DELETE /blog/posts/<slug>/comments/<comment_id>
        Auth: required. Header: Authorization: Bearer <accessToken>
        Body: none

        Rules:
        - A reply is removed.
        - A top-level comment without visible replies is removed.
        - A top-level comment that has visible replies is kept for its replies, as
          {"body": "", "deleted": true} without "author". Its likes are removed.
        - When the last reply of such a kept comment is deleted, the kept comment is removed too.
        - Deleting twice, or deleting a comment that does not exist, is 204 too.
        - Comments of other users that are not published look like missing comments (204).

        Response 204: no body.

        Errors:
        - 401 session_expired: missing, wrong or expired token.
        - 403 forbidden: the comment is a published comment of another user.
        - 404 not_found: post not found.
        Every error body: {"code": "...", "message": "..."}
        The message language follows the Accept-Language header (ar or en, default en).
        """
        current_user = self._get_logged_in_user()
        if not current_user:
            return self._error_response(401, 'session_expired', 'sign_in_first')

        post = self._find_post(slug)
        if not post:
            return self._error_response(404, 'not_found', 'post_not_found')

        comment_model = request.env['spark.blog.comment'].sudo().with_context(active_test=False)
        comment = comment_model.search([
            ('id', '=', comment_id),
            ('post_id', '=', post.id),
        ], limit=1)
        if not comment:
            return self._no_content_response()

        if comment.user_id.id != current_user.id:
            is_public_comment = comment.active and comment.state == 'published' and not comment.is_deleted
            if is_public_comment:
                return self._error_response(403, 'forbidden', 'forbidden')
            return self._no_content_response()

        if comment.is_deleted:
            return self._no_content_response()

        visible_reply_count = 0
        if not comment.parent_id:
            visible_reply_count = comment_model.search_count([
                ('parent_id', '=', comment.id),
                ('state', '=', 'published'),
                ('active', '=', True),
            ])

        if visible_reply_count > 0:
            comment.like_ids.unlink()
            comment.write({'body': '', 'is_deleted': True})
            return self._no_content_response()

        parent_comment = comment.parent_id
        comment.unlink()

        if parent_comment and parent_comment.is_deleted:
            remaining_reply_count = comment_model.search_count([
                ('parent_id', '=', parent_comment.id),
                ('state', '=', 'published'),
                ('active', '=', True),
            ])
            if remaining_reply_count == 0:
                parent_comment.unlink()

        return self._no_content_response()
