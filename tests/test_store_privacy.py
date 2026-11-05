"""Exercise the Store's offline policy resource and allowlisted HTTP navigation."""

import http.client
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from build_support import runtime_asset_content, runtime_asset_mapping, stage_runtime_assets
from server import BoardHTTPServer, Handler

ROOT = Path(__file__).resolve().parents[1]
POLICY = "static/about/privacy.html"
CANONICAL = "store-listing/site/privacy.html"


class PolicyText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.current = None
        self.links = []
        self.footer = []
        self.in_footer = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "footer":
            self.in_footer = True
        if tag == "a":
            self.links.append(values.get("href", ""))
        if tag in ("h2", "p"):
            self.current = []
        elif tag == "h1":
            self.current = None

    def handle_data(self, value):
        if self.in_footer:
            self.footer.append(value)
        if self.current is not None:
            self.current.append(value)

    def handle_endtag(self, tag):
        if tag == "footer":
            self.in_footer = False
        if tag in ("h2", "p") and self.current is not None:
            self.blocks.append(" ".join("".join(self.current).split()))
            self.current = None


def policy_text(html):
    parser = PolicyText()
    parser.feed(html.decode("utf-8"))
    return parser


class StorePrivacyTests(unittest.TestCase):
    def serve(self, store):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        staged = stage_runtime_assets(Path(temporary.name), store_profile=store)
        server = BoardHTTPServer(("127.0.0.1", 0), Handler)
        server.access_token = "disposable-policy-token"
        self.state_reads = 0
        def snapshot():
            self.state_reads += 1
            raise AssertionError("Reading the policy must not read board state.")
        server.monitor = SimpleNamespace(store_profile=store, sandbox=store, snapshot=snapshot)
        root_patch = patch("server.ROOT", staged)
        root_patch.start()
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
            root_patch.stop()
        self.addCleanup(cleanup)
        return server, staged

    def request(self, server, path, *, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            headers = {"Host": host} if host else {}
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_store_about_links_to_complete_staged_policy_without_account_reads(self):
        server, staged = self.serve(True)
        status, about, _ = self.request(server, "/about/")
        self.assertEqual(status, 200)
        self.assertIn(b'href="/about/privacy/"', about)
        status, content, headers = self.request(server, "/about/privacy/")
        self.assertEqual(status, 200)
        canonical = (ROOT / CANONICAL).read_bytes()
        expected = policy_text(canonical)
        actual = policy_text(content)
        # The local page includes the canonical footer as a paragraph.
        self.assertEqual(actual.blocks[:len(expected.blocks)], expected.blocks)
        self.assertEqual(" ".join("".join(actual.footer).split()), " ".join("".join(expected.footer).split()))
        self.assertIn(b'href="/about/"', content)
        self.assertIn(b"connect-src 'none'; form-action 'none'", content)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertTrue(all(link.startswith("/") or link == "mailto:Spacelydigital@gmail.com" for link in actual.links))
        self.assertEqual(self.state_reads, 0)
        self.assertEqual((staged / POLICY).read_bytes(), runtime_asset_content(POLICY, canonical))

    def test_direct_distribution_keeps_its_bytes_and_has_no_new_policy_route(self):
        server, staged = self.serve(False)
        self.assertNotIn(POLICY, runtime_asset_mapping(store_profile=False))
        self.assertFalse((staged / POLICY).exists())
        self.assertEqual(self.request(server, "/about/privacy/")[0], 404)
        self.assertEqual((staged / "static/about/index.html").read_bytes(), (ROOT / "static/about/index.html").read_bytes())

    def test_policy_is_a_static_local_document_with_existing_host_guard(self):
        server, _ = self.serve(True)
        for route in ("/about/privacy.html", "/store-listing/site/privacy.html", "/about/privacy/../support.html"):
            with self.subTest(route=route):
                self.assertEqual(self.request(server, route)[0], 404)
        self.assertEqual(self.request(server, "/about/privacy/", host="elsewhere.invalid")[0], 403)
        self.assertEqual(self.request(server, "/api/state")[0], 403)
        self.assertEqual(self.state_reads, 0)

    def test_missing_policy_fails_locally_without_external_fallback(self):
        server, staged = self.serve(True)
        (staged / POLICY).unlink()
        status, content, headers = self.request(server, "/about/privacy/")
        self.assertEqual(status, 503)
        self.assertNotIn("Location", headers)
        self.assertNotIn(b"https://", content)
        self.assertEqual(self.state_reads, 0)

    def test_changed_canonical_structure_is_rejected_before_staging_a_partial_policy(self):
        canonical = (ROOT / CANONICAL).read_bytes()
        for damaged in (canonical.replace(b"<main>", b'<main class="changed">'),
                        canonical.replace(b"<h1>Privacy</h1>", b"<h1>Changed</h1>"),
                        canonical + b"<footer>extra</footer>"):
            with self.subTest(bytes=len(damaged)), self.assertRaises(ValueError):
                runtime_asset_content(POLICY, damaged)


if __name__ == "__main__":
    unittest.main()
