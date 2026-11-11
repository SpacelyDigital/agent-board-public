"""Extract a terminal reply marker from one bounded, current ChatGPT turn.

The provider returns message text with this route. It remains transient: only
the allowlisted lifecycle fields below leave this module. No older pages are
requested and neither raw message IDs nor response bodies are retained.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid

from privacy_network import ProviderRequestError, RejectRedirects

MAX_RESPONSE_BYTES = 3 * 1024 * 1024
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _identifier(value):
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        result = str(uuid.UUID(value))
    except ValueError:
        return None
    return result if result == value.lower() else None


def _credential(value):
    return isinstance(value, str) and 0 < len(value) <= 16384 and all(32 < ord(c) < 127 for c in value)


def _timestamp(value, now):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and 0 < value <= now + 2 else None


def parse_last_reply(data, conversation_id: str, *, now: float | None = None) -> dict | None:
    """Require the exact current leaf, not another completed branch or tool turn."""
    now = time.time() if now is None else now
    identifier = _identifier(conversation_id)
    if (identifier is None or not isinstance(data, dict)
            or data.get("conversation_id") != identifier
            or data.get("is_archived") is not False
            or data.get("is_temporary_chat") is not False
            or data.get("mapping") is not None):
        raise ProviderRequestError("response")
    messages, current = data.get("messages"), data.get("current_node")
    page = data.get("page_info")
    if (not isinstance(messages, list) or len(messages) > 64
            or not isinstance(current, str) or not _OPAQUE_ID.fullmatch(current)
            or not isinstance(page, dict) or page.get("has_next_page") is not False):
        raise ProviderRequestError("response")
    ids = set()
    leaf = None
    for message in messages:
        if not isinstance(message, dict):
            raise ProviderRequestError("response")
        message_id = message.get("id")
        if not isinstance(message_id, str) or not _OPAQUE_ID.fullmatch(message_id) or message_id in ids:
            raise ProviderRequestError("response")
        ids.add(message_id)
        if message_id == current:
            leaf = message
    if leaf is None:
        raise ProviderRequestError("response")
    author, metadata = leaf.get("author"), leaf.get("metadata")
    if (not isinstance(author, dict) or author.get("role") != "assistant"
            or leaf.get("status") != "finished_successfully" or leaf.get("end_turn") is not True
            or leaf.get("channel") != "final" or leaf.get("recipient") != "all"):
        return None
    if not isinstance(metadata, dict):
        raise ProviderRequestError("response")
    # Do not promote hidden, interrupted, partial, or continued messages.
    for field in ("is_error", "is_visually_hidden_from_conversation", "is_merged",
                  "is_fragment", "continuation_id", "continuation"):
        if metadata.get(field) is not None and metadata.get(field) is not False:
            return None
    if data.get("context_truncation_continuation") is not None:
        return None
    finish = metadata.get("finish_details")
    if finish is not None and (not isinstance(finish, dict) or finish.get("type") != "stop"):
        return None
    created = _timestamp(leaf.get("create_time"), now)
    updated = _timestamp(leaf.get("update_time"), now)
    # The installed client's message merge accepts null update_time and uses
    # the explicit terminal status independently. Preserve null, not a made-up
    # finish timestamp; create_time only orders the stable reply identity.
    if (created is None or leaf.get("update_time") is not None and updated is None
            or updated is not None and updated < created):
        raise ProviderRequestError("response")
    return {"reply_id": hashlib.sha256(current.encode()).hexdigest(),
            "created_at": created, "updated_at": updated}


def read_last_reply(conversation_id: str, token: str, account_id: str | None,
                    *, timeout: float = 8) -> dict | None:
    """GET the verified latest-turn route with the existing account grant only."""
    identifier = _identifier(conversation_id)
    if (identifier is None or not _credential(token)
            or (account_id is not None and not _credential(account_id))
            or type(timeout) not in (int, float) or not 0 < timeout <= 10):
        raise ProviderRequestError("request")
    url = "https://chatgpt.com/backend-api/conversations/" + identifier + "?num_turns=1"
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "AgentBoard/0.2.5"}
    if account_id is not None:
        headers["ChatGPT-Account-ID"] = account_id
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), RejectRedirects(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderRequestError("oversized")
        return parse_last_reply(json.loads(raw), identifier)
    except ProviderRequestError:
        raise
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise ProviderRequestError("http", status) from None
    except (OSError, ValueError, UnicodeError, RecursionError, urllib.error.URLError, http.client.HTTPException):
        raise ProviderRequestError("unavailable") from None
