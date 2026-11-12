"""Check local support assets and DOM behavior without Storefront or account calls."""

from dataclasses import replace
import http.client
import json
from pathlib import Path
import subprocess
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from build_store import HELPER, parse_arguments, validate_store_asset_exclusions
from build_support import AppConfiguration, runtime_asset_mapping, stage_runtime_assets
from server import BoardHTTPServer, Handler

ROOT = Path(__file__).resolve().parents[1]
QR = "static/about/ko-fi.svg"


class StoreSupportTests(unittest.TestCase):
    def test_default_hidden_and_explicit_us_strategy_have_exact_asset_delta(self):
        hidden = runtime_asset_mapping(store_profile=True)
        optional = runtime_asset_mapping(store_profile=True, support_strategy="us-external")
        self.assertEqual(set(optional) - set(hidden), {QR})
        self.assertEqual({k: v for k, v in optional.items() if k != QR}, hidden)
        for excluded in ("static/about/ko-fi.png", "static/about/store.html",
                         "static/icons/claude.svg", "static/icons/codex.svg"):
            self.assertNotIn(excluded, optional)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = stage_runtime_assets(root / "us", store_profile=True, support_strategy="us-external")
            self.assertEqual((staged / QR).read_bytes(), (ROOT / QR).read_bytes())
            self.assertEqual({str(p.relative_to(staged)) for p in staged.rglob("*") if p.is_file()}, set(optional))
            about = (staged / "static/about/index.html").read_text()
            self.assertNotIn("ko-fi.com", about)
            self.assertNotIn("optional-coffee", about)
            direct = stage_runtime_assets(root / "direct")
            self.assertEqual((direct / "static/about/index.html").read_bytes(),
                             (ROOT / "static/about/index.html").read_bytes())
            self.assertEqual((direct / QR).read_bytes(), (ROOT / QR).read_bytes())
        svg = ET.fromstring((ROOT / QR).read_bytes())
        self.assertEqual({node.tag.rsplit("}", 1)[-1] for node in svg.iter()}, {"svg", "path"})
        self.assertFalse(any("href" in name.lower() for node in svg.iter() for name in node.attrib))

    def test_unknown_and_wrong_channel_strategy_fail_before_existing_stage_changes(self):
        configuration = AppConfiguration("local.agentboard.fixture", "0.2.5", "24", signing_channel="store-preview")
        self.assertEqual(configuration.store_support_strategy, "hidden")
        self.assertEqual(replace(configuration, store_support_strategy="us-external").store_support_strategy, "us-external")
        with self.assertRaises(ValueError):
            replace(configuration, store_support_strategy="usa")
        with self.assertRaises(ValueError):
            replace(configuration, signing_channel="direct", store_support_strategy="us-external")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "runtime-assets/preserved.txt"
            marker.parent.mkdir()
            marker.write_bytes(b"preserved stage")
            for profile, strategy in ((True, "usa"), (False, "us-external")):
                with self.subTest(profile=profile, strategy=strategy), self.assertRaises(ValueError):
                    stage_runtime_assets(root, store_profile=profile, support_strategy=strategy)
                self.assertEqual(marker.read_bytes(), b"preserved stage")

    def test_preview_strategy_defaults_hidden_without_accounts_or_building(self):
        with tempfile.TemporaryDirectory() as directory, patch("subprocess.run", side_effect=AssertionError("No account or build call")):
            output = str(ROOT / "build" / ("support-fixture-" + Path(directory).name))
            _, default, _ = parse_arguments(["--preview", "--bundle-id", "local.agentboard.fixture", "--output", output])
            _, optional, _ = parse_arguments(["--preview", "--bundle-id", "local.agentboard.fixture", "--output", output,
                                              "--support-strategy", "us-external"])
            self.assertEqual(default.store_support_strategy, "hidden")
            self.assertEqual(optional.store_support_strategy, "us-external")

    def test_only_exact_helper_resource_location_can_contain_optional_svg(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Agent Board.app"
            for contents in (app / "Contents", app / HELPER / "Contents"):
                for folder in ("Resources", "Frameworks"):
                    path = contents / folder / QR
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes((ROOT / QR).read_bytes())
                    with self.assertRaises(ValueError):
                        validate_store_asset_exclusions(app)
                    if path == app / HELPER / "Contents/Resources" / QR:
                        validate_store_asset_exclusions(app, "us-external")
                    else:
                        with self.assertRaises(ValueError):
                            validate_store_asset_exclusions(app, "us-external")
                    path.unlink()
            forbidden = app / HELPER / "Contents/Resources/static/about/ko-fi.png"
            forbidden.write_bytes(b"not allowed")
            with self.assertRaises(ValueError):
                validate_store_asset_exclusions(app, "us-external")

    def test_optional_qr_accepts_only_exact_pyinstaller_internal_directory_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Agent Board.app"
            contents = app / HELPER / "Contents"
            primary = contents / "Resources" / QR
            primary.parent.mkdir(parents=True)
            primary.write_bytes((ROOT / QR).read_bytes())
            alias = contents / "Frameworks/static"
            alias.parent.mkdir()
            alias.symlink_to("../Resources/static", target_is_directory=True)
            self.assertTrue((alias / "about/ko-fi.svg").samefile(primary))
            validate_store_asset_exclusions(app, "us-external")
            with self.assertRaises(ValueError):
                validate_store_asset_exclusions(app, "hidden")
            alias.unlink()
            outside = Path(directory) / "outside"
            (outside / "about").mkdir(parents=True)
            (outside / "about/ko-fi.svg").write_bytes(primary.read_bytes())
            for target in ("../Resources/static/../static", str(contents / "Resources/static"), str(outside)):
                with self.subTest(target=Path(target).name):
                    alias.symlink_to(target, target_is_directory=True)
                    with self.assertRaises(ValueError):
                        validate_store_asset_exclusions(app, "us-external")
                    alias.unlink()
            alias.mkdir()
            (alias / "about").mkdir()
            (alias / "about/ko-fi.svg").write_bytes(primary.read_bytes())
            with self.assertRaises(ValueError):
                validate_store_asset_exclusions(app, "us-external")

    def test_selected_strategy_serves_only_bundled_local_qr_without_external_fallback(self):
        for strategy, expected in (("hidden", 503), ("us-external", 200)):
            with self.subTest(strategy=strategy), tempfile.TemporaryDirectory() as directory:
                staged = stage_runtime_assets(Path(directory), store_profile=True, support_strategy=strategy)
                server = BoardHTTPServer(("127.0.0.1", 0), Handler)
                server.monitor = SimpleNamespace(store_profile=True, sandbox=True)
                worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
                with patch("server.ROOT", staged):
                    worker.start()
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    try:
                        connection.request("GET", "/about/ko-fi.svg")
                        response = connection.getresponse()
                        body = response.read()
                        self.assertEqual(response.status, expected)
                        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
                        self.assertIsNone(response.getheader("Location"))
                        if expected == 200:
                            self.assertEqual(body, (ROOT / QR).read_bytes())
                            self.assertEqual(response.getheader("Content-Type"), "image/svg+xml")
                    finally:
                        connection.close()
                        server.shutdown()
                        server.server_close()
                        worker.join(timeout=2)

    def test_actual_native_section_script_creates_and_removes_only_local_content(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required to exercise the native support section's JavaScript.")
        source = (ROOT / "MacApp.swift").read_text()
        function = source.split("func externalSupportSectionScript(visible: Bool) -> String {", 1)[1].split("\nfinal class AppDelegate", 1)[0]
        script = function.split('return """', 1)[1].split('"""', 1)[0]
        program = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(process.argv[1]);
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; }
  setAttribute(name, value) { this.attributes[name] = value; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  cloneNode() { const node = new Element(this.tagName); for (const key of ['className','href','target','rel']) node[key] = this[key]; return node; }
  querySelector(value) { return this.children.find(x => '.' + x.className === value); }
  insertBefore(node, sibling) { assert(this.children.includes(sibling)); node.parent = this; this.children.splice(this.children.indexOf(sibling),0,node); }
}
const page = new Element('main'), footer = new Element('footer');
footer.className = 'about-footer'; page.append(footer);
let resources = [];
const document = {
  querySelector: selector => selector === '.about-page' ? page : null,
  getElementById: id => page.children.find(x => x.id === id),
  createElement: tag => { const node = new Element(tag); if (tag === 'img') Object.defineProperty(node, 'src', { set(value) { resources.push(value); this.source = value; }, get() { return this.source; } }); return node; }
};
const context = {document};
function run(visible) { vm.runInNewContext(input.script.replace('\\(flag)', String(visible)), context); }
run(false); assert.equal(page.children.length,1); assert.deepEqual(resources,[]);
run(true); assert.equal(page.children.length,2);
const section = page.children[0];
assert.equal(section.id,'optional-coffee'); assert.equal(section.attributes['aria-labelledby'],'coffee-heading');
const [copy,qr] = section.children, [heading,invitation,privacy,link] = copy.children;
assert.equal(heading.id,'coffee-heading');
assert(invitation.textContent.includes('free')); assert(invitation.textContent.includes('does not unlock features'));
assert(privacy.textContent.includes('may share your contact and transaction details with Spacely Digital'));
for (const anchor of [link,qr]) { assert.equal(anchor.href,'https://ko-fi.com/spacelydigital'); assert.equal(anchor.target,'_blank'); assert.equal(anchor.rel,'noopener noreferrer'); }
assert(qr.attributes['aria-label']); assert.equal(qr.children[0].src,'/about/ko-fi.svg');
assert.deepEqual(resources,['/about/ko-fi.svg']);
run(true); assert.equal(page.children.filter(x => x.id === 'optional-coffee').length,1);
run(false); assert.equal(page.children.length,1);
const creations = resources.length; document.querySelector = () => null;
run(true); assert.equal(page.children.length,1); assert.equal(resources.length,creations);
console.log('PASS: native-generated section, hidden baseline, local QR, exact destinations, disclosure, replacement and removal');
'''
        completed = subprocess.run([node, "-e", program, json.dumps({"script": script})], capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
