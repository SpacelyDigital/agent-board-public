"""Store labels use text and never serve provider vector assets."""

import http.client
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from build_support import runtime_asset_mapping, stage_runtime_assets
from server import BoardHTTPServer, Handler


ROOT = Path(__file__).resolve().parents[1]
ICONS = ("claude", "codex")
HTML_ROUTES = ("/", "/index.html", "/colors/", "/designs/", "/about/", "/about/privacy/")


class StoreBrandingTests(unittest.TestCase):
    def serve(self, store_profile, *, staged=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        assets = stage_runtime_assets(directory, store_profile=store_profile) if staged else ROOT
        server = BoardHTTPServer(("127.0.0.1", 0), Handler)
        server.monitor = SimpleNamespace(store_profile=store_profile, sandbox=store_profile)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        root_patch = patch("server.ROOT", assets)
        root_patch.start()
        worker.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
            root_patch.stop()

        self.addCleanup(cleanup)
        return server

    def request(self, server, path):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_store_stage_excludes_vectors_and_direct_stage_preserves_exact_bytes(self):
        store = runtime_asset_mapping(store_profile=True)
        direct = runtime_asset_mapping(store_profile=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store_stage = stage_runtime_assets(root / "store", store_profile=True)
            direct_stage = stage_runtime_assets(root / "direct", store_profile=False)
            for name in ICONS:
                relative = "static/icons/" + name + ".svg"
                with self.subTest(provider=name):
                    self.assertNotIn(relative, store)
                    self.assertEqual(direct[relative], relative)
                    self.assertFalse((store_stage / relative).exists())
                    self.assertEqual((direct_stage / relative).read_bytes(), (ROOT / relative).read_bytes())

    def test_store_icon_routes_are_not_found_even_when_source_vectors_exist(self):
        server = self.serve(True)
        for name in ICONS:
            for suffix in ("", "?cache=fixture"):
                with self.subTest(provider=name, suffix=suffix):
                    status, _, _ = self.request(server, "/icons/" + name + ".svg" + suffix)
                    self.assertEqual(status, 404)

    def test_direct_icon_routes_keep_original_source_bytes(self):
        server = self.serve(False)
        for name in ICONS:
            status, content, headers = self.request(server, "/icons/" + name + ".svg")
            self.assertEqual(status, 200)
            self.assertEqual(content, (ROOT / "static/icons" / (name + ".svg")).read_bytes())
            self.assertEqual(headers["Content-Type"], "image/svg+xml")

    def test_all_store_html_routes_get_one_constant_presentation_marker(self):
        server = self.serve(True, staged=True)
        for path in HTML_ROUTES:
            with self.subTest(path=path):
                status, content, headers = self.request(server, path)
                self.assertEqual(status, 200)
                self.assertEqual(content.count(b'data-provider-marks="off"'), 1)
                self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        status, content, _ = self.request(server, "/about/")
        self.assertEqual(status, 200)
        self.assertIn(b"Claude and Codex are trademarks", content)
        self.assertNotIn(b"logos belong", content)

    def test_direct_html_bytes_and_existing_icon_assets_are_unchanged(self):
        server = self.serve(False)
        for path, file in (("/", "index.html"), ("/index.html", "index.html"),
                           ("/colors/", "colors/index.html"), ("/designs/", "designs/index.html")):
            with self.subTest(path=path):
                status, content, _ = self.request(server, path)
                self.assertEqual(status, 200)
                self.assertEqual(content, (ROOT / "static" / file).read_bytes())
                self.assertNotIn(b'data-provider-marks="off"', content)


if __name__ == "__main__":
    unittest.main()
