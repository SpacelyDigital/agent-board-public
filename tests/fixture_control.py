"""Disposable source files for exercising the real collector and interface.

This script only writes beneath a caller-supplied temporary folder. It never
writes the actual ~/.codex or ~/.claude directories or calls either model.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import time

IDENT = "e2e-review-session"
WAITING = "e2e-permission-session"


def append(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")


def event(path, kind, turn="turn-1"):
    stamp = time.time()
    payload = {"type": kind, "turn_id": turn}
    payload["started_at" if kind == "task_started" else "completed_at"] = stamp
    append(path, {"timestamp": stamp, "type": "event_msg", "payload": payload})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("action", choices=("init", "start", "finish", "resume", "fail", "archive"))
    args = parser.parse_args()
    root = args.root.resolve()
    temporary_roots = (Path(tempfile.gettempdir()).resolve(), Path('/private/tmp').resolve())
    if not any(p in root.parents for p in temporary_roots) or not root.name.startswith("agent-board-e2e-"):
        parser.error("Use a unique agent-board-e2e-* folder in the system temporary directory.")
    codex, claude = root / "codex", root / "claude"
    path = codex / (IDENT + ".jsonl")
    if args.action == "init":
        codex.mkdir(parents=True, exist_ok=False)
        (claude / "projects").mkdir(parents=True)
        with sqlite3.connect(codex / "state_5.sqlite") as db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY,rollout_path TEXT,updated_at INTEGER,title TEXT,cwd TEXT,archived INTEGER,thread_source TEXT,agent_path TEXT)")
    elif args.action == "start":
        for ident, title in ((IDENT, "Disposable review workflow"), (WAITING, "Disposable permission workflow")):
            log = codex / (ident + ".jsonl")
            append(log, {"type": "session_meta", "payload": {"id": ident, "cwd": "/tmp/agent-board-qa", "thread_source": "user"}})
            with sqlite3.connect(codex / "state_5.sqlite") as db:
                db.execute("INSERT INTO threads VALUES (?,?,?,?,?,0,'user',NULL)", (ident, str(log), int(time.time()), title, "/tmp/agent-board-qa"))
            event(log, "task_started")
        event(codex / (WAITING + ".jsonl"), "approval_requested")
    elif args.action == "finish":
        event(path, "task_complete")
    elif args.action == "resume":
        event(path, "task_started", "turn-2")
    elif args.action == "fail":
        event(path, "turn_aborted", "turn-2")
    elif args.action == "archive":
        with sqlite3.connect(codex / "state_5.sqlite") as db:
            db.execute("UPDATE threads SET archived=1 WHERE id=?", (IDENT,))
    print(root)


if __name__ == "__main__":
    main()
