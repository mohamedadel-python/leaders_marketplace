from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from unittest.mock import patch

import requests

from ..models import res_config_settings as video
from ..models.res_config_settings import get_video_embed_link, to_video_asset

YT = "https://www.youtube-nocookie.com/embed/1YYVp3IkCfI"
DRIVE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
DRIVE = "https://drive.google.com/file/d/%s/preview" % DRIVE_ID


@tagged("post_install", "-at_install")
class TestVideoLinks(TransactionCase):
    """What the admin pastes and what is saved (video links contract, section 2)."""

    def test_youtube_forms(self):
        for link in ("https://www.youtube.com/watch?v=1YYVp3IkCfI",
                     "https://www.youtube.com/watch?v=1YYVp3IkCfI&t=42s&list=PL1",
                     "https://youtu.be/1YYVp3IkCfI?si=abc", "https://www.youtube.com/shorts/1YYVp3IkCfI",
                     "https://www.youtube.com/live/1YYVp3IkCfI", "https://www.youtube.com/embed/1YYVp3IkCfI",
                     "https://m.youtube.com/watch?v=1YYVp3IkCfI", YT, "youtu.be/1YYVp3IkCfI"):
            self.assertEqual(to_video_asset(link), {"kind": "youtube", "url": YT}, link)

    def test_drive_forms(self):
        for link in ("https://drive.google.com/file/d/%s/view?usp=sharing" % DRIVE_ID,
                     "https://drive.google.com/file/d/%s/view?usp=drive_link" % DRIVE_ID,
                     "https://drive.google.com/open?id=%s" % DRIVE_ID,
                     "https://drive.google.com/uc?id=%s&export=download" % DRIVE_ID, DRIVE):
            self.assertEqual(to_video_asset(link), {"kind": "drive", "url": DRIVE}, link)

    def test_rejected(self):
        for link in ("https://drive.google.com/drive/folders/%s" % DRIVE_ID, "https://vimeo.com/123456",
                     "https://www.facebook.com/watch/?v=1", "https://example.com/x.mp4",
                     "https://www.youtube.com/watch?v=short", "https://drive.google.com/file/d/abc/view"):
            self.assertIsNone(to_video_asset(link), link)
            with self.assertRaises(UserError):
                get_video_embed_link(link)
        self.assertFalse(get_video_embed_link(""))

    def test_rejection_message_is_the_contract_text(self):
        with self.assertRaises(UserError) as caught:
            get_video_embed_link("https://vimeo.com/1")
        self.assertEqual(caught.exception.args[0], "Paste a YouTube or Google Drive video link")

    def test_drive_sharing_warning(self):
        class Answer:
            def __init__(self, status, url, text=""):
                self.status_code, self.url, self.text = status, url, text
        link = "https://drive.google.com/file/d/%s/view?usp=sharing" % DRIVE_ID
        with patch.object(video.requests, "get", return_value=Answer(200, DRIVE, "<video>")):
            self.assertFalse(video.drive_sharing_warning(link))  # public: no warning
        with patch.object(video.requests, "get", return_value=Answer(200, "https://accounts.google.com/ServiceLogin?x")):
            self.assertIn("Anyone with the link", video.drive_sharing_warning(link))
        with patch.object(video.requests, "get", return_value=Answer(404, DRIVE)):
            self.assertTrue(video.drive_sharing_warning(link))
        with patch.object(video.requests, "get", side_effect=requests.ConnectionError()):
            self.assertFalse(video.drive_sharing_warning(link))  # Google unreachable: never blocks
        with patch.object(video.requests, "get") as called:
            self.assertFalse(video.drive_sharing_warning("https://youtu.be/1YYVp3IkCfI"))  # YouTube: not checked
            called.assert_not_called()
        # the form warns (onchange) but does not block
        with patch.object(video.requests, "get", return_value=Answer(404, DRIVE)):
            settings = self.env["res.config.settings"].new({"company_story_video_url_ar": link})
            self.assertEqual(settings._onchange_company_story_video()["warning"]["title"], "Google Drive sharing")
            row = self.env["spark.testimonial"].new({"video_url": link})
            self.assertEqual(row._onchange_video_url()["warning"]["title"], "Google Drive sharing")
        self.assertEqual(self.env["spark.testimonial"].new({"video_url": "https://vimeo.com/1"})._onchange_video_url()
                         ["warning"]["message"], "Paste a YouTube or Google Drive video link")

    def test_saved_on_the_settings_and_the_testimonial(self):
        settings = self.env["res.config.settings"].create({
            "company_story_video_url_ar": "https://drive.google.com/file/d/%s/view?usp=sharing" % DRIVE_ID,
            "company_story_video_url_en": "https://youtu.be/1YYVp3IkCfI"})
        settings.set_values()
        params = self.env["ir.config_parameter"].sudo()
        self.assertEqual(params.get_param("spark_fu.company_story_video_url_ar"), DRIVE)
        self.assertEqual(params.get_param("spark_fu.company_story_video_url_en"), YT)
        with self.assertRaises(UserError):
            self.env["res.config.settings"].create({"company_story_video_url_ar": "https://vimeo.com/1"}).set_values()


@tagged("post_install", "-at_install")
class TestVideoEndpoints(HttpCase):

    def test_company_story_follows_the_language_and_kind(self):
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("spark_fu.company_story_video_url_ar", DRIVE)
        params.set_param("spark_fu.company_story_video_url_en", YT)
        ar = self.url_open("/company-story", headers={"Accept-Language": "ar"}).json()
        en = self.url_open("/company-story", headers={"Accept-Language": "en"}).json()
        self.assertEqual(ar["video"], {"kind": "drive", "url": DRIVE})
        self.assertEqual(en["video"], {"kind": "youtube", "url": YT})
        params.set_param("spark_fu.company_story_video_url_ar", False)
        params.set_param("spark_fu.company_story_video_url_en", False)
        self.assertNotIn("video", self.url_open("/company-story").json())

    def test_testimonials_send_youtube_drive_or_nothing(self):
        import base64
        import io
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (630, 914), (60, 80, 150)).save(buffer, "JPEG", quality=60)
        picture = base64.b64encode(buffer.getvalue())
        Testimonial = self.env["spark.testimonial"]
        Testimonial.search([]).write({"state": "draft"})
        common = {"role": "Engineer", "quote": "Good", "image": picture, "customer_permission": True,
                  "state": "published"}
        Testimonial.create([
            dict(common, name="A", sequence=1, video_url="https://youtu.be/1YYVp3IkCfI"),
            dict(common, name="B", sequence=2, video_url="https://drive.google.com/open?id=%s" % DRIVE_ID),
            dict(common, name="C", sequence=3)])
        rows = self.url_open("/testimonials?limit=4").json()
        self.assertEqual([r.get("video") for r in rows],
                         [{"kind": "youtube", "url": YT}, {"kind": "drive", "url": DRIVE}, None])
        self.assertNotIn("video", rows[2])
