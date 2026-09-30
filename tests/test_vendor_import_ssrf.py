"""F3: SSRF-safe image download for the vendor bulk import."""
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

import urllib3

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.leaders_marketplace.tools import safe_fetch
from odoo.addons.leaders_marketplace.tools.safe_fetch import SafeFetchError, safe_get

from .test_vendor_import import IMAGE_PARAM, ImportCommon, PNG, new_row

MODULE = "odoo.addons.leaders_marketplace.tools.safe_fetch"
PUBLIC = "93.184.216.34"


def fake_dns(*addresses):
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
                for a in addresses]
    return getaddrinfo


class FakePoolResponse:
    def __init__(self, body=b"", status=200, headers=None, chunks=None, error=None):
        self.status, self.headers = status, headers or {}
        self._chunks = chunks if chunks is not None else ([body] if body else [])
        self._error = error
        self.closed = False

    def stream(self, size):
        for chunk in self._chunks:
            yield chunk
        if self._error:
            raise self._error

    def close(self):
        self.closed = True


@tagged("post_install", "-at_install")
class TestSafeGet(TransactionCase):

    def _blocked(self, *addresses, url="http://images.example.com/a.png"):
        with patch("socket.getaddrinfo", fake_dns(*addresses)), patch(MODULE + "._request") as request:
            with self.assertRaises(SafeFetchError) as ctx:
                safe_get(url)
            request.assert_not_called()
        self.assertEqual(ctx.exception.code, "blocked", addresses)

    def test_private_and_special_addresses_are_rejected_without_a_connection(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.5", "172.16.0.9", "::1", "fc00::1",
                        "fe80::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "224.0.0.1", "ff02::1", "240.0.0.1",
                        "0.0.0.0", "::", "100.64.0.1", "64:ff9b::7f00:1", "2002:7f00:1::1"):
            self._blocked(address)

    def test_mixed_answers_are_rejected(self):
        self._blocked(PUBLIC, "10.0.0.1")
        self._blocked("127.0.0.1", PUBLIC)

    def test_ip_literals_and_odd_spellings_are_rejected(self):
        # no DNS patch needed: the literal itself is "resolved" by getaddrinfo
        for url in ("http://127.0.0.1/a.png", "http://2130706433/a.png", "http://0x7f.0.0.1/a.png",
                    "http://[::1]/a.png", "http://[::ffff:7f00:1]/a.png", "http://169.254.169.254/latest/meta-data",
                    "http://localhost/a.png"):
            with patch(MODULE + "._request") as request:
                with self.assertRaises(SafeFetchError, msg=url) as ctx:
                    safe_get(url)
                request.assert_not_called()
            self.assertIn(ctx.exception.code, ("blocked", "resolve", "url"), url)

    def test_bad_urls_are_rejected(self):
        for url in ("file:///etc/passwd", "ftp://93.184.216.34/a.png", "gopher://93.184.216.34/_x",
                    "http://user@93.184.216.34/a.png", "http://user:pw@example.com/a.png",
                    "http://example.com@127.0.0.1/a.png", "http://93.184.216.34:8080/a.png",
                    "http://93.184.216.34:22/a.png", "https://93.184.216.34:80/a.png", "http:///a.png",
                    "http://exa mple.com/a.png", "http://example.com/a\r\nHost: evil", "", "//example.com/a.png",
                    "http://example.com\\@127.0.0.1/", "http://example.com/" + "a" * 2100):
            with patch("socket.getaddrinfo") as dns, patch(MODULE + "._request") as request:
                with self.assertRaises(SafeFetchError, msg=url):
                    safe_get(url)
                dns.assert_not_called()
                request.assert_not_called()

    def test_unresolvable_host(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror()), patch(MODULE + "._request") as request:
            with self.assertRaises(SafeFetchError) as ctx:
                safe_get("http://nope.example.com/a.png")
            request.assert_not_called()
        self.assertEqual(ctx.exception.code, "resolve")

    def test_dns_is_resolved_once_and_connection_goes_to_the_resolved_ip(self):
        dns = MagicMock(side_effect=fake_dns(PUBLIC))
        response = FakePoolResponse(PNG)
        with patch("socket.getaddrinfo", dns), patch(MODULE + "._request", return_value=response) as request:
            self.assertEqual(safe_get("http://images.example.com/img/a.png?x=1"), PNG)
        self.assertEqual(dns.call_count, 1)
        args = request.call_args.args
        self.assertEqual(args[:5], ("http", PUBLIC, 80, "images.example.com", "/img/a.png?x=1"))
        self.assertTrue(response.closed)

    def test_http_request_reaches_local_server_by_ip_with_original_host_header(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen["host"], seen["path"] = self.headers.get("Host"), self.path
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1/secret")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(PNG)))
                self.end_headers()
                self.wfile.write(PNG)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        # The fake "public" resolution is 127.0.0.1 so the real socket goes to the local server; the
        # public check and the default-port table are patched for this test only.
        with patch("socket.getaddrinfo", fake_dns("127.0.0.1")), \
                patch(MODULE + "._is_public_address", return_value=True), \
                patch.dict(safe_fetch.DEFAULT_PORTS, {"http": port}):
            self.assertEqual(safe_get("http://images.example.com/img.png"), PNG)
            self.assertEqual(seen["host"], "images.example.com")
            self.assertEqual(seen["path"], "/img.png")
            with self.assertRaises(SafeFetchError) as ctx:
                safe_get("http://images.example.com/redirect")
            self.assertEqual(ctx.exception.code, "redirect")
            self.assertEqual(seen["path"], "/redirect", "the redirect target must never be requested")

    def test_https_uses_original_hostname_for_sni_and_certificate_check(self):
        captured = {}

        def urlopen(pool, method, target, **kwargs):
            captured.update(pool=pool, headers=kwargs["headers"], kwargs=kwargs)
            return FakePoolResponse(PNG)

        with patch("socket.getaddrinfo", fake_dns(PUBLIC)), \
                patch.object(urllib3.HTTPSConnectionPool, "urlopen", urlopen):
            self.assertEqual(safe_get("https://images.example.com/a.png"), PNG)
        pool = captured["pool"]
        self.assertIsInstance(pool, urllib3.HTTPSConnectionPool)
        self.assertEqual(pool.host, PUBLIC)
        self.assertEqual(pool.port, 443)
        self.assertEqual(pool.assert_hostname, "images.example.com")
        self.assertEqual(pool.conn_kw.get("server_hostname"), "images.example.com")
        self.assertEqual(pool.cert_reqs, "CERT_REQUIRED")
        self.assertEqual(captured["headers"]["Host"], "images.example.com")
        self.assertFalse(captured["kwargs"]["redirect"])
        # the pool builds a connection object that carries the SNI / certificate host name
        conn = pool._new_conn()
        self.assertEqual(conn.host, PUBLIC)
        self.assertEqual(conn.server_hostname, "images.example.com")
        self.assertEqual(conn.assert_hostname, "images.example.com")
        conn.close()

    def test_redirect_is_not_followed(self):
        for status in (301, 302, 303, 307, 308):
            with patch("socket.getaddrinfo", fake_dns(PUBLIC)), patch(
                    MODULE + "._request",
                    return_value=FakePoolResponse(status=status, headers={"Location": "http://127.0.0.1/x"})) as request:
                with self.assertRaises(SafeFetchError) as ctx:
                    safe_get("http://images.example.com/a.png")
                self.assertEqual(request.call_count, 1)
            self.assertEqual(ctx.exception.code, "redirect")

    def test_http_error_status(self):
        with patch("socket.getaddrinfo", fake_dns(PUBLIC)), patch(
                MODULE + "._request", return_value=FakePoolResponse(status=404)):
            with self.assertRaises(SafeFetchError) as ctx:
                safe_get("http://images.example.com/a.png")
        self.assertEqual((ctx.exception.code, ctx.exception.detail), ("status", 404))

    def test_oversize_is_rejected(self):
        with patch("socket.getaddrinfo", fake_dns(PUBLIC)):
            with patch(MODULE + "._request", return_value=FakePoolResponse(
                    headers={"Content-Length": "5000"})):
                with self.assertRaises(SafeFetchError) as ctx:
                    safe_get("http://images.example.com/a.png", max_bytes=1000)
                self.assertEqual(ctx.exception.code, "too_large")
            response = FakePoolResponse(chunks=[b"x" * 600, b"x" * 600, b"x" * 600])
            with patch(MODULE + "._request", return_value=response):
                with self.assertRaises(SafeFetchError) as ctx:
                    safe_get("http://images.example.com/a.png", max_bytes=1000)
                self.assertEqual(ctx.exception.code, "too_large")
            self.assertTrue(response.closed)

    def test_timeouts_are_handled(self):
        with patch("socket.getaddrinfo", fake_dns(PUBLIC)):
            with patch(MODULE + "._request", side_effect=urllib3.exceptions.ConnectTimeoutError()):
                with self.assertRaises(SafeFetchError) as ctx:
                    safe_get("http://images.example.com/a.png")
                self.assertEqual(ctx.exception.code, "timeout")
            with patch(MODULE + "._request", return_value=FakePoolResponse(
                    chunks=[b"a"], error=urllib3.exceptions.ReadTimeoutError(None, None, "slow"))):
                with self.assertRaises(SafeFetchError) as ctx:
                    safe_get("http://images.example.com/a.png")
                self.assertEqual(ctx.exception.code, "timeout")
            with patch(MODULE + "._request", side_effect=ConnectionRefusedError()):
                with self.assertRaises(SafeFetchError) as ctx:
                    safe_get("http://images.example.com/a.png")
                self.assertEqual(ctx.exception.code, "network")
            # overall deadline: a server that keeps trickling data is cut off
            clock = iter([0, 0, 100, 200, 300])
            with patch(MODULE + "._request", return_value=FakePoolResponse(chunks=[b"a", b"b", b"c"])), \
                    patch(MODULE + ".time.monotonic", lambda: next(clock)):
                with self.assertRaises(SafeFetchError) as ctx:
                    safe_get("http://images.example.com/a.png", timeout=10)
                self.assertEqual(ctx.exception.code, "timeout")


@tagged("post_install", "-at_install")
class TestVendorImportImageSetting(ImportCommon):

    def _get(self, sku):
        return self.Offer.search([("seller_sku", "=", sku), ("vendor_id", "=", self.vendor.id)])

    def _images(self, job):
        return [(lvl, c, m) for r, c, lvl, m in self._errors(job) if c == "image_url"]

    def test_setting_off_by_default_ignores_urls_with_warning_and_no_fetch(self):
        self.param.search([("key", "=", IMAGE_PARAM)]).unlink()
        self.assertFalse(self.Import._leaders_image_urls_allowed())
        with patch(MODULE + ".safe_get") as get, patch(MODULE + "._request") as request, \
                patch("socket.getaddrinfo") as dns:
            job = self._new_job([new_row(name="A", sku="SS-1", price="5", image_url="http://93.184.216.34/a.png")])
            self.assertEqual(job.valid_rows, 1)
            self.assertIn("disabled", self._images(job)[0][2], "warning at validation time")
            self._run(job)
            get.assert_not_called()
            request.assert_not_called()
            dns.assert_not_called()
        self.assertEqual(job.applied_rows, 1)
        self.assertFalse(self._get("SS-1").product_tmpl_id.image_1920)
        self.assertTrue(all(lvl == "warning" for lvl, c, m in self._images(job)))
        self.assertTrue(self._images(job))

    def test_setting_false_value_is_off_and_true_is_on(self):
        self.param.set_param(IMAGE_PARAM, "False")
        self.assertFalse(self.Import._leaders_image_urls_allowed())
        self.param.set_param(IMAGE_PARAM, "True")
        self.assertTrue(self.Import._leaders_image_urls_allowed())

    def test_setting_on_downloads_and_private_host_only_warns(self):
        with patch("socket.getaddrinfo", fake_dns("10.0.0.1")), patch(MODULE + "._request") as request:
            job = self._new_job([new_row(name="A", sku="SS-2", price="5", image_url="http://intranet.example.com/a.png")])
            self._run(job)
            request.assert_not_called()
        self.assertEqual(job.applied_rows, 1)
        self.assertFalse(self._get("SS-2").product_tmpl_id.image_1920)
        self.assertIn("private", self._images(job)[0][2])
        with patch("socket.getaddrinfo", fake_dns("127.0.0.1")), patch(MODULE + "._request") as request:
            job = self._new_job([new_row(name="B", sku="SS-3", price="5", image_url="http://loop.example.com/a.png")])
            self._run(job)
            request.assert_not_called()
        self.assertIn("private", self._images(job)[0][2])
        with patch("socket.getaddrinfo", fake_dns(PUBLIC)), \
                patch(MODULE + "._request", return_value=FakePoolResponse(PNG)):
            job = self._new_job([new_row(name="C", sku="SS-4", price="5", image_url="http://img.example.com/a.png")])
            self._run(job)
        self.assertTrue(self._get("SS-4").product_tmpl_id.image_1920)
