"""The optional account readers connect directly to the provider, never a relay.

This boundary accepts only the existing read-only Anthropic usage and Code
routes. It ignores environment/system proxy configuration, uses verified TLS,
rejects redirects, and never includes remote text or credentials in errors.
Codex's separately installed app-server owns its own account connection.
"""

from __future__ import annotations

import http.client
from email.utils import parsedate_to_datetime
import json
import re
import ssl
import time
from urllib.parse import parse_qsl, urlsplit
import urllib.error
import urllib.request


ANTHROPIC_ORIGIN = "https://api.anthropic.com"
_PROJECT_PATH = re.compile(
    r"/v1/code/(?:channels(?:/chan_[A-Za-z0-9_]{6,120}/"
    r"(?:timeline|threads/cmsg_[A-Za-z0-9_]{6,120}))?|sessions/cse_[A-Za-z0-9_]{6,120})"
)


class ProviderRequestError(Exception):
    """A fixed reason and optional HTTP status, without remote or local details."""

    def __init__(self, reason: str, status: int | None = None, *, retry_after: float | None = None):
        self.reason = reason
        self.status = status
        self.retry_after = retry_after
        super().__init__("The direct provider read was unavailable.")


def _retry_after(value: object) -> float | None:
    """Retain only a bounded wait duration, never an arbitrary response header."""
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]{1,8}", value):
            seconds = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = date.timestamp() - time.time()
        return max(0.0, min(86400.0, seconds))
    except (ValueError, TypeError, OverflowError):
        return None


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderRequestError("redirect")


def _validate_url(url: str, purpose: str) -> None:
    if (not isinstance(url, str) or len(url) > 16384
            or any(ord(char) < 33 or ord(char) > 126 for char in url)):
        raise ProviderRequestError("request")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ProviderRequestError("request") from None
    # Exact authority also rejects userinfo, extra ports, suffix domains and
    # alternate spellings. No caller can substitute a developer/relay endpoint.
    if (parts.scheme != "https" or parts.netloc != "api.anthropic.com"
            or parts.fragment or not url.startswith(ANTHROPIC_ORIGIN + "/")):
        raise ProviderRequestError("request")
    if purpose == "usage":
        if parts.path != "/api/oauth/usage" or parts.query:
            raise ProviderRequestError("request")
        return
    if purpose != "projects" or not _PROJECT_PATH.fullmatch(parts.path):
        raise ProviderRequestError("request")
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=3, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        raise ProviderRequestError("request") from None
    query = dict(pairs)
    if len(query) != len(pairs):
        raise ProviderRequestError("request")
    if parts.path == "/v1/code/channels":
        valid = query == {"scope": "all"}
    elif parts.path.startswith("/v1/code/sessions/"):
        valid = not query
    elif parts.path.endswith("/timeline"):
        valid = query == {"limit": "100"}
    else:
        cursor = query.get("cursor")
        valid = (set(query) <= {"limit", "cursor"} and query.get("limit") in ("1", "100")
                 and (cursor is None or (query["limit"] == "100" and 0 < len(cursor) <= 4096
                      and not any(ord(char) < 32 or ord(char) == 127 for char in cursor))))
    if not valid:
        raise ProviderRequestError("request")


def read_provider_json(url: str, token: str, *, purpose: str,
                       timeout: float, max_bytes: int) -> dict:
    """Perform one bounded GET with a bearer only to an approved HTTPS route."""
    _validate_url(url, purpose)
    if (not isinstance(token, str) or not 0 < len(token) <= 16384
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
            or not isinstance(max_bytes, int) or isinstance(max_bytes, bool)
            or not 0 < max_bytes <= 4 * 1024 * 1024
            or not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not 0 < timeout <= 30):
        raise ProviderRequestError("request")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json",
               "anthropic-beta": "oauth-2025-04-20", "User-Agent": "AgentBoard/1.0"}
    if purpose == "projects":
        headers["anthropic-version"] = "2023-06-01"
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        # An empty proxy map avoids both inherited environment variables and
        # macOS system proxy discovery. There is no fallback to a relay.
        context = ssl.create_default_context()
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), RejectRedirects(),
            urllib.request.HTTPSHandler(context=context))
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ProviderRequestError("oversized")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ProviderRequestError("response")
        return value
    except ProviderRequestError:
        raise
    except urllib.error.HTTPError as error:
        status = error.code
        retry_after = _retry_after(error.headers.get("Retry-After")) if error.headers and status in (429, 503) else None
        error.close()
        raise ProviderRequestError("http", status, retry_after=retry_after) from None
    except (OSError, ValueError, UnicodeError, RecursionError, urllib.error.URLError,
            http.client.HTTPException):
        raise ProviderRequestError("unavailable") from None
