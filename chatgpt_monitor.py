"""Join exact local and provider conversation IDs with reply-status readings."""

import hashlib
import math
import re
import time

from chat_links import DESKTOP_CHAT_ID, DESKTOP_CHAT_ID_PREFIX
from claude_chat_monitor import _PrivateCheckpoint


class _InvalidCheckpoint(ValueError):
    pass


class ChatGPTConversationSource:
    provider = "chatgpt"
    name = "ChatGPT"
    source_id = "chatgpt_chats"

    def __init__(self, catalog, status_reader, *, state_path=None, clock=time.time):
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
        self._activation_at = clock()
        self._marker_mode = state_path is not None
        self._checkpoint = None
        self._checkpoint_error = False
        self._checkpoint_dirty = False
        self._checkpoint_unavailable = False
        self._baseline = None
        self._loaded_baseline = None
        if self._marker_mode:
            try:
                self._checkpoint = _PrivateCheckpoint(state_path)
                self._loaded_baseline = self._checkpoint.load()
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                self._checkpoint_error = True

    @staticmethod
    def _time(value, now):
        return (float(value) if type(value) in (int, float) and 0 < value <= now + 2
                and math.isfinite(value) else None)

    def _prepare_baseline(self, store, now):
        scope = self.account_identity
        if scope is None:
            return
        if self._baseline is None and self._loaded_baseline is not None:
            raw = self._loaded_baseline
            if (not isinstance(raw, dict) or raw.get("version") != 1
                    or not isinstance(raw.get("account_scope"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", raw["account_scope"])
                    or self._time(raw.get("initialized_at"), now) is None
                    or not isinstance(raw.get("chats"), dict) or len(raw["chats"]) > 5000):
                raise _InvalidCheckpoint("Invalid chat checkpoint")
            rows = {}
            for ident, row in raw["chats"].items():
                if not isinstance(ident, str) or not DESKTOP_CHAT_ID.fullmatch(ident) or not isinstance(row, dict):
                    raise _InvalidCheckpoint("Invalid chat checkpoint identity")
                marker = row.get("reply_id")
                if marker is not None and (not isinstance(marker, str) or not re.fullmatch(r"[0-9a-f]{64}", marker)):
                    raise _InvalidCheckpoint("Invalid chat checkpoint marker")
                record = {"reply_id": marker}
                for field in ("created_at", "updated_at", "pending_started_at"):
                    value = row.get(field)
                    normalized = self._time(value, now)
                    if value is not None and normalized is None:
                        raise _InvalidCheckpoint("Invalid chat checkpoint timestamp")
                    record[field] = normalized
                if marker is not None and (record["created_at"] is None or
                        record["updated_at"] is not None and record["created_at"] > record["updated_at"]):
                    raise _InvalidCheckpoint("Invalid chat checkpoint reply")
                rows[ident] = record
            self._baseline = {"version": 1, "account_scope": raw["account_scope"],
                              "initialized_at": raw["initialized_at"], "chats": rows}
            self._loaded_baseline = None
        if self._baseline is None or self._baseline["account_scope"] != scope:
            activation = self._activation_at if self._baseline is None else now
            self._baseline = {"version": 1, "account_scope": scope, "initialized_at": activation, "chats": {}}
            self._checkpoint_dirty = True
        for uuid, record in self._baseline["chats"].items():
            started = record["pending_started_at"]
            ident = DESKTOP_CHAT_ID_PREFIX + uuid
            saved = store.get("chatgpt:" + ident) if started is not None and ident not in self.pending else None
            event = f"chatgpt-stream:{uuid}:{scope}:{started}"
            if saved and saved["status"] in {"cooking", "disconnected"} and saved.get("turn_id") == event:
                self.pending[ident] = (started, event)

    def _reply_candidates(self, candidates):
        cutoff = self._baseline["initialized_at"] if self._baseline is not None else self._activation_at
        previous = self._baseline["chats"] if self._baseline is not None else {}
        result = {}
        for item in candidates:
            ident = item["id"]
            uuid = ident[len(DESKTOP_CHAT_ID_PREFIX):]
            marker = previous.get(uuid, {})
            updated = item["source_updated_at"]
            if ident in self.pending or (updated >= cutoff and updated > (marker.get("updated_at") or marker.get("created_at") or 0)):
                result[uuid] = updated
        return result

    def _verified_reply(self, row, now):
        if (not isinstance(row, dict) or row.get("fresh") is not True
                or row.get("account_scope") != self.account_identity
                or not isinstance(row.get("reply_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["reply_id"])):
            return None
        observed = self._time(row.get("observed_at"), now)
        created = self._time(row.get("created_at"), now)
        updated = self._time(row.get("updated_at"), now)
        revision = self._time(row.get("revision"), now)
        if (observed is None or not 0 <= now - observed <= 15 or created is None
                or revision is None
                or row.get("updated_at") is not None and updated is None
                or updated is not None and created > updated):
            return None
        return {"reply_id": row["reply_id"], "created_at": created, "updated_at": updated,
                "observed_at": observed, "revision": revision, "account_scope": self.account_identity}

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
        transaction = {"pending": dict(self.pending), "processed": dict(self.processed),
                       "account": self.account_identity, "committed": False}
        try:
            return self._scan(store, now, transaction)
        except Exception:
            if not transaction["committed"]:
                # A receipt is consumed only after its lifecycle write succeeds.
                # Retry the same fresh observation after a transient DB failure,
                # while never restoring work from a different signed-in account.
                same_account = transaction["account"] == self.account_identity
                self.pending = transaction["pending"] if same_account else {}
                self.processed = transaction["processed"] if same_account else {}
            self.available = False
            self.status_available = False
            self.cooking = {}
            raise

    def _scan(self, store, now, transaction):
        if self._checkpoint_error:
            self.available = False
            self.cooking = {}
            return self.health()
        self.catalog.tracked_ids = store.session_ids("chatgpt")
        _, catalog_health = self.catalog.scan(now, store.activated_at)
        metadata = dict(self.catalog.latest_metadata)
        # scan refreshes account identity and schedules discovery even without
        # any local catalog rows. Newly discovered IDs join the next poll.
        candidates = dict(self.latest_metadata)
        for ident, item in metadata.items():
            previous = candidates.get(ident)
            if previous is None or item["source_updated_at"] > previous["source_updated_at"]:
                candidates[ident] = item
        ordered = sorted(candidates.values(), key=lambda item: (
            item["id"] not in self.pending, -item["source_updated_at"], item["id"]))
        identifiers = [item["id"][len(DESKTOP_CHAT_ID_PREFIX):] for item in ordered]
        readings = (self.status_reader.scan(identifiers, now=now, reply_candidates=self._reply_candidates(ordered[:256]))
                    if self._marker_mode else self.status_reader.scan(identifiers, now=now))
        account_identity = self.status_reader.account_identity()
        if account_identity != self.account_identity:
            self.pending.clear()
            self.processed.clear()
            self.cooking.clear()
            self.account_identity = account_identity
        if self._marker_mode:
            try:
                self._prepare_baseline(store, now)
            except _InvalidCheckpoint:
                self._checkpoint_error = True
                self.available = False
                return self.health()
            except Exception:
                self._checkpoint_unavailable = True
                self.available = False
                self.cooking = {}
                return self.health()
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
                if (saved and saved["status"] in ({"cooking", "disconnected"} if self._marker_mode else {"cooking"})
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
            status = {"IS_STREAMING": "cooking", "COMPLETE": "disconnected" if self._marker_mode else "ready", "FAILURE": "needs_you"}[state]
            detail = {"cooking": "ChatGPT is responding.", "ready": "Review the latest reply in ChatGPT.",
                      "disconnected": "Waiting to confirm the latest ChatGPT reply.",
                      "needs_you": "ChatGPT reported that this reply failed. Open the conversation to continue."}[status]
            observations.append({**self.catalog._fields(item), "status": status, "detail": detail,
                                 "started_at": started, "updated_at": started if status in {"cooking", "disconnected"} else observed,
                                 "event_id": event, "turn_id": event, "confirmed_live": status == "cooking"})
            if status not in {"cooking", "disconnected"}:
                self.pending.pop(ident, None)
        if self._marker_mode and self._baseline is not None:
            records = {uuid: dict(row) for uuid, row in self._baseline["chats"].items()}
            acknowledged = {}
            replies = self.status_reader.completed_replies(now)
            for uuid, raw in replies.items():
                ident = DESKTOP_CHAT_ID_PREFIX + uuid
                item = metadata.get(ident)
                reply = self._verified_reply(raw, now)
                if item is None or reply is None:
                    continue
                latest_status = readings.get(uuid, {})
                if (latest_status.get("status") in {"IS_STREAMING", "FAILURE"}
                        and latest_status.get("observed_at", 0) >= reply["observed_at"]):
                    continue
                acknowledged[uuid] = reply
                prior = records.get(uuid)
                pending = self.pending.get(ident)
                provider_time = reply["updated_at"] if reply["updated_at"] is not None else reply["created_at"]
                progress_matches = pending is not None and provider_time >= pending[0] - 2
                known_prior = prior is not None and prior.get("reply_id") is not None
                # Compare like timestamps. A nullable finish time is never
                # fabricated from creation; creation orders the message only.
                both_finished = known_prior and reply["updated_at"] is not None and prior["updated_at"] is not None
                current_order = reply["updated_at"] if both_finished else reply["created_at"]
                prior_order = (prior["updated_at"] if both_finished else prior["created_at"]) if known_prior else None
                changed = known_prior and reply["reply_id"] != prior["reply_id"] and (
                    current_order > prior_order or current_order == prior_order and progress_matches)
                first = not known_prior
                new_activity = provider_time > self._baseline["initialized_at"] or progress_matches
                # Selecting a newer historical branch changes the current leaf
                # without creating a reply. Advance its baseline silently until
                # the provider marker proves activity after monitoring began.
                if (changed or first) and new_activity:
                    event = f"chatgpt-reply:{uuid}:{self.account_identity}:{reply['reply_id']}"
                    event_time = reply["updated_at"] if reply["updated_at"] is not None else reply["observed_at"]
                    saved = store.get("chatgpt:" + ident)
                    same_pending = pending and saved and saved.get("turn_id") == pending[1] and saved["status"] in {"cooking", "disconnected"}
                    prior_event = f"chatgpt-reply:{uuid}:{self.account_identity}:{prior['reply_id']}" if known_prior else None
                    prior_revision = hashlib.sha256((prior_event + ':ready').encode()).hexdigest()[:24] if prior_event else None
                    same_previous_reply = saved and prior_revision is not None and saved.get("event_id") == prior_revision
                    if same_pending or same_previous_reply:
                        event_time = max(event_time, saved["updated_at"])
                    observations = [row for row in observations if row["id"] != ident]
                    observations.append({**self.catalog._fields(item), "status": "ready",
                        "detail": "Review the latest reply in ChatGPT.",
                        "started_at": pending[0] if pending else reply["created_at"], "updated_at": event_time,
                        "event_id": event, "turn_id": pending[1] if pending else event})
                    self.pending.pop(ident, None)
                    current_cooking.pop(ident, None)
                # Never roll a reply baseline backwards when an old branch is
                # selected. A rename with the same marker also stays quiet.
                if (prior is None or prior.get("reply_id") is None or changed):
                    records[uuid] = {"reply_id": reply["reply_id"], "created_at": reply["created_at"],
                                     "updated_at": reply["updated_at"], "pending_started_at": None}
        store.apply(observations, now)
        transaction["committed"] = True
        self._checkpoint_unavailable = False
        if self._marker_mode and self._baseline is not None:
            for uuid, record in records.items():
                pending = self.pending.get(DESKTOP_CHAT_ID_PREFIX + uuid)
                record["pending_started_at"] = pending[0] if pending else None
            for ident, pending in self.pending.items():
                uuid = ident[len(DESKTOP_CHAT_ID_PREFIX):]
                records.setdefault(uuid, {"reply_id": None, "created_at": None, "updated_at": None})["pending_started_at"] = pending[0]
            if len(records) > 5000:
                records = dict(sorted(records.items(), key=lambda item: (
                    item[1]["pending_started_at"] is not None, item[1]["updated_at"] or 0), reverse=True)[:5000])
            checkpoint = {**self._baseline, "chats": records}
            self._checkpoint_dirty |= checkpoint != self._baseline
            self._baseline = checkpoint
            if self._checkpoint_dirty:
                try:
                    self._checkpoint.save(checkpoint)
                    self._checkpoint_dirty = False
                except (OSError, ValueError):
                    self._checkpoint_unavailable = True
            if not self._checkpoint_dirty:
                self.status_reader.ack_completed_replies(acknowledged)
        self.cooking = current_cooking
        self.latest_metadata = metadata
        self.available = bool(catalog_health.get("available") or discovery_available or self.status_available) and not self._checkpoint_unavailable
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
        if self._checkpoint is not None:
            self._checkpoint.close()

    def health(self):
        detail = ("Monitoring ChatGPT reply status using its conversation IDs."
                  if self.status_available else
                  "ChatGPT conversations are available. Reply status is connecting or unavailable.")
        if not self.available:
            detail = "ChatGPT conversations could not be read. Open ChatGPT and check its existing sign-in."
        if self._checkpoint_error or self._checkpoint_unavailable:
            detail = "The local ChatGPT chat checkpoint could not be read or saved."
        return {"id": self.source_id, "name": "ChatGPT chats", "available": self.available,
                "detail": detail, "last_seen": self.observed_at,
                "status_available": self.status_available}
