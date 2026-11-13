"""Store labels use text and never serve provider vector assets."""

import http.client
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from build_support import runtime_asset_mapping, stage_runtime_assets
from server import BoardHTTPServer, Handler, _runtime_build_label


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

    def test_about_displays_running_package_build_in_direct_and_store_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            contents = Path(directory) / "Collector.app/Contents"
            executable = contents / "MacOS/agent-board-collector"
            executable.parent.mkdir(parents=True)
            info = contents / "Info.plist"
            info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "9.8.7", "CFBundleVersion": "123456"}))
            with patch("server.sys.frozen", True, create=True), patch("server.sys.executable", str(executable)):
                label = _runtime_build_label()
                self.assertEqual(label, "Version 9.8.7 (build 123456)")
                # The displayed identity remains the captured running package,
                # even if an app update replaces its on-disk metadata later.
                info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "9.8.8", "CFBundleVersion": "123457"}))
                with patch("server.BUILD_LABEL", label):
                    for store_profile in (False, True):
                        with self.subTest(store_profile=store_profile):
                            server = self.serve(store_profile, staged=True)
                            status, content, headers = self.request(server, "/about/")
                            self.assertEqual(status, 200)
                            self.assertIn(b'<p class="about-version">Version 9.8.7 (build 123456)</p>', content)
                            self.assertNotIn(b"runtime-build", content)
                            self.assertNotIn(b"123457", content)
                            self.assertNotIn(str(directory).encode(), content)
                            self.assertIn("default-src 'self'", headers["Content-Security-Policy"])


class RuntimeBuildLabelTests(unittest.TestCase):
    def test_source_run_does_not_claim_a_packaged_version(self):
        with patch("server.sys.frozen", False, create=True), patch("pathlib.Path.open", side_effect=AssertionError("unexpected read")):
            self.assertEqual(_runtime_build_label(), "Development build")

    def test_invalid_or_unreadable_package_metadata_has_no_injected_version(self):
        with tempfile.TemporaryDirectory() as directory:
            contents = Path(directory) / "Collector.app/Contents"
            executable = contents / "MacOS/agent-board-collector"
            executable.parent.mkdir(parents=True)
            info = contents / "Info.plist"
            with patch("server.sys.frozen", True, create=True), patch("server.sys.executable", str(executable)):
                self.assertEqual(_runtime_build_label(), "Version unavailable")
                for value in ({}, [], {"CFBundleShortVersionString": "<script>private</script>", "CFBundleVersion": "1"},
                              {"CFBundleShortVersionString": "1.2.3", "CFBundleVersion": True},
                              {"CFBundleShortVersionString": "1.2.3", "CFBundleVersion": "-1"}):
                    info.write_bytes(plistlib.dumps(value))
                    self.assertEqual(_runtime_build_label(), "Version unavailable")
                for raw in (b"not a plist", b"x" * 65537):
                    info.write_bytes(raw)
                    self.assertEqual(_runtime_build_label(), "Version unavailable")


class StorePresentationTests(unittest.TestCase):
    def run_node(self, script, *paths):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        result = subprocess.run([node, "-e", script, *map(str, paths)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_board_and_theme_renderers_keep_store_provider_headings_text_only(self):
        self.run_node(r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const board = fs.readFileSync(process.argv[1], 'utf8');
const boundary = board.indexOf('  // The native app injects');
assert.ok(boundary > 0);
for (const mode of ['off', undefined]) {
  let removed = 0, queried = false;
  const document = {documentElement:{dataset:{providerMarks:mode}}, querySelectorAll(selector) {
    assert.equal(selector, '.usage-logo'); queried = true;
    return [{remove(){removed++;}}, {remove(){removed++;}}];
  }};
  vm.runInNewContext(board.slice(0, boundary) + '})();', {document});
  assert.equal(removed, mode === 'off' ? 2 : 0);
  assert.equal(queried, mode === 'off');
}
const gallery = fs.readFileSync(process.argv[2], 'utf8');
const begin = gallery.indexOf('  function providerUsage('), end = gallery.indexOf('\n  preview.innerHTML', begin);
assert.ok(begin > 0 && end > begin);
for (const mode of ['off', undefined]) {
  const context = {document:{documentElement:{dataset:{providerMarks:mode}}},
    usage:[{provider:'Claude',remaining:40,reset:'1d',credits:'sample'},
           {provider:'Codex',remaining:50,reset:'2d',credits:'sample'}]};
  vm.createContext(context); vm.runInContext(gallery.slice(begin, end), context);
  for (const provider of ['Claude', 'Codex']) {
    const html = context.providerUsage(provider);
    assert.ok(html.includes('>' + provider + '</h2>'));
    assert.equal(html.includes('class="usage-logo'), mode !== 'off');
  }
}
""", ROOT / "static/app.js", ROOT / "static/colors/colors.js")
        css = (ROOT / "static/style.css").read_text()
        for provider in ICONS:
            self.assertIn(f'html:not([data-provider-marks="off"]) .usage-logo-{provider}', css)

    def test_actual_source_footer_distinguishes_limited_unavailable_and_disconnected(self):
        self.run_node(r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const begin = source.indexOf('  function renderSources() {'), end = source.indexOf('  function resetCountdown(', begin);
assert.ok(begin > 0 && end > begin);
function node(tag, className, text) {
  return {tag,className,text,children:[],attributes:{},append(...children){this.children.push(...children);},
    setAttribute(name,value){this.attributes[name]=value;}};
}
let rows;
const context = {state:{sources:[]},connected:true,make:node,
  document:{createDocumentFragment(){return node('fragment');}},
  elements:{sources:{replaceChildren(fragment){rows=fragment.children;}}}};
vm.createContext(context); vm.runInContext(source.slice(begin,end),context);
const cases = [
  [{available:true}, true, 'connected', false],
  [{available:true,waiting_for_chat:true}, true, 'waiting for a chat', false],
  [{available:true,coverage_limited:true}, true, 'limited', true],
  [{available:false,coverage_limited:true}, true, 'unavailable', false],
  [{available:true,coverage_limited:true}, false, 'unconfirmed', false],
  [{available:true,sample:true}, true, 'sample', false],
];
for (const [fields,connected,expected,limited] of cases) {
  context.connected=connected;
  const detail='Verified source; some histories are still being checked.';
  context.state.sources=[{id:'codex',name:'Codex',detail,...fields}];
  context.renderSources();
  assert.equal(rows.length,1);
  assert.equal(rows[0].children[2].text,expected);
  assert.equal(rows[0].className.split(' ').includes('limited'),limited);
  assert.equal(rows[0].title,detail);
  assert.equal(rows[0].attributes.role,'group');
  assert.equal(rows[0].attributes['aria-label'],`Codex: ${expected}. ${detail}`);
}
""", ROOT / "static/app.js")


if __name__ == "__main__":
    unittest.main()
