"""Build and validate links that only select an existing conversation."""

import re
from urllib.parse import parse_qs, urlsplit

_CODEX_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_CLAUDE_SESSION = re.compile(r"local_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_CLAUDE_PROJECT = re.compile(r"/project/chan_[A-Za-z0-9_]{6,120}\Z")
_CLAUDE_THREAD = re.compile(r"cmsg_[A-Za-z0-9_]{6,120}\Z")
DESKTOP_CHAT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
DESKTOP_CHAT_ID_PREFIX = "desktop_chat:"
DESKTOP_CHAT_URLS = {"claude": "https://claude.ai/chat/", "chatgpt": "https://chatgpt.com/c/"}


class UnsupportedChatLink(ValueError):
    pass


def chat_url(session):
    provider = session.get("provider")
    if provider == "codex":
        ident = session.get("id")
        if not isinstance(ident, str) or not _CODEX_ID.fullmatch(ident) or ident == "new":
            raise UnsupportedChatLink("This Codex conversation has no valid chat identifier.")
        return "codex://threads/" + ident
    value = session.get("open_url")
    if provider in ("claude", "chatgpt") and session.get("source_kind") == "desktop_chat":
        ident = session.get("id")
        if isinstance(ident, str) and ident.startswith(DESKTOP_CHAT_ID_PREFIX):
            chat_id = ident[len(DESKTOP_CHAT_ID_PREFIX):]
            if DESKTOP_CHAT_ID.fullmatch(chat_id) and value == DESKTOP_CHAT_URLS[provider] + chat_id:
                return value
        raise UnsupportedChatLink("The conversation link is invalid.")
    if provider != "claude":
        raise UnsupportedChatLink("This conversation provider is not supported.")
    if not isinstance(value, str) or len(value) > 1000:
        raise UnsupportedChatLink("This Claude session could not be matched to an existing desktop chat.")
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
    except ValueError:
        raise UnsupportedChatLink("The Claude conversation link is invalid.") from None
    if (parsed.scheme == "claude" and parsed.netloc == "code" and _CLAUDE_PROJECT.fullmatch(parsed.path)
            and not parsed.fragment and set(query) <= {"thread"}
            and (not query or len(query["thread"]) == 1 and _CLAUDE_THREAD.fullmatch(query["thread"][0]))):
        return "claude://code" + parsed.path + ("?thread=" + query["thread"][0] if query else "")
    if (parsed.scheme != "claude" or parsed.netloc != "code" or parsed.path != "/continue"
            or parsed.fragment or set(query) != {"session"} or len(query["session"]) != 1
            or not _CLAUDE_SESSION.fullmatch(query["session"][0])):
        raise UnsupportedChatLink("The Claude conversation link is invalid.")
    # Rebuild a canonical URI with only the verified session selector. The
    # desktop app supports other parameters that could submit an initial prompt.
    return "claude://code/continue?session=" + query["session"][0]


def open_command(session):
    url = chat_url(session)
    if session.get("source_kind") == "desktop_chat" and session.get("provider") in ("claude", "chatgpt"):
        return ["/usr/bin/open", url]
    bundle = "com.openai.codex" if session["provider"] == "codex" else "com.anthropic.claudefordesktop"
    return ["/usr/bin/open", "-b", bundle, url]
