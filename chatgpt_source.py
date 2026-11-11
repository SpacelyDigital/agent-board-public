"""Read ChatGPT conversation identities from the desktop app's local catalog.

Only catalog metadata is selected. The catalog is a cache, so an absent status
does not mean a completed reply and its presence does not prove a live network
connection. Conversation bodies, credentials and provider writes are unused.
"""

from __future__ import annotations

from contextlib import closing
import math
from pathlib import Path
import sqlite3
import time

from chat_links import DESKTOP_CHAT_ID, DESKTOP_CHAT_ID_PREFIX, DESKTOP_CHAT_URLS


MAX_ROWS = 5000
QUERY_SECONDS = 0.5
SNAPSHOT_MAX_AGE = 15


def _timestamp(value, now):
    return (float(value) if type(value) in (int, float) and math.isfinite(value)
            and 0 < value <= now + 2 else None)


class ChatGPTSource:
    provider = "chatgpt"
    name = "ChatGPT"
    source_id = "chatgpt_chats"

    def __init__(self, catalog_path: Path | None = None):
        self.path = (Path(catalog_path) if catalog_path is not None else
                     Path.home() / ".codex/sqlite/codex.db")
        self.latest_metadata: dict[str, dict] = {}
        self.tracked_ids: set[str] = set()
        self.available = False
        self.observed_at = None

    def _read(self, now):
        deadline = time.monotonic() + QUERY_SECONDS
        with closing(sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True,
                                     timeout=QUERY_SECONDS)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = db.execute("""SELECT thread_id,display_title,source_created_at,
                source_updated_at,chatgpt_async_status,observation_sequence
                FROM local_thread_catalog
                WHERE source_kind='chatgpt' AND missing_candidate=0
                ORDER BY source_recency_at DESC,source_updated_at DESC,thread_id
                LIMIT ?""", (MAX_ROWS + 1,)).fetchall()
        if len(rows) > MAX_ROWS:
            raise ValueError("The local chat catalog exceeds the supported read bound.")
        result = {}
        for row in rows:
            raw_id = row["thread_id"]
            if not isinstance(raw_id, str) or not DESKTOP_CHAT_ID.fullmatch(raw_id.lower()):
                continue
            updated = _timestamp(row["source_updated_at"], now)
            created = _timestamp(row["source_created_at"], now)
            if updated is None:
                continue
            title = row["display_title"]
            if (not isinstance(title, str) or len(title) > 4096
                    or any(0xd800 <= ord(char) <= 0xdfff for char in title)):
                continue
            title = " ".join(title.split())[:160] or "ChatGPT chat"
            ident = DESKTOP_CHAT_ID_PREFIX + raw_id.lower()
            state = row["chatgpt_async_status"]
            state = state if type(state) is int else None
            sequence = row["observation_sequence"]
            sequence = sequence if type(sequence) is int and sequence >= 0 else 0
            candidate = {"id": ident, "title": title, "source_updated_at": updated,
                         "source_created_at": created, "async_status": state,
                         "observation_sequence": sequence}
            previous = result.get(ident)
            # A ChatGPT UUID is the conversation identity across hosts. A
            # conflicting equally recent cache entry cannot prove its state.
            if previous and previous["source_updated_at"] == updated:
                if previous["async_status"] != state:
                    candidate["async_status"] = None
                if previous["observation_sequence"] > sequence:
                    candidate["title"] = previous["title"]
            if previous is None or previous["source_updated_at"] <= updated:
                result[ident] = candidate
        return result

    @staticmethod
    def _fields(item):
        ident = item["id"]
        return {"key": "chatgpt:" + ident, "provider": "chatgpt", "source_kind": "desktop_chat",
                "id": ident, "title": item["title"], "can_open": True,
                "open_label": "Open ChatGPT", "open_kind": "url",
                "open_url": DESKTOP_CHAT_URLS["chatgpt"] + ident[len(DESKTOP_CHAT_ID_PREFIX):]}

    def scan(self, now, cutoff):
        try:
            current = self._read(now)
            # The catalog has no per-row observation timestamp. Even a cached
            # STREAMING or UNREAD enum cannot establish a live response. Only
            # refresh names and links of already tracked records here; a
            # separate exact-ID status reader owns their lifecycle.
            observations = [{**self._fields(item), "status": "disconnected",
                "updated_at": item["source_updated_at"],
                "event_id": f"chatgpt-catalog:{ident}:{item['source_updated_at']}",
                "metadata_only": True, "discovery_eligible": False,
                "detail": "No current reply status was recorded."}
                for ident, item in current.items() if ident in self.tracked_ids]
            self.latest_metadata = current
            self.available = True
            self.observed_at = now
            return observations, self.health()
        except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
            self.latest_metadata.clear()
            self.available = False
            return [], self.health()

    def open_chats(self, now):
        if (not self.available or self.observed_at is None
                or not 0 <= now - self.observed_at <= SNAPSHOT_MAX_AGE):
            return {}
        return {ident: {**self._fields(item), "open_only": True, "catalog_only": True, "status": "disconnected",
                       "updated_at": item["source_updated_at"], "notification_eligible": False,
                       "detail": "Saved in ChatGPT. No current reply status was confirmed."}
                for ident, item in self.latest_metadata.items()}

    def recent_chats(self, limit=12):
        """Bound the candidate set for a separate exact-ID status request."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Choose between 1 and 50 recent chats.")
        return [dict(item) for item in sorted(self.latest_metadata.values(),
                    key=lambda item: (item["source_updated_at"], item["id"]), reverse=True)[:limit]]

    def health(self):
        return {"id": self.source_id, "name": "ChatGPT chats", "available": self.available,
                "detail": ("Reading saved ChatGPT conversations from the local catalog."
                           if self.available else "The local ChatGPT conversation catalog could not be read."),
                "last_seen": self.observed_at if self.available else None}
