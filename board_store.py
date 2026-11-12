"""The board owns acknowledgements, never the providers' conversations."""

import hashlib
import fcntl
import json
import math
import re
import os
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from chat_links import DESKTOP_CHAT_ID, DESKTOP_CHAT_ID_PREFIX

STATUSES = {"cooking", "needs_you", "ready", "disconnected"}
CHATGPT_WINDOW_ID = re.compile(r"window:[0-9a-f]{32}\Z")
OBSERVATION_PERSIST_SECONDS = 60


def valid_desktop_chat_id(provider, ident):
    return isinstance(ident, str) and (
        DESKTOP_CHAT_ID.fullmatch(ident) is not None
        or provider == "chatgpt" and CHATGPT_WINDOW_ID.fullmatch(ident) is not None)


class Conflict(Exception):
    pass


class BoardStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._db = None
        self._closed = False
        self._observed_at = {}
        self.recovery_notice = None
        # Independent app launches must not both quarantine the same database.
        with self._initialization_lock():
            self._private_database_files()
            try:
                try:
                    self._initialize()
                except sqlite3.DatabaseError as exc:
                    self._close_connection()
                    # Busy/full/permission/IO failures do not mean that owner
                    # data is corrupt. Only explicit corruption permits recovery.
                    if getattr(exc, "sqlite_errorcode", 0) & 0xff not in (11, 26):
                        raise
                    self._recover_corrupt_database()
                    self._initialize()
                if hasattr(self._db, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"):
                    self._db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, False)
                self._retire_legacy_chatgpt_windows()
            except BaseException:
                self._close_connection()
                raise

    def _retire_legacy_chatgpt_windows(self):
        """Keep pre-verification window observations without replaying their alerts."""
        marker = "chatgpt_provider_identity_v2"
        with self.connect() as db:
            if db.execute("SELECT 1 FROM metadata WHERE key=?", (marker,)).fetchone():
                return
            rows = [row for row in db.execute("SELECT * FROM sessions WHERE provider='chatgpt'")
                    if row["id"].startswith(DESKTOP_CHAT_ID_PREFIX)
                    and CHATGPT_WINDOW_ID.fullmatch(row["id"][len(DESKTOP_CHAT_ID_PREFIX):])
                    and self._extra(row["extra"]).get("source_kind") == "desktop_chat"]
            if rows:
                # These IDs were created from a window and title, without proof
                # of the selected product. They cannot safely be merged by title.
                recovery = self._new_recovery_directory()
                backup_path = recovery / "board-before-chat-identity-repair.sqlite3"
                descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(descriptor)
                backup = sqlite3.connect(backup_path)
                try:
                    db.backup(backup)
                finally:
                    backup.close()
                for row in rows:
                    extra = self._extra(row["extra"])
                    extra["legacy_identity_unverified"] = True
                    db.execute("UPDATE sessions SET extra=? WHERE key=?", (json.dumps(extra), row["key"]))
            db.execute("INSERT INTO metadata VALUES (?,?)", (marker, str(time.time())))

    @contextmanager
    def _initialization_lock(self):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(self.path.parent / ".board-initialize.lock", flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise PermissionError("The board initialization lock is not a private regular file.")
            os.fchmod(descriptor, 0o600)
            # A second process may be starting or recovering the same board. A
            # deadline avoids an indefinite launch hang if that process stalls.
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Another Agent Board process is initializing the local database. Retry after it exits.")
                    time.sleep(0.05)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _private_database_files(self):
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            if not suffix:
                flags |= os.O_CREAT
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileNotFoundError:
                if suffix:
                    continue
                raise
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise PermissionError("The board database must be your own regular private file.")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)

    def _new_recovery_directory(self):
        recovery_root = self.path.parent / "recovery"
        recovery_root.mkdir(mode=0o700, exist_ok=True)
        info = recovery_root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError("The board recovery directory must be your own private directory.")
        os.chmod(recovery_root, 0o700)
        recovery = recovery_root / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        recovery.mkdir(mode=0o700)
        return recovery

    def _recover_corrupt_database(self):
        self._close_connection()
        recovery = self._new_recovery_directory()
        copies = recovery / "copies"
        copies.mkdir(mode=0o700)
        originals = [Path(str(self.path) + suffix) for suffix in ("", "-wal", "-shm")
                     if Path(str(self.path) + suffix).exists()]
        versions = {}
        for original in originals:
            before = original.stat()
            versions[original] = (before.st_ino, before.st_size, before.st_mtime_ns)
            destination = copies / original.name
            with original.open("rb") as source, destination.open("xb") as target:
                os.fchmod(target.fileno(), 0o600)
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        for original, version in versions.items():
            after = original.stat()
            if (after.st_ino, after.st_size, after.st_mtime_ns) != version:
                raise OSError("The damaged database changed during recovery. The original files were not replaced.")
        replacement = recovery / "replacement.sqlite3"
        replacement_store = BoardStore(replacement)
        notice = ("The local board database was damaged. A new board was created, and the original files "
                  f"were preserved at {recovery}. Reviewed and renamed states may need to be restored from a backup.")
        try:
            with replacement_store.connect() as db:
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('recovery_notice', ?)", (notice,))
        finally:
            # Flush and close the replacement WAL before moving its main file.
            replacement_store.close()
        moved = []
        try:
            for original in originals:
                destination = recovery / original.name
                os.replace(original, destination)
                moved.append((original, destination))
            os.replace(replacement, self.path)
        except OSError:
            # Completed copies remain available even if restoring a moved file
            # also fails. Never open a fresh board after an incomplete recovery.
            for original, destination in reversed(moved):
                os.replace(destination, original)
            raise

    @staticmethod
    def _extra(value):
        try:
            result = json.loads(value)
            if not isinstance(result, dict):
                return {}
            normalized = BoardStore._normalize_extra(result)
            revision = BoardStore._history_revision(result.get("history_revision"))
            if revision:
                normalized["history_revision"] = revision
            attention = result.get("attention_since")
            if BoardStore._finite_number(attention):
                normalized["attention_since"] = attention
            # This binding is assigned only by apply after verified reply
            # evidence. Decode it from owned stored state, but never accept it
            # through _normalize_extra on a new source observation.
            reply_event = result.get("project_reply_event")
            reply_id = normalized.get("project_reply_id")
            if (normalized.get("source_kind") == "project" and normalized.get("project_role") == "thread"
                    and isinstance(reply_id, str) and re.fullmatch(r"cmsg_[A-Za-z0-9_]{6,120}", reply_id)
                    and isinstance(reply_event, str) and re.fullmatch(r"[0-9a-f]{24}", reply_event)):
                normalized["project_reply_event"] = reply_event
                if type(result.get("project_reply_identity_version")) is int and result["project_reply_identity_version"] == 1:
                    normalized["project_reply_identity_version"] = 1
                if result.get("project_reply_baseline") == "legacy_resolved_review":
                    normalized["project_reply_baseline"] = "legacy_resolved_review"
            return normalized
        except (ValueError, TypeError, RecursionError):
            return {}

    def _initialize(self):
        with self.connect() as db:
            # Confirm structural integrity before schema creation or migration
            # can modify a damaged owner database.
            check = db.execute("PRAGMA quick_check(1)").fetchone()
            if check is None or check[0] != "ok":
                failure = sqlite3.DatabaseError("The local board database failed its integrity check.")
                failure.sqlite_errorcode = 11
                raise failure
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS background_sessions (
                    key TEXT PRIMARY KEY, reason TEXT NOT NULL, added_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_preferences (
                    key TEXT PRIMARY KEY, title_override TEXT,
                    placement TEXT NOT NULL DEFAULT 'auto',
                    snoozed_until REAL NOT NULL DEFAULT 0, snoozed_event TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    key TEXT PRIMARY KEY, provider TEXT NOT NULL, id TEXT NOT NULL,
                    title TEXT NOT NULL, cwd TEXT NOT NULL, status TEXT NOT NULL,
                    detail TEXT NOT NULL, started_at REAL, updated_at REAL NOT NULL,
                    observed_at REAL NOT NULL, event_id TEXT NOT NULL,
                    reviewed_event TEXT, reviewed_at REAL, can_open INTEGER NOT NULL,
                    extra TEXT NOT NULL DEFAULT '{}'
                );
            """)
            activated = db.execute("SELECT value FROM metadata WHERE key='activated_at'").fetchone()
            if activated is None:
                self.activated_at = time.time()
                db.execute("INSERT INTO metadata VALUES ('activated_at', ?)", (str(self.activated_at),))
            else:
                try:
                    self.activated_at = float(activated[0])
                    valid = math.isfinite(self.activated_at) and self.activated_at >= 0
                except (TypeError, ValueError, OverflowError):
                    valid = False
                if not valid:
                    # Repair only the invalid discovery timestamp. Existing
                    # chats/preferences remain intact, with a complete backup.
                    recovery = self._new_recovery_directory()
                    backup_path = recovery / "board-before-metadata-repair.sqlite3"
                    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                    os.close(descriptor)
                    backup = sqlite3.connect(backup_path)
                    try:
                        db.backup(backup)
                    finally:
                        backup.close()
                    self.activated_at = time.time()
                    notice = ("The board's saved start time was invalid and has been reset. Existing chats and "
                              f"preferences were kept, and a local database backup was preserved at {backup_path}.")
                    old_notice = db.execute("SELECT value FROM metadata WHERE key='recovery_notice'").fetchone()
                    if old_notice and isinstance(old_notice[0], str):
                        notice = old_notice[0] + " " + notice
                    db.execute("UPDATE metadata SET value=? WHERE key='activated_at'", (str(self.activated_at),))
                    db.execute("INSERT OR REPLACE INTO metadata VALUES ('recovery_notice', ?)", (notice,))
            notice = db.execute("SELECT value FROM metadata WHERE key='recovery_notice'").fetchone()
            self.recovery_notice = notice[0] if notice and isinstance(notice[0], str) else None
            # Older v1 rows could open only the Claude app. Retire that fallback
            # even if their source files have since disappeared. A current source
            # scan will restore a real link when it finds the desktop session.
            for row in db.execute("SELECT * FROM sessions WHERE provider='claude'").fetchall():
                extra = self._extra(row["extra"])
                if extra.get("open_kind") != "app":
                    continue
                title = row["title"]
                project = Path(row["cwd"]).name
                slug = re.sub(r"[^a-z0-9]+", "-", project.casefold()).strip("-")
                generic = title.casefold().startswith("remote control session") or (slug and re.fullmatch(re.escape(slug) + r"-[a-z0-9]{1,8}", title.casefold()))
                if generic and row["updated_at"] > 0:
                    label = project.replace("-", " ").replace("_", " ").capitalize() or "Claude"
                    title = label + " · " + datetime.fromtimestamp(row["started_at"] or row["updated_at"]).strftime("%b %d, %I:%M %p").replace(", 0", ", ")
                    extra["title_source"] = "last_known_activity"
                extra.update(open_kind="unavailable", open_error="The original desktop session mapping is no longer available.")
                extra.pop("open_url", None)
                db.execute("UPDATE sessions SET title=?,can_open=0,extra=? WHERE key=?", (title, json.dumps(extra), row["key"]))

    @contextmanager
    def connect(self):
        with self.lock:
            if self._closed:
                raise sqlite3.ProgrammingError("The board database is closed.")
            if self._db is None:
                self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
                self._db.row_factory = sqlite3.Row
                # A failed integrity check must not checkpoint or delete the
                # original WAL before corruption recovery preserves its bytes.
                if hasattr(self._db, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"):
                    self._db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
                # Keep owner actions durable on power loss. Avoiding redundant
                # writes removes churn without weakening synchronous commits.
                self._db.execute("PRAGMA synchronous=FULL")
                self._db.execute("PRAGMA temp_store=MEMORY")
            db = self._db
            with db:
                yield db

    def _close_connection(self):
        with self.lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def close(self):
        with self.lock:
            self._close_connection()
            self._closed = True

    @staticmethod
    def _changed(previous, values, now=None):
        if previous is None:
            return True
        for field, value in values.items():
            if field == "observed_at" or previous[field] == value:
                continue
            if field == "extra":
                try:
                    if json.loads(previous[field]) == json.loads(value):
                        continue
                except (ValueError, TypeError, RecursionError):
                    pass
            return True
        return now is not None and now - previous["observed_at"] >= OBSERVATION_PERSIST_SECONDS

    def _fresh_row(self, row):
        if row is None:
            return None
        result = dict(row)
        result["observed_at"] = max(row["observed_at"], self._observed_at.get(row["key"], 0))
        return result

    def apply(self, observations, now=None):
        now = now if now is not None else time.time()
        with self.lock:
            with self.connect() as db:
                observed = self._apply(db, observations, now)
            # Only committed observations can refresh confidence. A failed
            # transaction must neither publish freshness nor consume a retry.
            for key, stamp in observed.items():
                self._observed_at[key] = max(stamp, self._observed_at.get(key, 0))

    def _apply(self, db, observations, now):
        observed = {}
        background = {row["key"]: row["reason"] for row in db.execute("SELECT key,reason FROM background_sessions")}
        for source in observations:
            obs = self.normalize(source, now)
            if obs is None:
                continue
            previous = db.execute("SELECT * FROM sessions WHERE key=?", (obs["key"],)).fetchone()
            if previous is None:
                if source.get("discovery_eligible") is False:
                    continue
                # Start with work in progress and events since this board was enabled.
                # Opening the board never imports years of completed conversations.
                fresh_event = obs["updated_at"] >= self.activated_at
                active_work = obs["status"] in ("cooking", "needs_you") and now - obs["updated_at"] < 86400
                confirmed_live = source.get("confirmed_live") is True and obs["status"] in ("cooking", "needs_you")
                pending_review = source.get("source_kind") == "project" and source.get("pending_review") is True and obs["status"] == "ready"
                project_history = source.get("source_kind") == "project" and source.get("background_reason") in ("older_project_thread", "project_overview")
                if not (fresh_event or active_work or confirmed_live or pending_review or project_history):
                    continue
            # Routing is decided by the source. A later valid Desktop
            # mapping can move an ordinary chat back to the main board.
            if source.get("background_reason"):
                reason = str(source["background_reason"])[:200]
                if background.get(obs["key"]) != reason:
                    db.execute("""INSERT INTO background_sessions VALUES (?,?,?)
                        ON CONFLICT(key) DO UPDATE SET reason=excluded.reason""",
                        (obs["key"], reason, now))
                    background[obs["key"]] = reason
            elif source.get("background_routing_uncertain") is not True and obs["key"] in background:
                db.execute("DELETE FROM background_sessions WHERE key=?", (obs["key"],))
                background.pop(obs["key"])
            if previous and source.get("history_checking") is True:
                # Indexing confidence is independent of the last lifecycle.
                # In particular, never manufacture a turn or lose its review
                # and snooze identities while older records are being read.
                extra = self._extra(previous["extra"])
                extra["history_checking"] = True
                extra["history_checking_detail"] = obs["detail"]
                if previous["status"] in ("ready", "needs_you"):
                    if extra.get("history_quiet_event") != previous["event_id"]:
                        extra["history_quiet_at"] = now
                    extra["history_quiet_event"] = previous["event_id"]
                values = {"extra": json.dumps(extra)}
                if self._changed(previous, values, now):
                    db.execute("UPDATE sessions SET extra=?,observed_at=? WHERE key=?",
                               (values["extra"], now, obs["key"]))
                observed[obs["key"]] = now
                continue
            source_extra = self._extra(obs["extra"])
            previous_extra = self._extra(previous["extra"]) if previous else {}
            same_reply_event = self._verified_project_reply_event(db, previous, obs, source, source_extra, previous_extra)
            if same_reply_event is not None:
                obs["event_id"] = same_reply_event
            same_project = (obs["provider"] == "claude" and source_extra.get("source_kind") == "project"
                and source_extra.get("project_role") == "thread"
                and source_extra.get("project_thread_id") == previous_extra.get("project_thread_id")
                and source_extra.get("project_channel_id") == previous_extra.get("project_channel_id"))
            if self._verified_project_reply(source, obs, source_extra):
                source_extra["project_reply_event"] = obs["event_id"]
                source_extra["project_reply_identity_version"] = 1
            elif same_project and previous_extra.get("project_reply_id") and previous_extra.get("project_reply_event"):
                # A working/blocked observation is not a new completed reply.
                # Retain only the last verified reply binding through that work.
                source_extra["project_reply_id"] = previous_extra["project_reply_id"]
                source_extra["project_reply_event"] = previous_extra["project_reply_event"]
                for field in ("project_reply_identity_version", "project_reply_baseline"):
                    if field in previous_extra:
                        source_extra[field] = previous_extra[field]
            obs["extra"] = json.dumps(source_extra)
            revision = source_extra.get("history_revision")
            prior_revision = previous_extra.get("history_revision")
            same_history_bytes = bool(revision and prior_revision
                and revision["size"] == prior_revision.get("size")
                and revision["sha256"] == prior_revision.get("sha256"))
            rebased = bool(revision and prior_revision and not same_history_bytes
                           and revision["generation"] != prior_revision.get("generation"))
            project_observed = source.get("project_status_observed_at")
            current_project_work = bool(previous and obs["provider"] == "claude"
                and source_extra.get("source_kind") == previous_extra.get("source_kind") == "project"
                and source.get("confirmed_live") is True and source.get("metadata_only") is not True
                and obs["status"] in ("cooking", "needs_you")
                and self._finite_number(project_observed)
                and 0 <= now - project_observed <= 35
                and project_observed >= max(previous["observed_at"],
                                            self._observed_at.get(obs["key"], 0),
                                            observed.get(obs["key"], 0)))
            if current_project_work and obs["updated_at"] < previous["updated_at"]:
                # Project last_reply_at describes a message, not when work
                # resumed or a live prompt appeared. A bounded fresh status
                # read may supersede old attention without moving message
                # time backwards. Ready and local-history reads still use
                # the existing older-event protection below.
                obs["updated_at"] = previous["updated_at"]
            if previous and (source.get("metadata_only") is True
                             or (obs["updated_at"] < previous["updated_at"] and not rebased)):
                # A title or desktop-session mapping can improve while the
                # source's lifecycle record is older than a cached state.
                # Refresh display/navigation metadata without reverting work
                # status, turn identity, or the owner's acknowledgement.
                extra = self._extra(previous["extra"])
                if revision:
                    extra["history_revision"] = revision
                    if obs["event_id"] == previous["event_id"]:
                        extra.pop("history_checking", None)
                        extra.pop("history_checking_detail", None)
                    elif not prior_revision:
                        # Older installations have no physical checkpoint.
                        # Establish one without claiming that a conflicting
                        # backdated lifecycle supersedes the cached turn.
                        extra["history_checking"] = True
                        extra["history_checking_detail"] = "The current local history conflicts with a newer saved status. Waiting for confirmed new activity."
                        if previous["status"] in ("ready", "needs_you"):
                            extra["history_quiet_event"] = previous["event_id"]
                            extra.setdefault("history_quiet_at", now)
                for field in ("open_url", "open_label", "open_kind", "title_source", "open_error", "project_name", "project_channel_id", "project_role", "project_thread_id"):
                    if field in source_extra:
                        extra[field] = source_extra[field]
                    else:
                        extra.pop(field, None)
                values = {"title": obs["title"], "cwd": obs["cwd"],
                          "can_open": obs["can_open"], "extra": json.dumps(extra)}
                if self._changed(previous, values):
                    db.execute("UPDATE sessions SET title=?,cwd=?,can_open=?,extra=? WHERE key=?",
                               (*values.values(), obs["key"]))
                continue
            extra = self._extra(obs["extra"])
            if previous:
                old_extra = self._extra(previous["extra"])
                if old_extra.get("history_quiet_event") == obs["event_id"]:
                    extra["history_quiet_event"] = old_extra["history_quiet_event"]
                    extra["history_quiet_at"] = old_extra.get("history_quiet_at", now)
            if obs["status"] in ("ready", "needs_you"):
                old_extra = self._extra(previous["extra"]) if previous else {}
                extra["attention_since"] = (old_extra.get("attention_since", previous["updated_at"])
                    if previous and previous["event_id"] == obs["event_id"] else obs["updated_at"])
            obs["extra"] = json.dumps(extra)
            if self._changed(previous, obs, now):
                db.execute("""
                    INSERT INTO sessions
                    (key,provider,id,title,cwd,status,detail,started_at,updated_at,observed_at,event_id,can_open,extra)
                    VALUES (:key,:provider,:id,:title,:cwd,:status,:detail,:started_at,:updated_at,:observed_at,:event_id,:can_open,:extra)
                    ON CONFLICT(key) DO UPDATE SET
                      title=excluded.title, cwd=excluded.cwd, status=excluded.status,
                      detail=excluded.detail, started_at=excluded.started_at,
                      updated_at=excluded.updated_at, observed_at=excluded.observed_at,
                      event_id=excluded.event_id, can_open=excluded.can_open, extra=excluded.extra
                """, obs)
            observed[obs["key"]] = now

        return observed

    @staticmethod
    def _verified_project_reply(source, obs, extra):
        thread, reply = extra.get("project_thread_id"), extra.get("project_reply_id")
        revision = source.get("_project_reply_revision")
        if (obs["provider"] != "claude" or obs["status"] != "ready"
                or source.get("project_reply_verified") is not True
                or source.get("metadata_only") is True
                or extra.get("source_kind") != "project" or extra.get("project_role") != "thread"
                or not isinstance(thread, str) or not re.fullmatch(r"cmsg_[A-Za-z0-9_]{6,120}", thread)
                or not isinstance(reply, str) or not re.fullmatch(r"cmsg_[A-Za-z0-9_]{6,120}", reply)
                or reply != source.get("_completion_reply_id")
                or not isinstance(revision, list) or len(revision) != 3 or revision[0] != thread
                or extra.get("preview_at") != obs["updated_at"]
                or extra.get("preview_kind") != "reply" or not extra.get("preview")):
            return False
        try:
            raw_stamp = revision[1]
            if isinstance(raw_stamp, str) and len(raw_stamp) <= 100:
                stamp = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00")).timestamp()
            elif BoardStore._finite_number(raw_stamp):
                stamp = raw_stamp / 1000 if raw_stamp > 100_000_000_000 else raw_stamp
            else:
                return False
            if stamp != obs["updated_at"]:
                return False
        except (ValueError, OverflowError, OSError):
            return False
        count = revision[2]
        return bool(count is None or type(count) is int and count >= 0 or isinstance(count, str)
                    and re.fullmatch(r"[0-9]{1,12}", count))

    def _verified_project_reply_event(self, db, previous, obs, source, extra, previous_extra):
        """Keep an exact acknowledged assistant reply across old event formats.

        Old idle-completion events included the assistant message ID. A legacy
        resolved Review can establish a first-ID baseline only for its exact
        native revision and a reply already created by the acknowledgement.
        A preserved ID-bearing Review/Snooze can also recover an old event
        that resurfaced when only the aggregate thread status changed.
        """
        if (previous is None or not self._verified_project_reply(source, obs, extra)
                or previous["provider"] != "claude" or previous_extra.get("source_kind") != "project"
                or previous_extra.get("project_role") != "thread"):
            return None
        thread, reply = extra.get("project_thread_id"), extra.get("project_reply_id")
        if (thread != previous_extra.get("project_thread_id")
                or extra.get("project_channel_id") != previous_extra.get("project_channel_id")
                or previous_extra.get("project_reply_id") not in (None, reply)):
            return None
        binding = previous_extra.get("project_reply_event")
        prefix = "claude:" + obs["id"] + ":ready:"

        def normalized_aliases(name, limit):
            values = source.get(name)
            if not isinstance(values, list) or not 1 <= len(values) <= limit:
                return set()
            if any(not isinstance(value, str) or not value.startswith(prefix)
                   or not re.fullmatch(r"[0-9a-f]{24}", value[len(prefix):]) for value in values):
                return set()
            return {hashlib.sha256((value + ":ready").encode()).hexdigest()[:24] for value in values}

        aliases = normalized_aliases("_project_event_aliases", 6)
        id_aliases = normalized_aliases("_project_id_event_aliases", 2)
        # Reconstruct the historical encoding from the verified proof. A
        # caller-supplied alias list alone cannot bind a different message.
        revision = source["_project_reply_revision"]
        def legacy(values):
            raw = prefix + hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
            return hashlib.sha256((raw + ":ready").encode()).hexdigest()[:24]
        expected_ids = {legacy([*revision, "idle", False, waiting, reply]) for waiting in (False, None)}
        expected_aliases = expected_ids | {legacy([*revision, "resolved", working, waiting])
                                           for working in (False, None) for waiting in (False, None)}
        if id_aliases != expected_ids or aliases != expected_aliases:
            return None
        if (previous_extra.get("project_reply_identity_version") != 1
                and previous["updated_at"] == obs["updated_at"]
                and previous["reviewed_event"] in aliases - id_aliases
                and self._finite_number(previous["reviewed_at"])
                and obs["updated_at"] <= previous["reviewed_at"]):
            # No old assistant ID is asserted. This is the first verified ID
            # baseline for a native revision the owner had already reviewed.
            # Versioning every binding prevents this upgrade rule from later
            # acknowledging a different ID with reused provider metadata.
            extra["project_reply_baseline"] = "legacy_resolved_review"
            return previous["reviewed_event"]
        if previous_extra.get("project_reply_id") == reply and isinstance(binding, str) and re.fullmatch(r"[0-9a-f]{24}", binding):
            if previous_extra.get("project_reply_baseline") == "legacy_resolved_review":
                extra["project_reply_baseline"] = "legacy_resolved_review"
            return binding
        if previous["status"] == "ready" and previous["event_id"] in id_aliases:
            return previous["event_id"]
        if obs["updated_at"] != previous["updated_at"]:
            return None
        if previous["status"] == "ready" and previous["event_id"] not in aliases:
            return None
        if (previous["reviewed_event"] in id_aliases and self._finite_number(previous["reviewed_at"])
                and previous["reviewed_at"] >= obs["updated_at"]):
            return previous["reviewed_event"]
        preference = db.execute("SELECT snoozed_event FROM session_preferences WHERE key=?", (obs["key"],)).fetchone()
        if preference is not None and preference["snoozed_event"] in id_aliases:
            return preference["snoozed_event"]
        return None

    @staticmethod
    def normalize(source, now):
        if not isinstance(source, dict):
            return None
        provider = source.get("provider")
        if provider not in ("codex", "claude", "chatgpt") or not isinstance(source.get("status"), str) or source["status"] not in STATUSES:
            return None
        ident = str(source.get("id", ""))
        if not ident or len(ident) > 200 or any(0xd800 <= ord(char) <= 0xdfff for char in ident):
            return None
        unlinked = False
        if source.get("source_kind") == "desktop_chat":
            desktop_id = ident[len(DESKTOP_CHAT_ID_PREFIX):]
            if (provider not in ("claude", "chatgpt") or not ident.startswith(DESKTOP_CHAT_ID_PREFIX)
                    or not valid_desktop_chat_id(provider, desktop_id)):
                return None
            unlinked = CHATGPT_WINDOW_ID.fullmatch(desktop_id) is not None
            if unlinked and (not isinstance(source.get("title"), str) or not source["title"].strip()):
                return None
        elif provider == "chatgpt":
            return None
        try:
            updated = float(source["updated_at"])
            started = float(source["started_at"]) if source.get("started_at") else None
            if not math.isfinite(updated) or (started is not None and not math.isfinite(started)):
                return None
        except (TypeError, ValueError, KeyError, OverflowError):
            return None
        raw_event = str(source.get("event_id") or f"{updated}:{source['status']}")
        if len(raw_event) > 4096:
            return None
        # A status change is a new revision even if a source reuses its event ID.
        event_id = hashlib.sha256((raw_event + ":" + source["status"]).encode("utf-8", errors="surrogatepass")).hexdigest()[:24]
        extra = BoardStore._normalize_extra(source)
        if unlinked:
            extra.pop("open_url", None)
        return {
            "key": source["provider"] + ":" + ident,
            "provider": source["provider"], "id": ident,
            "title": BoardStore._text(source.get("title") or "Untitled conversation", 500),
            "cwd": BoardStore._text(source.get("cwd") or "", 2000),
            "status": source["status"], "detail": BoardStore._text(source.get("detail") or "", 1000),
            "updated_at": updated, "started_at": started, "observed_at": now,
            "event_id": event_id, "can_open": int(not unlinked and bool(source.get("can_open"))),
            "extra": json.dumps(extra),
        }

    @staticmethod
    def _text(value, limit):
        return str(value)[:limit].encode("utf-8", errors="replace").decode("utf-8")

    @staticmethod
    def _normalize_extra(source):
        result = {}
        for field in ("turn_id", "open_label", "open_kind", "open_url", "open_error", "title_source", "project_name", "project_channel_id", "project_role", "project_thread_id", "project_reply_id", "preview", "preview_kind", "source_kind", "history_checking_detail", "history_quiet_event"):
            if field in source and isinstance(source[field], str):
                result[field] = BoardStore._text(source[field], 2400 if field == "preview" else 2000)
        for field in ("last_activity_at", "preview_at", "history_quiet_at"):
            value = source.get(field)
            if BoardStore._finite_number(value):
                result[field] = value
        if type(source.get("status_inferred")) is bool:
            result["status_inferred"] = source["status_inferred"]
        if type(source.get("history_checking")) is bool:
            result["history_checking"] = source["history_checking"]
        if type(source.get("legacy_identity_unverified")) is bool:
            result["legacy_identity_unverified"] = source["legacy_identity_unverified"]
        revision = BoardStore._verified_history_revision(source)
        if revision:
            result["history_revision"] = revision
        return result

    @staticmethod
    def _verified_history_revision(source):
        # This authority is produced by the local transcript reader after a
        # complete index. Metadata/live-only observations cannot assert it.
        if (source.get("provider") not in ("claude", "codex") or source.get("source_kind", "local") != "local"
                or source.get("history_verified") is not True or source.get("history_checking") is True
                or source.get("metadata_only") is True):
            return None
        return BoardStore._history_revision(source.get("history_revision"))

    @staticmethod
    def _history_revision(value):
        if (not isinstance(value, dict) or type(value.get("size")) is not int or not 0 <= value["size"] < 2 ** 63
                or any(not isinstance(value.get(field), str)
                       or not re.fullmatch(r"[0-9a-f]{64}", value[field])
                       for field in ("file_id", "sha256", "generation"))):
            return None
        return {field: value[field] for field in ("file_id", "size", "sha256", "generation")}

    @staticmethod
    def _finite_number(value):
        try:
            return type(value) in (float, int) and math.isfinite(value)
        except OverflowError:
            return False

    @staticmethod
    def decorate(row, preference=None, now=None):
        item = dict(row)
        item.update(BoardStore._extra(item.pop("extra")))
        item.pop("history_revision", None)  # Internal file checkpoint, not board content.
        item["can_open"] = bool(item["can_open"])
        item["automatic_title"] = item["title"]
        item["source_background_reason"] = item.get("background_reason")
        preference = dict(preference) if preference else {}
        if preference.get("title_override"):
            item["title"] = preference["title_override"]
            item["title_override"] = preference["title_override"]
        placement = preference.get("placement", "auto")
        item["placement_override"] = placement
        if placement == "board":
            item.pop("background_reason", None)
        elif placement == "background":
            item["background_reason"] = "manual"
        if item.get("legacy_identity_unverified"):
            item["background_reason"] = "unverified_chat_identity"
            item["status"] = "disconnected"
            item["detail"] = "An older window reading could not verify this chat's identity. It is preserved locally outside conversation lists."
        item["snoozed_until"] = (preference.get("snoozed_until", 0)
            if preference.get("snoozed_event") == item["event_id"] else 0)
        item["history_checking"] = item.get("history_checking") is True
        if item["history_checking"]:
            item["confirmed_status"] = item["status"]
            item["status"] = "disconnected"
            item["detail"] = item.get("history_checking_detail", "Checking local chat history before confirming its status.")
            for field in ("preview", "preview_kind", "preview_at"):
                item.pop(field, None)
        quiet_reconfirmation = (item.get("history_quiet_event") == item["event_id"]
            and (not item["snoozed_until"] or item["snoozed_until"] <= item.get("history_quiet_at", 0)))
        item["reconfirmation_silent"] = bool(quiet_reconfirmation)
        item["notification_eligible"] = bool(not item.get("background_reason")
            and not item["history_checking"]
            and item["reviewed_event"] != item["event_id"]
            and item["status"] in ("ready", "needs_you")
            and item["snoozed_until"] <= (now if now is not None else time.time()))
        return item

    def snapshot(self, hidden_desktop_providers=()):
        with self.lock, self.connect() as db:
            rows = [self._fresh_row(row) for row in db.execute("""SELECT s.*,b.reason AS background_reason FROM sessions s
                LEFT JOIN background_sessions b ON b.key=s.key
                ORDER BY s.updated_at DESC""").fetchall()]
            preferences = {row["key"]: row for row in db.execute("SELECT * FROM session_preferences")}
        active, history, background = [], [], []
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            item = self.decorate(row, preferences.get(row["key"]))
            # Unsupported window identities can refer to conversations already
            # represented by their canonical IDs. Keep their recoverable state
            # in SQLite without showing a second card in any conversation list.
            if item.get("legacy_identity_unverified"):
                continue
            if item.get("source_kind") == "desktop_chat" and item["provider"] in hidden_desktop_providers:
                continue
            if item.get("background_reason"):
                background.append(item)
                continue
            item.pop("background_reason", None)
            if item["reviewed_event"] == item["event_id"]:
                history.append(item)
            else:
                counts[item["status"]] += 1
                active.append(item)
        priority = {"needs_you": 0, "ready": 1, "disconnected": 2, "cooking": 3}
        active.sort(key=lambda x: (priority[x["status"]], x["updated_at"], x["key"]))
        return {"sessions": active, "history": history[:100], "background": background,
                "counts": counts, "started_at": self.activated_at,
                "recovery_notice": self.recovery_notice}

    def get(self, key):
        return self.get_many((key,)).get(key)

    def get_many(self, keys):
        """Read a scan's saved states together, including in-memory freshness."""
        keys = tuple(dict.fromkeys(keys))
        result = {}
        with self.lock, self.connect() as db:
            for offset in range(0, len(keys), 900):
                batch = keys[offset:offset + 900]
                rows = db.execute("""SELECT s.*,b.reason AS background_reason,
                    p.title_override AS pref_title_override,p.placement AS pref_placement,
                    p.snoozed_until AS pref_snoozed_until,p.snoozed_event AS pref_snoozed_event
                    FROM sessions s LEFT JOIN background_sessions b ON b.key=s.key
                    LEFT JOIN session_preferences p ON p.key=s.key WHERE s.key IN ("""
                    + ",".join("?" for _ in batch) + ")", batch)
                for row in rows:
                    item = self._fresh_row(row)
                    preference = {field: item.pop("pref_" + field) for field in
                                  ("title_override", "placement", "snoozed_until", "snoozed_event")}
                    preference = {key: value for key, value in preference.items() if value is not None}
                    result[item["key"]] = self.decorate(item, preference)
        return result

    def rename(self, key, title):
        if title is not None:
            if not isinstance(title, str):
                raise ValueError("Enter a chat name.")
            title = self._text(" ".join(title.split()), 161)
            if not title or len(title) > 160 or any(ord(char) < 32 for char in title):
                raise ValueError("Use a name between 1 and 160 characters.")
        with self.lock, self.connect() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone():
                raise KeyError(key)
            db.execute("""INSERT INTO session_preferences (key,title_override) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET title_override=excluded.title_override""", (key, title))

    def move(self, key, placement):
        if placement not in ("board", "background", "auto"):
            raise ValueError("Choose Board, Background, or automatic placement.")
        with self.lock, self.connect() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone():
                raise KeyError(key)
            db.execute("""INSERT INTO session_preferences (key,placement) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET placement=excluded.placement""", (key, placement))
            if placement == "board":
                db.execute("UPDATE sessions SET reviewed_event=NULL,reviewed_at=NULL WHERE key=?", (key,))

    def snooze(self, key, event_id, minutes=10, now=None):
        if type(minutes) is not int or minutes not in (0, 10):
            raise ValueError("Snooze is available for 10 minutes.")
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if row["event_id"] != event_id or row["status"] not in ("ready", "needs_you"):
                raise Conflict("This conversation changed. Snooze its latest reply instead.")
            until = (now if now is not None else time.time()) + minutes * 60 if minutes else 0
            db.execute("""INSERT INTO session_preferences (key,snoozed_until,snoozed_event) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET snoozed_until=excluded.snoozed_until,snoozed_event=excluded.snoozed_event""",
                (key, until, event_id))

    def review(self, key, event_id):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if row["status"] == "cooking":
                raise Conflict("This conversation is working again. Review its next reply when it finishes.")
            if not event_id or row["event_id"] != event_id:
                raise Conflict("This conversation changed. The board has refreshed; review the latest status.")
            db.execute("UPDATE sessions SET reviewed_event=event_id,reviewed_at=? WHERE key=?", (time.time(), key))

    def restore(self, key):
        with self.lock, self.connect() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone():
                raise KeyError(key)
            db.execute("UPDATE sessions SET reviewed_event=NULL,reviewed_at=NULL WHERE key=?", (key,))

    def session_ids(self, provider, candidates=None):
        if candidates is not None:
            candidates = tuple(candidates)
            if not candidates:
                return set()
            placeholders = ",".join("?" for _ in candidates)
            with self.lock, self.connect() as db:
                rows = db.execute("SELECT id FROM sessions WHERE provider=? AND id IN (" + placeholders + ")",
                                  (provider, *candidates))
                return {row["id"] for row in rows}
        with self.lock, self.connect() as db:
            return {row["id"] for row in db.execute("SELECT id FROM sessions WHERE provider=?", (provider,))}

    def transcript_revision(self, ident, provider="claude"):
        if provider not in ("claude", "codex"):
            return None
        with self.lock, self.connect() as db:
            row = db.execute("SELECT extra FROM sessions WHERE provider=? AND id=?", (provider, ident)).fetchone()
            return self._extra(row["extra"]).get("history_revision") if row else None

    def set_background_sessions(self, provider, reasons, now=None):
        """Route internal sessions separately without deleting chats or reviews."""
        if provider not in ("claude", "codex"):
            return
        with self.lock, self.connect() as db:
            existing = {row["id"]: row["reason"] for row in db.execute(
                "SELECT s.id,b.reason FROM sessions s LEFT JOIN background_sessions b ON b.key=s.key WHERE s.provider=?",
                (provider,))}
            for ident, reason in reasons.items():
                key = provider + ":" + str(ident)
                reason = str(reason)[:200]
                if str(ident) not in existing or existing[str(ident)] == reason:
                    continue
                db.execute("""INSERT INTO background_sessions (key,reason,added_at)
                    SELECT key,?,? FROM sessions WHERE key=?
                    ON CONFLICT(key) DO UPDATE SET reason=excluded.reason""",
                    (reason, now if now is not None else time.time(), key))
                existing[str(ident)] = reason

    def mark_unavailable(self, provider, detail, present_keys=None, source_kind=None):
        # Keep completed replies and reviewed rows. Only unreviewed live work
        # loses confirmation; a healthy scan can limit this to missing sessions.
        present_keys = set(present_keys) if present_keys is not None else set()
        with self.lock, self.connect() as db:
            rows = db.execute("""
                SELECT key,event_id,extra FROM sessions
                WHERE provider=? AND status IN ('cooking','needs_you')
                  AND (reviewed_event IS NULL OR reviewed_event <> event_id)
            """, (provider,)).fetchall()
            for row in rows:
                if self._extra(row["extra"]).get("history_checking") is True:
                    continue
                if source_kind is not None and self._extra(row["extra"]).get("source_kind", "local") != source_kind:
                    continue
                if row["key"] in present_keys:
                    continue
                event = hashlib.sha256((row["event_id"] + ":disconnected").encode()).hexdigest()[:24]
                db.execute("UPDATE sessions SET status='disconnected',detail=?,event_id=? WHERE key=?", (detail, event, row["key"]))
