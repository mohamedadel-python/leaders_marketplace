import base64
import logging
import re
from urllib.parse import parse_qs, urlparse

import requests
from PIL import WebPImagePlugin

from odoo import api, models, fields
from odoo.exceptions import UserError
from odoo.tools.image import binary_to_image
from odoo.tools.mimetypes import guess_mimetype

_logger = logging.getLogger(__name__)

VIDEO_LINK_ERROR = 'Paste a YouTube or Google Drive video link'
DRIVE_HELP = (
    'A Google Drive video plays on the site only when: the file is shared as "Anyone with the link" (Viewer); '
    'it is a video file (MP4 recommended), not a folder, document or shortcut; and Drive has finished processing it. '
    'Google limits: a file watched a lot can be blocked for a while ("too many views"), so YouTube (unlisted is fine) '
    'is the safer choice for a busy page; the visitor presses play twice (on our card, then in Drive\'s player); '
    'a file larger than about 100 MB may not get a streamable preview.'
)

POSTER_PARAMETER = 'spark_fu.company_story_poster_attachment_id'
POSTER_ALLOWED_TYPES = ['image/jpeg', 'image/webp']
POSTER_MAX_SIZE = 500 * 1024
POSTER_MIN_WIDTH = 1560
POSTER_MIN_HEIGHT = 872

YT_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
DRIVE_ID = re.compile(r'^[A-Za-z0-9_-]{10,}$')
YT_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be',
            'www.youtube-nocookie.com', 'youtube-nocookie.com'}
DRIVE_HOSTS = {'drive.google.com', 'docs.google.com'}
YOUTUBE_EMBED = 'https://www.youtube-nocookie.com/embed/'
DRIVE_EMBED = 'https://drive.google.com/file/d/%s/preview'


def to_video_asset(link):
    """{"kind": "youtube" | "drive", "url": <embed form>} for a YouTube or Google Drive video link, else None.

    YouTube: watch?v=, youtu.be/, /shorts/, /live/, /embed/, youtube-nocookie. Drive: /file/d/<id>/...,
    open?id=, uc?id=. A Drive folder, Vimeo, Facebook or any other site is None.
    """
    if not link:
        return None
    text = link.strip()
    if '://' not in text:
        text = 'https://' + text
    parts = urlparse(text)
    host = (parts.hostname or '').lower()
    if host in YT_HOSTS:
        if host == 'youtu.be':
            video_id = parts.path.strip('/').split('/')[0]
        else:
            video_id = parse_qs(parts.query).get('v', [None])[0]
            segments = parts.path.split('/')
            if not video_id and len(segments) > 2 and segments[1] in ('embed', 'shorts', 'live', 'v'):
                video_id = segments[2]
        if video_id and YT_ID.match(video_id):
            return {'kind': 'youtube', 'url': YOUTUBE_EMBED + video_id}
    if host in DRIVE_HOSTS and '/folders/' not in parts.path:
        match = re.search(r'/d/([^/]+)', parts.path)
        file_id = match.group(1) if match else parse_qs(parts.query).get('id', [None])[0]
        if file_id and DRIVE_ID.match(file_id):
            return {'kind': 'drive', 'url': DRIVE_EMBED % file_id}
    return None


def get_video_embed_link(link):
    """The embed link to store for a pasted YouTube or Google Drive link; anything else is refused."""
    if not link:
        return False
    asset = to_video_asset(link)
    if not asset:
        raise UserError(VIDEO_LINK_ERROR)
    return asset['url']


def drive_sharing_warning(link):
    """Optional check of a Drive link: the preview is asked without a Google session. A sign-in page, an
    "access" page or a 404 means the file is not shared publicly: a warning text is returned, else False.
    Any network trouble returns False (never blocks the admin)."""
    asset = to_video_asset(link)
    if not asset or asset['kind'] != 'drive':
        return False
    try:
        response = requests.get(asset['url'], timeout=6, allow_redirects=True,
                                headers={'User-Agent': 'Mozilla/5.0'})
    except requests.RequestException:
        _logger.info('Drive sharing check skipped: Google could not be reached')
        return False
    private = (response.status_code in (401, 403, 404) or 'accounts.google.com' in response.url
               or 'ServiceLogin' in response.text[:20000] or 'You need access' in response.text[:20000])
    if private:
        return ('This Google Drive file does not look shared publicly, so it will not play on the site. '
                'In Drive, set sharing to "Anyone with the link" (Viewer), then save again.')
    return False


# kept for older callers: it now accepts Google Drive links too
get_youtube_embed_link = get_video_embed_link


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    company_story_video_url_ar = fields.Char(
        string='Arabic Video Link',
        config_parameter='spark_fu.company_story_video_url_ar',
    )
    company_story_video_url_en = fields.Char(
        string='English Video Link',
        config_parameter='spark_fu.company_story_video_url_en',
    )
    company_story_poster = fields.Binary(string='Poster', attachment=False)

    @api.onchange('company_story_video_url_ar', 'company_story_video_url_en')
    def _onchange_company_story_video(self):
        for link in (self.company_story_video_url_ar, self.company_story_video_url_en):
            if link and not to_video_asset(link):
                return {'warning': {'title': 'Video link', 'message': VIDEO_LINK_ERROR}}
            message = drive_sharing_warning(link)
            if message:
                return {'warning': {'title': 'Google Drive sharing', 'message': message}}

    def _get_company_story_poster_attachment(self):
        attachment_id = self.env['ir.config_parameter'].sudo().get_param(POSTER_PARAMETER)
        if not attachment_id:
            return self.env['ir.attachment']
        attachment = self.env['ir.attachment'].sudo().browse(int(attachment_id)).exists()
        return attachment.with_context(bin_size=False)

    def _check_company_story_poster(self, poster_data):
        if guess_mimetype(poster_data) not in POSTER_ALLOWED_TYPES:
            raise UserError('The poster must be a JPG or WebP image.')

        if len(poster_data) > POSTER_MAX_SIZE:
            raise UserError('The poster must be under 500 KB.')

        width, height = binary_to_image(poster_data).size
        if width < POSTER_MIN_WIDTH or height < POSTER_MIN_HEIGHT:
            raise UserError('The poster must be at least 1560 x 872 px.')

    def _save_company_story_poster(self):
        poster = self.with_context(bin_size=False).company_story_poster
        attachment = self._get_company_story_poster_attachment()

        if not poster:
            if attachment:
                attachment.unlink()
                self.env['ir.config_parameter'].sudo().set_param(POSTER_PARAMETER, False)
            return

        poster_data = base64.b64decode(poster)
        if attachment and attachment.raw == poster_data:
            return

        self._check_company_story_poster(poster_data)

        values = {
            'name': 'company_story_poster',
            'raw': poster_data,
            'mimetype': guess_mimetype(poster_data),
            'public': True,
        }
        attachment_model = self.env['ir.attachment'].sudo().with_context(image_no_postprocess=True)
        if attachment:
            attachment_model.browse(attachment.id).write(values)
        else:
            new_attachment = attachment_model.create(values)
            self.env['ir.config_parameter'].sudo().set_param(POSTER_PARAMETER, new_attachment.id)

    @api.model
    def get_values(self):
        values = super().get_values()
        attachment = self._get_company_story_poster_attachment()
        if attachment:
            values['company_story_poster'] = attachment.datas
        return values

    def set_values(self):
        self._save_company_story_poster()
        self.company_story_video_url_ar = get_video_embed_link(self.company_story_video_url_ar)
        self.company_story_video_url_en = get_video_embed_link(self.company_story_video_url_en)
        super().set_values()
