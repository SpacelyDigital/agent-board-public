"""Join exact local and provider conversation IDs with reply-status readings."""

import math

from chat_links import DESKTOP_CHAT_ID, DESKTOP_CHAT_ID_PREFIX


class ChatGPTConversationSource:
    provider = "chatgpt"
    name = "ChatGPT"
    source_id = "chatgpt_chats"

    def __init__(self, catalog, status_reader):
        self.catalog = catalog
        self.status_reader = status_reader
        self.cooking = {}
        self.pending = {}
        self.processed = {}
        self.available = False
        self.observed_at = None
        self.status_available = False
        self.latest_metadata = {}
        self.account_identity = None

    @staticmethod
    def _discovered_item(uuid, row, now):
        if (not isinstance(uuid, str) or not DESKTOP_CHAT_ID.fullmatch(uuid)
                or not isinstance(row, dict)):
            return None
        updated = row.get("source_updated_at")
        created = row.get("source_created_at")
        title = row.get("title")
        if (type(updated) not in (float, int) or not math.isfinite(updated)
                or not 0 < updated <= now + 2
                or not isinstance(title, str) or len(title) > 4096
                or any(0xd800 <= ord(char) <= 0xdfff for char in title)):
            return None
        created = (created if type(created) in (float, int) and math.isfinite(created)
                   and 0 < created <= now + 2 else None)
        return {"id": DESKTOP_CHAT_ID_PREFIX + uuid,
                "title": " ".join(title.split())[:160] or "ChatGPT chat",
                "source_updated_at": updated, "source_created_at": created}

    def scan(self, store, now):
        self.catalog.tracked_ids = store.session_ids("chatgpt")
        _, catalog_health = self.catalog.scan(now, store.activated_at)
        metadata = dict(self.catalog.latest_metadata)
        # scan refreshes account identity and schedules discovery even without
        # any local catalog rows. Newly discovered IDs join the next poll.
        candidates = {**self.latest_metadata, **metadata}
        identifiers = [item["id"][len(DESKTOP_CHAT_ID_PREFIX):] for item in
                       sorted(candidates.values(), key=lambda item: (
                           item["id"] not in self.pending, -item["source_updated_at"], item["id"]))]
        readings = self.status_reader.scan(identifiers, now=now)
        account_identity = self.status_reader.account_identity()
        if account_identity != self.account_identity:
            self.pending.clear()
            self.processed.clear()
            self.cooking.clear()
            self.account_identity = account_identity
        discovered = self.status_reader.discovered_chats(now)
        discovery_available = self.status_reader.discovery_available(now)
        for uuid, row in discovered.items():
            item = self._discovered_item(uuid, row, now)
            if item is None:
                continue
            previous = metadata.get(item["id"])
            if previous is None or item["source_updated_at"] >= previous["source_updated_at"]:
                metadata[item["id"]] = item
        # An outage in discovery cannot erase a known in-flight identity. It
        # still needs a fresh, exact-ID status response before it can finish.
        for ident in self.pending:
            if ident not in metadata and ident in self.latest_metadata:
                metadata[ident] = self.latest_metadata[ident]
        metadata_updates = [{**self.catalog._fields(item), "status": "disconnected",
            "updated_at": item["source_updated_at"],
            "event_id": f"chatgpt-catalog:{ident}:{item['source_updated_at']}",
            "metadata_only": True, "discovery_eligible": False,
            "detail": "No current reply status was recorded."}
            for ident, item in metadata.items() if ident in self.catalog.tracked_ids]
        store.apply(metadata_updates, now)
        current_cooking = {}
        observations = []
        self.status_available = False
        for uuid, reading in readings.items():
            ident = DESKTOP_CHAT_ID_PREFIX + uuid
            item = metadata.get(ident)
            if item is None or reading.get("fresh") is not True:
                continue
            observed = reading.get("observed_at")
            if (type(observed) not in (float, int) or not math.isfinite(observed)
                    or not 0 <= now - observed <= 15):
                continue
            state = reading["status"]
            if state not in ("IS_STREAMING", "COMPLETE", "FAILURE"):
                continue
            processed = self.processed.get(ident, 0)
            if observed < processed:
                continue
            self.status_available = True
            if observed == processed:
                previous = self.pending.get(ident)
                if state == "IS_STREAMING" and previous is not None:
                    current_cooking[ident] = (previous[0], item["title"])
                continue
            # Recover only a previously observed stream with the same exact ID.
            # A first-seen COMPLETE is history, not a newly completed response.
            if ident not in self.pending:
                saved = store.get("chatgpt:" + ident)
                stream_prefix = f"chatgpt-stream:{uuid}:{self.account_identity}:"
                if (saved and saved["status"] == "cooking"
                        and self.account_identity is not None
                        and isinstance(saved.get("turn_id"), str)
                        and saved["turn_id"].startswith(stream_prefix)
                        and type(saved.get("started_at")) in (float, int)
                        and math.isfinite(saved["started_at"]) and 0 < saved["started_at"] <= now):
                    self.pending[ident] = (saved["started_at"], saved["turn_id"])
            previous = self.pending.get(ident)
            if state == "IS_STREAMING":
                if previous is None:
                    previous = (observed, f"chatgpt-stream:{uuid}:{self.account_identity}:{observed}")
                    self.pending[ident] = previous
                current_cooking[ident] = (previous[0], item["title"])
            self.processed[ident] = observed
            if previous is None:
                continue
            started, event = previous
            status = {"IS_STREAMING": "cooking", "COMPLETE": "ready", "FAILURE": "needs_you"}[state]
            detail = {"cooking": "ChatGPT is responding.", "ready": "Review the latest reply in ChatGPT.",
                      "needs_you": "ChatGPT reported that this reply failed. Open the conversation to continue."}[status]
            observations.append({**self.catalog._fields(item), "status": status, "detail": detail,
                                 "started_at": started, "updated_at": started if status == "cooking" else observed,
                                 "event_id": event, "turn_id": event, "confirmed_live": status == "cooking"})
            if status != "cooking":
                self.pending.pop(ident, None)
        store.apply(observations, now)
        self.cooking = current_cooking
        self.latest_metadata = metadata
        self.available = bool(catalog_health.get("available") or discovery_available or self.status_available)
        self.observed_at = now if self.available else self.observed_at
        return self.health()

    def open_chats(self, now):
        # Catalog-only rows deliberately do not claim to be open or completed.
        if (not self.available or self.observed_at is None
                or not 0 <= now - self.observed_at <= 15):
            return {}
        return {ident: {**self.catalog._fields(item), "open_only": True, "catalog_only": True,
                       "status": "disconnected", "updated_at": item["source_updated_at"],
                       "notification_eligible": False,
                       "detail": "Saved in ChatGPT. No current reply status was confirmed."}
                for ident, item in self.latest_metadata.items()}

    def close(self):
        self.status_reader.close()

    def health(self):
        detail = ("Monitoring ChatGPT reply status using its conversation IDs."
                  if self.status_available else
                  "ChatGPT conversations are available. Reply status is connecting or unavailable.")
        if not self.available:
            detail = "ChatGPT conversations could not be read. Open ChatGPT and check its existing sign-in."
        return {"id": self.source_id, "name": "ChatGPT chats", "available": self.available,
                "detail": detail, "last_seen": self.observed_at,
                "status_available": self.status_available}
