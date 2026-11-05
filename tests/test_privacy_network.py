"""Real HTTPS transport with fake data and a disposable, loopback-only CA.

The product's fixed HTTPS URLs are unchanged. Only the test process routes their
TCP sockets to its loopback TLS server and trusts its temporary certificate.
No test sends traffic to Anthropic, OpenAI, a developer endpoint or the Internet.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import privacy_network as network
import usage_source as usage
import claude_project_source as projects


class DirectProviderPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="agent-board-fake-tls-")
        folder = Path(cls.directory.name)
        cls.cert, key = folder / "fixture-ca.pem", folder / "fixture-key.pem"
        config = folder / "fixture-openssl.cnf"
        config.write_text("""[req]
distinguished_name=dn
x509_extensions=extensions
prompt=no
[dn]
CN=api.anthropic.com
[extensions]
subjectAltName=DNS:api.anthropic.com
basicConstraints=critical,CA:TRUE
keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign
extendedKeyUsage=serverAuth
""")
        try:
            subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048",
                            "-nodes", "-days", "1", "-config", str(config),
                            "-keyout", str(key), "-out", str(cls.cert)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            key.chmod(0o600)
            cls.context = ssl.create_default_context(cafile=str(cls.cert))
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(str(cls.cert), str(key))
            cls.requests, cls.connects = [], []
            cls.total_requests = 0
            cls.status, cls.body, cls.location = 200, b"{}", None

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    cls.total_requests += 1
                    cls.requests.append({"method": self.command, "path": self.path,
                                         "headers": dict(self.headers)})
                    self.send_response(cls.status)
                    if cls.location is not None:
                        self.send_header("Location", cls.location)
                    self.send_header("Content-Length", str(len(cls.body)))
                    self.end_headers()
                    self.wfile.write(cls.body)

            cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            cls.server.socket = server_context.wrap_socket(cls.server.socket, server_side=True)
            cls.thread = threading.Thread(target=cls.server.serve_forever,
                                          kwargs={"poll_interval": 0.01}, daemon=True)
            cls.thread.start()
        except Exception:
            cls.directory.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.directory.cleanup()

    def setUp(self):
        type(self).requests.clear()
        type(self).connects.clear()
        type(self).status, type(self).body, type(self).location = 200, b"{}", None

    @contextmanager
    def transport(self, *, trust_fixture=True):
        original_connect = socket.create_connection

        def connect(address, *args, **kwargs):
            self.connects.append(address)
            if address != ("api.anthropic.com", 443):
                raise AssertionError("A read tried an unexpected connection destination")
            return original_connect(("127.0.0.1", self.server.server_port), *args, **kwargs)

        # Any attempt to use a configured proxy must fail this test. The only
        # real sockets created here target the disposable loopback TLS fixture.
        proxy_environment = {name: "http://127.0.0.1:1" for name in
                             ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
        proxy_environment.update(no_proxy="", NO_PROXY="")
        with patch.dict(os.environ, proxy_environment), \
                patch("privacy_network.urllib.request.getproxies", side_effect=AssertionError("No proxy discovery allowed")), \
                patch("socket.create_connection", side_effect=connect):
            if trust_fixture:
                with patch("privacy_network.ssl.create_default_context", return_value=self.context):
                    yield
            else:
                yield

    def read(self, path="/api/oauth/usage", *, purpose="usage", token="fixture-only-bearer", max_bytes=4096):
        return network.read_provider_json(network.ANTHROPIC_ORIGIN + path, token,
                                          purpose=purpose, timeout=2, max_bytes=max_bytes)

    def test_direct_get_bypasses_proxy_and_sends_no_body_or_board_data(self):
        type(self).body = b'{"five_hour":{"utilization":20,"resets_at":1800000000}}'
        with self.transport():
            value = self.read()
        self.assertEqual(value["five_hour"]["utilization"], 20)
        self.assertEqual(self.connects, [("api.anthropic.com", 443)])
        self.assertEqual(len(self.requests), 1)
        row = self.requests[0]
        self.assertEqual(row["method"], "GET")
        self.assertEqual(row["path"], "/api/oauth/usage")
        headers = {name.lower(): value for name, value in row["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer fixture-only-bearer")
        self.assertNotIn("content-length", headers)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("referer", headers)
        self.assertNotIn("x-agent-board-access", headers)
        self.assertTrue(self.context.check_hostname)
        self.assertEqual(self.context.verify_mode, ssl.CERT_REQUIRED)

    def test_every_supported_read_route_reaches_only_the_provider(self):
        paths = ["/v1/code/channels?scope=all", "/v1/code/sessions/cse_fixture123",
                 "/v1/code/channels/chan_fixture123/timeline?limit=100",
                 "/v1/code/channels/chan_fixture123/threads/cmsg_fixture123?limit=1",
                 "/v1/code/channels/chan_fixture123/threads/cmsg_fixture123?limit=100&cursor=fixture%2B%2F%3D"]
        with self.transport():
            for path in paths:
                self.assertEqual(self.read(path, purpose="projects"), {})
        self.assertEqual([row["path"] for row in self.requests], paths)
        self.assertEqual(self.connects, [("api.anthropic.com", 443)] * len(paths))

    def test_foreign_origins_ports_credentials_fragments_and_write_routes_never_connect(self):
        urls = ["http://api.anthropic.com/api/oauth/usage",
                "https://api.anthropic.com.evil.example/api/oauth/usage",
                "https://api.anthropic.com:443/api/oauth/usage",
                "https://fixture@api.anthropic.com/api/oauth/usage",
                "https://api.anthropic.com/api/oauth/usage#secret",
                "https://api.anthropic.com/api/oauth/usage?secret=board-data",
                "https://api.anthropic.com/v1/messages",
                "file:///tmp/private", "https://api.anthropic.com/\napi/oauth/usage",
                "https://api.anthropic.com/v1/code/channels?scope=all&scope=all",
                "https://api.anthropic.com/v1/code/channels?scope=all&report=board-data",
                "https://api.anthropic.com/v1/code/channels/chan_fixture123/threads/cmsg_fixture123?limit=1&cursor=unexpected"]
        with self.transport():
            for url in urls:
                for purpose in ("usage", "projects"):
                    with self.subTest(url=url, purpose=purpose), self.assertRaises(network.ProviderRequestError):
                        network.read_provider_json(url, "fixture", purpose=purpose, timeout=2, max_bytes=4096)
        self.assertEqual(self.connects, [])
        self.assertEqual(self.requests, [])

    def test_header_injection_and_invalid_budgets_never_connect(self):
        with self.transport():
            for token in ("", "fixture\r\nX-Leak: yes", "fixture\x00secret", "ü", "x" * 16385):
                with self.subTest(token_length=len(token)), self.assertRaises(network.ProviderRequestError):
                    self.read(token=token)
            for max_bytes in (0, -1, True, 4 * 1024 * 1024 + 1):
                with self.assertRaises(network.ProviderRequestError):
                    self.read(max_bytes=max_bytes)
        self.assertEqual(self.connects, [])

    def test_every_redirect_rejects_before_any_second_connection(self):
        for status in (301, 302, 303, 307, 308):
            for target in ("https://developer.example/collect?secret=must-not-escape",
                           "/api/oauth/usage", "http://api.anthropic.com/api/oauth/usage"):
                self.setUp()
                type(self).status, type(self).location = status, target
                with self.subTest(status=status, target=target), self.transport():
                    with self.assertRaises(network.ProviderRequestError) as error:
                        self.read()
                self.assertEqual(error.exception.reason, "redirect")
                self.assertEqual(self.connects, [("api.anthropic.com", 443)])
                self.assertEqual(len(self.requests), 1)
                self.assertNotIn("must-not-escape", str(error.exception))

    def test_tls_certificate_must_be_trusted_before_any_authenticated_http(self):
        with self.transport(trust_fixture=False):
            with self.assertRaises(network.ProviderRequestError) as error:
                self.read()
        self.assertEqual(error.exception.reason, "unavailable")
        self.assertEqual(self.connects, [("api.anthropic.com", 443)])
        self.assertEqual(self.requests, [])

    def test_error_body_and_malformed_or_large_responses_do_not_escape(self):
        for status, body, reason in ((401, b"Bearer secret-error-body", "http"),
                                     (429, b"Bearer secret-error-body", "http"),
                                     (500, b"Bearer secret-error-body", "http"),
                                     (200, b"secret-malformed-json", "unavailable"),
                                     (200, b"[]", "response"),
                                     (200, b"x" * 4097, "oversized")):
            self.setUp()
            type(self).status, type(self).body = status, body
            with self.subTest(status=status, reason=reason), self.transport():
                with self.assertRaises(network.ProviderRequestError) as error:
                    self.read()
            self.assertEqual(error.exception.reason, reason)
            self.assertNotIn("secret", str(error.exception))
            self.assertNotIn("fixture-only-bearer", str(error.exception))

    def test_actual_usage_and_project_adapters_keep_public_fields_and_safe_errors(self):
        type(self).body = json.dumps({"five_hour": {"utilization": 21, "resets_at": 1800000000},
                                    "seven_day": {"utilization": 9, "resets_at": 1800100000},
                                    "private_account": "must-not-escape"}).encode()
        with self.transport(), patch("usage_source._claude_token", return_value="fixture-only-bearer"):
            windows = usage._fetch_claude_in_process(1799999999)
            self.assertEqual([row["remaining_percent"] for row in windows], [79, 91])
            self.assertNotIn("must-not-escape", json.dumps(windows))
            type(self).body = b'{"data":[]}'
            self.assertEqual(projects._Client("fixture-only-bearer").get("/v1/code/channels?scope=all"), {"data": []})
            type(self).status, type(self).body = 500, b"Bearer private-response-must-not-escape"
            for function, exception in ((lambda: usage._fetch_claude_in_process(1799999999), usage.UsageUnavailable),
                                         (lambda: projects._Client("fixture-only-bearer").get("/v1/code/channels?scope=all"), projects.ProjectUnavailable)):
                with self.assertRaises(exception) as error:
                    function()
                self.assertIn("HTTP 500", str(error.exception))
                self.assertNotIn("must-not-escape", str(error.exception))
        self.assertEqual(len(self.requests), 4)


if __name__ == "__main__":
    unittest.main()
