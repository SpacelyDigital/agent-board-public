"""Read Claude Code status without sending prompts or changing its settings.

The supported ``claude agents --json --all`` interface supplies live state.
Existing session metadata and a bounded complete transcript index supply timestamps and
turn identities, and remain a fallback when the CLI is unavailable. Transcript
formats are private implementation details, so uncertainty is never completion.
Captions and handoff previews are bounded literal excerpts. Credentials and
tool results are never included in observations.
"""

from __future__ import annotations

import hashlib
import copy
import codecs
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time
import subprocess
from typing import Any
from datetime import datetime
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

TAIL_BYTES = 4 * 1024 * 1024
INFERRED_ACTIVE_SECONDS = 180
REGISTRY_FRESH_SECONDS = 300
CLI_POLL_SECONDS = 5
DESKTOP_HEADER_BYTES = 16 * 1024
DESKTOP_METADATA_BYTES = 4 * 1024 * 1024
PROMPT_HEADER_BYTES = 512 * 1024
TRANSCRIPT_READ_CHUNK = 256 * 1024
TRANSCRIPT_POLL_BYTES = 128 * 1024 * 1024
TRANSCRIPT_POLL_SECONDS = 0.075
TRANSCRIPT_RECORD_BYTES = 1024 * 1024
TRANSCRIPT_TEXT_CHARS = 64 * 1024
TRANSCRIPT_MAX_FILES = 1024
TRANSCRIPT_MAX_BUFFER_BYTES = 16 * 1024 * 1024
TRANSCRIPT_MAX_IDENTITIES = 32768
BACKGROUND_MAX_IDENTITIES = 4096
_INTERRUPT = re.compile(r"^\[Request interrupted by user(?: for tool use)?\]$")
_LOCAL_SESSION = re.compile(r"^local_[A-Za-z0-9-]{1,64}$")
_PLACEHOLDER_TITLES = {"remote control session", "remote control session (from a project)",
                       "claude code session", "new session", "untitled"}
_CONTEXT_TAG = re.compile(r"<(/?)(INSTRUCTIONS|environment_context|system-reminder|system|context|"
                          r"environment|permissions|developer_instructions)\b[^<>]*>", re.I)
_BACKGROUND_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TASK_NOTIFICATION_LIMIT = 64 * 1024


def _background_id(value: Any) -> str:
    return value if isinstance(value, str) and _BACKGROUND_ID.fullmatch(value) else ""


def _transcript_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate transcript field")
        result[key] = value
    return result


def _transcript_constant(value):
    raise ValueError("Non-finite transcript JSON constant")


def _transcript_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Transcript number exceeds the supported finite range")
    return number


def _task_notification(text: str) -> tuple[str, str, str]:
    """Read only the lifecycle fields of an explicit task notification.

    The task's output and summary must never become a reply preview, prompt
    caption, or a new user turn. An arbitrary mention of background work in
    assistant prose is not lifecycle evidence.
    """
    text = text.strip()
    if (len(text) > _TASK_NOTIFICATION_LIMIT or not text.startswith("<task-notification>")
            or not text.endswith("</task-notification>") or "<!" in text):
        return "", "", ""
    try:
        envelope = ET.fromstring(text)
    except ET.ParseError:
        return "", "", ""
    if envelope.tag != "task-notification" or envelope.attrib:
        return "", "", ""
    fields = {}
    for child in envelope:
        if child.tag in ("task-id", "tool-use-id", "status"):
            if child.tag in fields or child.attrib or len(child):
                return "", "", ""
            fields[child.tag] = (child.text or "").strip()
    identifier = _background_id(fields.get("task-id"))
    tool_identifier = _background_id(fields.get("tool-use-id"))
    if "tool-use-id" in fields and not tool_identifier:
        return "", "", ""
    status = fields.get("status")
    return ((identifier, status, tool_identifier)
            if identifier and status in ("completed", "failed", "killed", "stopped") else ("", "", ""))


class _BackgroundWork:
    """Bounded transcript-local lifecycle identities, never task output paths."""

    def __init__(self):
        self.requests: dict[str, float] = {}
        self.tasks: dict[str, tuple[str, float]] = {}
        self.task_by_tool: dict[str, str] = {}
        self.terminal_tasks: dict[str, tuple[str, float]] = {}
        self.terminal_tools: dict[str, tuple[str, float]] = {}
        self.unknown = False
        self.limit = BACKGROUND_MAX_IDENTITIES

    @property
    def identity_count(self) -> int:
        return sum(len(items) for items in (self.requests, self.tasks, self.task_by_tool,
                                            self.terminal_tasks, self.terminal_tools))

    def reserve(self, count: int) -> bool:
        if self.identity_count + count > self.limit:
            self.unknown = True
            return False
        return True

    @property
    def pending(self) -> bool:
        return bool(self.requests or self.tasks)

    def request(self, tool_id: str, stamp: float) -> None:
        terminal = self.terminal_tools.get(tool_id)
        if terminal:
            if stamp > terminal[1]:
                self.unknown = True  # Reused identity cannot certify new work.
            return
        existing_task = self.tasks.get(self.task_by_tool.get(tool_id, ""))
        existing_stamp = existing_task[1] if existing_task else self.requests.get(tool_id)
        if existing_stamp is not None:
            if stamp != existing_stamp:
                self.unknown = True
            return
        if self.reserve(1):
            self.requests[tool_id] = stamp

    def result(self, task_id: str, tool_id: str, stamp: float) -> None:
        terminal = self.terminal_tasks.get(task_id)
        terminal_tool = self.terminal_tools.get(tool_id) if tool_id else None
        if terminal or terminal_tool:
            pair_matches = ((not terminal or not tool_id or terminal[0] == tool_id)
                            and (not terminal_tool or terminal_tool[0] == task_id))
            terminal_stamp = max(terminal[1] if terminal else 0, terminal_tool[1] if terminal_tool else 0)
            if not pair_matches or stamp > terminal_stamp:
                self.unknown = True
            return  # A delayed launch result must not resurrect terminal work.
        existing = self.tasks.get(task_id)
        if ((existing and tool_id and existing[0] != tool_id)
                or (tool_id in self.task_by_tool and self.task_by_tool[tool_id] != task_id)):
            self.unknown = True
            return
        launched = self.requests.get(tool_id, stamp)
        if tool_id in self.requests and stamp < launched:
            self.unknown = True
            return
        if not existing and not self.reserve(2):
            return
        self.tasks.setdefault(task_id, (tool_id, launched))
        if tool_id:
            self.requests.pop(tool_id, None)
            self.task_by_tool[tool_id] = task_id

    def rejected(self, tool_id: str, stamp: float) -> None:
        launched = self.requests.get(tool_id)
        if launched is not None and stamp >= launched:
            self.requests.pop(tool_id)

    def settle(self, task_id: str, tool_id: str, stamp: float) -> bool:
        if task_id in self.terminal_tasks or (tool_id and tool_id in self.terminal_tools):
            return False
        existing = self.tasks.get(task_id)
        if existing:
            known_tool, launched = existing
            # Both IDs, when supplied, must describe the same launch. Never
            # settle an unrelated request through a second supplied identity.
            if (tool_id and tool_id != known_tool) or not launched or stamp < launched:
                return False
            self.tasks.pop(task_id)
            if known_tool:
                self.task_by_tool.pop(known_tool, None)
            tool_id = known_tool
        elif tool_id in self.requests and tool_id not in self.task_by_tool:
            launched = self.requests[tool_id]
            if not launched or stamp < launched:
                return False
            if not self.reserve(1):
                return False
            self.requests.pop(tool_id)
        else:
            return False
        self.terminal_tasks[task_id] = (tool_id, stamp)
        if tool_id:
            self.terminal_tools[tool_id] = (task_id, stamp)
        return True


def _timestamp(value: Any, default: float = 0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            numeric = float(value)
            return (numeric / 1000 if numeric > 100_000_000_000 else numeric) if math.isfinite(numeric) else default
        except OverflowError:
            return default
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return default


def _short(value: Any, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


def _literal_preview(value: Any) -> str:
    """Keep a bounded literal reply/question, rejecting internal envelopes."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()
    if (not text or re.match(r"(?is)^<(?:relay|system|system-reminder|environment_context|task-notification|"
                             r"INSTRUCTIONS|context|permissions|developer_instructions)\b", text)
            or "<relay" in text[:200] or _INTERRUPT.fullmatch(text)):
        return ""
    return text[:500]


def _question_preview(blocks: list[dict]) -> str:
    for block in blocks:
        if block.get("type") != "tool_use" or block.get("name") != "AskUserQuestion":
            continue
        tool_input = block.get("input")
        questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
        if isinstance(questions, list):
            return _literal_preview("\n".join(q["question"] for q in questions
                                           if isinstance(q, dict) and isinstance(q.get("question"), str)))
    return ""


def _useful_title(value: Any, cwd: str = "") -> bool:
    title = _short(value).casefold()
    if not title or title in _PLACEHOLDER_TITLES:
        return False
    slug = re.sub(r"[^a-z0-9]+", "-", Path(cwd).name.casefold()).strip("-")
    return not (slug and re.fullmatch(re.escape(slug) + r"-[a-z0-9]{1,8}", title))


def _strip_context_blocks(text: str) -> str:
    # Regex matching an opening tag followed by .*? and a missing closing tag
    # becomes quadratic when a malformed transcript repeats opening tags.
    # Scan each tag once and discard incomplete internal envelopes as well.
    counts: dict[str, int] = {}
    depth, offset, pieces = 0, 0, []
    for match in _CONTEXT_TAG.finditer(text):
        if not depth:
            pieces.append(text[offset:match.start()])
        tag = match[2].casefold()
        if match[1]:
            if counts.get(tag, 0):
                counts[tag] -= 1
                depth -= 1
        elif not match[0].rstrip().endswith("/>"):
            counts[tag] = counts.get(tag, 0) + 1
            depth += 1
        offset = match.end()
    if not depth:
        pieces.append(text[offset:])
    return " ".join(pieces)


def _prompt_caption(text: Any) -> str:
    """Produce a short literal task caption, without context/config wrappers."""
    if not isinstance(text, str):
        return ""
    # Find each fence once; repeated incomplete openings cannot force the
    # regex engine to rescan an entire bounded transcript for every opening.
    offset, pieces = 0, []
    while True:
        opening = text.find("```", offset)
        newline = text.find("\n", opening + 3) if opening >= 0 else -1
        closing = text.find("```", newline + 1) if newline >= 0 else -1
        if closing < 0:
            pieces.append(text[offset:])
            break
        pieces.append(text[offset:opening])
        offset = closing + 3
    text = "".join(pieces)
    text = _strip_context_blocks(text)
    text = re.sub(r"</?user_(?:query|request)\b[^>]*>", "", text, flags=re.I)
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:#+\s*|[-*]\s+)", "", line).strip()
        if not line or line.startswith("<") or re.match(
                r"(?i)^(?:AGENTS\.md(?: instructions)?|environment(?: context)?|system (?:prompt|instructions)|"
                r"you are (?:an?|the) (?:AI|agent|assistant)|\[Request interrupted)", line):
            continue
        line = re.sub(r"https?://\S+|(?:/Users/|/home/|/private/|/tmp/)\S+", "", line)
        line = " ".join(line.replace("`", "").split()).rstrip(" ,;:")
        line = re.sub(r"(?i)\s+(?:at|in)\s*$", "", line)
        if len(line) < 20 or len(re.findall(r"\w+", line)) < 4:
            continue
        if re.fullmatch(r"(?i)(?:yes|ok(?:ay)?|continue|go ahead|do it|thanks|thank you|please continue)[.! ]*", line):
            continue
        if len(line) > 100:
            prefix = line[:99]
            line = (prefix.rsplit(" ", 1)[0] if " " in prefix else prefix).rstrip(" ,;:") + "…"
        return line
    return ""


def _real_user_text(event: dict) -> str:
    if (event.get("type") != "user" or event.get("isMeta") or event.get("isSidechain")
            or event.get("agentId") or "toolUseResult" in event or event.get("promptSource") == "system"):
        return ""
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return "" if _INTERRUPT.fullmatch(content.strip()) else content
    if isinstance(content, list):
        if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            return ""
        return "\n".join(block["text"] for block in content if isinstance(block, dict)
                         and block.get("type") == "text" and isinstance(block.get("text"), str))
    return ""


def _task_caption(event: dict) -> tuple[str, str]:
    caption = _prompt_caption(_real_user_text(event))
    if caption:
        return caption, "prompt"
    # Remote Control project children receive a coordinator's task note rather
    # than the user's original message. Its bounded task caption is useful, but
    # it must never be mistaken for a submitted user turn.
    content = event.get("message", {}).get("content") if isinstance(event.get("message"), dict) else None
    if (event.get("type") == "user" and event.get("isMeta") and isinstance(content, str)
            and not event.get("isSidechain") and not event.get("agentId")
            and "coordinator session" in content[:200] and re.search(r"<relay\b", content)):
        opening = re.search(r"<note\b[^<>]*>", content, flags=re.I)
        closing = re.search(r"</note\s*>", content[opening.end():], flags=re.I) if opening else None
        if opening and closing:
            note_text = content[opening.end():opening.end() + closing.start()]
            project = re.search(r"(?im)^\s*Project:\s*(.+)", note_text)
            if project:
                project_text = re.split(r"\s+(?:at|in)\s+[`~]*[/~]", project.group(1), maxsplit=1)[0]
                project_text = re.sub(r"\([^\n)]{1,160}\)", "", project_text)
                caption = _prompt_caption(project_text)
            else:
                # Skip machine/folder/permission directions and find the actual
                # task sentence instead of displaying the relay's scaffolding.
                caption = ""
                for sentence in re.split(r"\n+|(?<=[.!?])\s+", note_text):
                    if re.match(r"(?i)^\s*(?:Task:|Current task:|Job:|Goal:|Objective:|Request:|"
                                r"(?:Please\s+)?(?:build|implement|fix|add|investigate|review|create|audit|"
                                r"update|research|design|analy[sz]e|compare|debug|finish)\b)", sentence):
                        sentence = re.sub(r"\([^\n)]{1,300}\)", "", sentence)
                        caption = _prompt_caption(re.sub(r"(?i)^\s*(?:Task|Current task|Job|Goal|Objective|Request):\s*", "", sentence))
                        caption = caption[:1].upper() + caption[1:]
                        if caption:
                            break
            if caption:
                return caption, "task"
    return "", ""


def _identity(event: dict, offset: int = 0) -> str:
    explicit = event.get("uuid") or event.get("id")
    if explicit:
        return str(explicit)
    fields = [event.get("type"), event.get("subtype"), event.get("timestamp"), offset]
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()[:20]


def _process_alive(pid: Any) -> bool | None:
    """Permission denial is unknown, not evidence that a process exited."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None


def _find_claude() -> str | None:
    candidates = [shutil.which("claude"), str(Path.home() / ".local/bin/claude"),
                  "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    bundled = Path.home() / "Library/Application Support/Claude/claude-code"
    try:
        candidates.extend(str(p) for p in sorted(
            bundled.glob("*/*/claude.app/Contents/MacOS/claude"),
            key=lambda p: p.stat().st_mtime, reverse=True))
    except OSError:
        pass
    return next((p for p in candidates if p and os.path.isfile(p) and os.access(p, os.X_OK)), None)


class _TranscriptConsumer:
    def __init__(self, session_id: str, captions: bool):
        self.session_id = session_id
        self.captions = captions
        self.unknown = False
        self.background = _BackgroundWork()
        self.state = {"id": session_id, "status": "disconnected", "updated_at": 0,
                      "event_id": "", "turn_id": None, "cwd": "", "title": "",
                      "prompt_title": "", "caption_source": "",
                      "detail": "No completed reply was recorded."}

    def consume(self, event: Any, offset: int) -> None:
        state, background = self.state, self.background
        if not isinstance(event, dict):
            self.unknown = True
            return
        if event.get("isSidechain") or event.get("agentId"):
            return
        if event.get("sessionId") and event["sessionId"] != self.session_id:
            return
        kind = event.get("type")
        if not isinstance(kind, str):
            self.unknown = True
            return
        if kind in ("user", "assistant") and not isinstance(event.get("message"), dict):
            self.unknown = True
            return
        stamp = _timestamp(event.get("timestamp"))
        if event.get("cwd"):
            state["cwd"] = _short(event["cwd"], 2048)
        if kind == "custom-title" and event.get("customTitle"):
            state["title"] = _short(event["customTitle"])
        if self.captions and not state["prompt_title"]:
            state["prompt_title"], state["caption_source"] = _task_caption(event)
        message = event.get("message")
        message = message if isinstance(message, dict) else {}
        content = message.get("content", "")
        if kind in ("user", "assistant") and not isinstance(content, (str, list)):
            self.unknown = True
            return
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str))
        status = detail = None
        # Desktop queues a task notification before the model receives its
        # next user/meta record. That queue entry already settles the task.
        notification_text = (event.get("content", "")
                             if kind == "queue-operation" and event.get("operation") == "enqueue"
                             else text if kind == "user" and (event.get("isMeta") is True
                                                             or event.get("promptSource") == "system") else "")
        notification_text = notification_text if isinstance(notification_text, str) else ""
        task_id, task_status, tool_id = _task_notification(notification_text)
        if task_id:
            if background.settle(task_id, tool_id, stamp):
                if task_status == "completed":
                    status, detail = "cooking", "Claude is continuing after a background task."
                else:
                    status, detail = "disconnected", "Claude's background task failed or was stopped."
                    state["background_failed"] = True
        elif kind == "user":
            if _INTERRUPT.fullmatch(text.strip()):
                status, detail = "disconnected", "The turn was interrupted."
            elif any(b.get("type") == "tool_result" for b in blocks) or "toolUseResult" in event:
                # A tool result has role=user but is still the same turn.
                status, detail = "cooking", "Claude is continuing after a tool result."
                result = event.get("toolUseResult")
                task_id = _background_id(result.get("backgroundTaskId")) if isinstance(result, dict) else ""
                if isinstance(result, dict) and result.get("backgroundTaskId") not in (None, "") and not task_id:
                    background.unknown = True
                results = [block for block in blocks if block.get("type") == "tool_result"]
                result_tool_id = _background_id(results[0].get("tool_use_id")) if len(results) == 1 else ""
                for block in blocks:
                    if block.get("type") != "tool_result":
                        continue
                    tool_id = _background_id(block.get("tool_use_id"))
                    if tool_id and block.get("is_error") is True:
                        background.rejected(tool_id, stamp)
                if task_id:
                    background.result(task_id, result_tool_id, stamp)
                if not state["turn_id"] and event.get("promptId"):
                    state["turn_id"] = str(event["promptId"])
            elif not event.get("isMeta") and (text.strip() or blocks):
                status, detail = "cooking", "A prompt is awaiting Claude's reply."
                state["turn_id"] = str(event.get("promptId") or event.get("uuid") or _identity(event, offset))
                state["started_at"] = stamp
                state.pop("background_failed", None)
                state.pop("preview", None)
                state.pop("preview_kind", None)
                state.pop("preview_at", None)
        elif kind == "assistant":
            for block in blocks:
                tool_input = block.get("input")
                if (block.get("type") == "tool_use" and block.get("name") == "Bash"
                        and isinstance(tool_input, dict) and tool_input.get("run_in_background") is True):
                    tool_id = _background_id(block.get("id"))
                    if tool_id:
                        background.request(tool_id, stamp)
                    else:
                        background.unknown = True
            if event.get("isApiErrorMessage") or event.get("error"):
                status, detail = "disconnected", "Claude recorded an error."
            elif message.get("stop_reason") == "end_turn":
                if background.pending:
                    status, detail = "cooking", "Claude has unresolved background work."
                elif background.unknown or self.unknown:
                    status, detail = "disconnected", "Claude's background lifecycle could not be verified from the available history."
                elif state.get("background_failed"):
                    status, detail = "disconnected", "Claude's background task failed or was stopped."
                else:
                    status, detail = "ready", "Claude finished responding."
                    preview = _literal_preview(text)
                    if preview:
                        state.update(preview=preview, preview_kind="reply", preview_at=stamp)
            elif message.get("stop_reason") in (None, "tool_use", "pause_turn"):
                tools = [b.get("name") for b in blocks if b.get("type") == "tool_use"]
                if "AskUserQuestion" in tools or "ExitPlanMode" in tools:
                    status, detail = "needs_you", "Claude is waiting for your answer."
                    preview = _question_preview(blocks)
                    if preview:
                        state.update(preview=preview, preview_kind="permission", preview_at=stamp)
                else:
                    status, detail = "cooking", "Claude is responding or using tools."
            else:
                status, detail = "disconnected", "The reply ended without a completed turn."
        elif kind == "system" and event.get("subtype") in ("api_error", "error", "turn_aborted", "interrupted"):
            status, detail = "disconnected", "Claude recorded an error or interruption."
        if status:
            if status != "ready" and not (status == "needs_you" and _question_preview(blocks)):
                for field in ("preview", "preview_kind", "preview_at"):
                    state.pop(field, None)
            state.update(status=status, detail=detail, updated_at=stamp or state["updated_at"],
                         event_id=_identity(event, offset))

    def snapshot(self) -> dict:
        result = dict(self.state)
        result["background_pending"] = self.background.pending
        result["background_history_unknown"] = self.unknown or self.background.unknown
        if result["background_history_unknown"] and result["status"] in ("ready", "cooking"):
            result.update(status="disconnected", detail="Claude's background lifecycle could not be verified from the available history.")
            for key in ("preview", "preview_kind", "preview_at"):
                result.pop(key, None)
        return result


class _TranscriptBudget:
    def __init__(self):
        self.remaining = TRANSCRIPT_POLL_BYTES
        self.deadline = time.monotonic() + TRANSCRIPT_POLL_SECONDS

    @property
    def available(self) -> bool:
        return self.remaining > 0 and time.monotonic() < self.deadline


_BLOCK_FIELDS = {"type": True, "text": "text", "name": True, "id": True,
                 "tool_use_id": True, "is_error": True,
                 "input": {"run_in_background": True,
                           "questions": ({"question": "text"},)}}
_RECORD_FIELDS = {key: True for key in (
    "type", "subtype", "timestamp", "cwd", "customTitle", "sessionId", "isSidechain",
    "agentId", "isMeta", "promptSource", "operation", "promptId", "uuid", "id",
    "isApiErrorMessage", "error")}
_RECORD_FIELDS.update(message={"stop_reason": True, "content": "content"},
                      content="text", toolUseResult={"backgroundTaskId": True})
_JSON_STRING_SPECIAL = re.compile(rb'["\\\x00-\x1f]')


class _ProjectedJSONRecord:
    """Validate a large JSON record while retaining only bounded status fields.

    Image bytes, tool output, commands and unknown fields are validated and
    discarded incrementally. The retained projection has an independent size,
    nesting and field-count limit. Incomplete or unsupported JSON never grants
    completion authority. Small records keep using the ordinary JSON decoder.
    """

    def __init__(self, fields=None):
        self.root_fields = _RECORD_FIELDS if fields is None else fields
        self.stack = []
        self.result = None
        self.complete = False
        self.string = None
        self.atom = None
        self.atom_schema = None
        self.retained = 0
        self.fields = 0

    @property
    def buffer_size(self):
        return self.retained + (len(self.atom) if self.atom is not None else 0)

    def reserve(self, size):
        self.retained += size
        if self.retained > TRANSCRIPT_RECORD_BYTES:
            raise ValueError("Transcript status fields exceed the bounded projection")

    def schema(self):
        if not self.stack:
            return self.root_fields
        parent = self.stack[-1]
        if parent["kind"] == "object":
            return parent["schema"].get(parent["key"], False) if isinstance(parent["schema"], dict) else False
        return parent["schema"][0] if isinstance(parent["schema"], tuple) else False

    def emit(self, value):
        if not self.stack:
            if self.complete:
                raise ValueError("Multiple transcript values")
            self.result, self.complete = value, True
            return
        parent = self.stack[-1]
        if parent["kind"] == "object":
            if parent["state"] != "value":
                raise ValueError("Unexpected transcript value")
            if isinstance(parent["schema"], dict) and parent["key"] in parent["schema"]:
                parent["value"][parent["key"]] = value
            parent["key"] = None
        else:
            if parent["state"] not in ("value", "value_or_end"):
                raise ValueError("Unexpected transcript array value")
            if isinstance(parent["schema"], tuple):
                self.reserve(16)
                parent["value"].append(value)
        parent["state"] = "comma_or_end"

    def begin_container(self, kind, schema):
        if len(self.stack) >= 64:
            raise ValueError("Transcript nesting exceeds the supported limit")
        if schema == "content" and kind == "array":
            schema = (_BLOCK_FIELDS,)
        elif schema in ("content", "text"):
            # Structured text is ignored by the consumer; message.content
            # becomes None and remains explicitly unsupported. A progress
            # event's unrelated structured content must not poison history.
            schema = False
        if schema and not isinstance(schema, dict if kind == "object" else tuple):
            raise ValueError("Unsupported structured transcript lifecycle field")
        self.reserve(64)
        self.stack.append({"kind": kind, "schema": schema,
                           "value": ({} if kind == "object" else []) if schema else None,
                           "seen": set(), "key": None,
                           "state": "key_or_end" if kind == "object" else "value_or_end"})

    def begin_string(self, role, schema=False):
        self.string = {"role": role, "schema": schema, "parts": [], "count": 0,
                       "limit": (4096 if role == "key" or schema is True else TRANSCRIPT_TEXT_CHARS),
                       "truncated": False, "escape": None,
                       "decoder": codecs.getincrementaldecoder("utf-8")("strict")}

    def string_text(self, text):
        token = self.string
        if token["role"] != "key" and not token["schema"]:
            return
        room = max(0, token["limit"] - token["count"])
        kept = text[:room]
        if kept:
            self.reserve(len(kept.encode("utf-8", "surrogatepass")))
            token["parts"].append(kept)
            if len(token["parts"]) >= 64:
                token["parts"] = ["".join(token["parts"])]
            token["count"] += len(kept)
        if len(text) > room:
            token["truncated"] = True
            if token["role"] == "key" or token["schema"] is True:
                raise ValueError("Transcript identity or field name exceeds the supported limit")

    def end_string(self):
        token = self.string
        self.string_text(token["decoder"].decode(b"", final=True))
        value = "".join(token["parts"])
        # Match json.loads' combination of escaped UTF-16 surrogate pairs.
        value = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
        if token["truncated"]:
            # A bounded prefix must never turn a longer message into an exact
            # interruption or a complete task-notification envelope.
            value += "\0"
        self.string = None
        if token["role"] == "key":
            parent = self.stack[-1]
            if value in parent["seen"]:
                raise ValueError("Duplicate transcript field")
            self.fields += 1
            if self.fields > 16384:
                raise ValueError("Transcript field count exceeds the supported limit")
            self.reserve(64)
            parent["seen"].add(value)
            parent["key"], parent["state"] = value, "colon"
        else:
            self.emit(value)

    def finish_atom(self):
        raw = bytes(self.atom)
        self.atom = None
        try:
            value = json.loads(raw, parse_constant=_transcript_constant, parse_float=_transcript_float)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Invalid transcript scalar") from None
        if isinstance(value, (dict, list, str)):
            raise ValueError("Invalid transcript scalar")
        self.emit(value if self.atom_schema else None)

    def feed(self, data, deadline):
        offset, length = 0, len(data)
        while offset < length and time.monotonic() < deadline:
            if self.string is not None:
                token = self.string
                escape = token["escape"]
                if escape is not None:
                    char = chr(data[offset])
                    offset += 1
                    if escape == "":
                        if char == "u":
                            token["escape"] = "u"
                        elif char in '"\\/bfnrt':
                            self.string_text(json.loads('"\\' + char + '"'))
                            token["escape"] = None
                        else:
                            raise ValueError("Invalid transcript string escape")
                    elif char in "0123456789abcdefABCDEF":
                        token["escape"] += char
                        if len(token["escape"]) == 5:
                            self.string_text(chr(int(token["escape"][1:], 16)))
                            token["escape"] = None
                    else:
                        raise ValueError("Invalid transcript Unicode escape")
                    continue
                special = _JSON_STRING_SPECIAL.search(data, offset)
                end = special.start() if special else length
                if end > offset:
                    self.string_text(token["decoder"].decode(data[offset:end], final=False))
                    offset = end
                if not special:
                    continue
                char = data[offset]
                offset += 1
                if char == 34:
                    self.end_string()
                elif char == 92:
                    self.string_text(token["decoder"].decode(b"", final=True))
                    token["decoder"].reset()
                    token["escape"] = ""
                else:
                    raise ValueError("Unescaped transcript control character")
                continue
            if self.atom is not None:
                if data[offset] in b" \r\t,]}":
                    self.finish_atom()
                    continue
                self.atom.append(data[offset])
                offset += 1
                if len(self.atom) > 128:
                    raise ValueError("Transcript scalar exceeds the supported limit")
                continue
            char = data[offset]
            if char in b" \r\t":
                offset += 1
                continue
            if self.complete:
                raise ValueError("Trailing transcript data")
            parent = self.stack[-1] if self.stack else None
            state = parent["state"] if parent else "value"
            if state in ("key_or_end", "key"):
                if char == 125 and state == "key_or_end":
                    self.emit(self.stack.pop()["value"])
                elif char == 34:
                    self.begin_string("key")
                else:
                    raise ValueError("Invalid transcript object key")
            elif state == "colon":
                if char != 58:
                    raise ValueError("Missing transcript colon")
                parent["state"] = "value"
            elif state == "comma_or_end":
                expected = 125 if parent["kind"] == "object" else 93
                if char == expected:
                    self.emit(self.stack.pop()["value"])
                elif char == 44:
                    parent["state"] = "key" if parent["kind"] == "object" else "value"
                else:
                    raise ValueError("Invalid transcript separator")
            elif state == "value_or_end" and char == 93:
                self.emit(self.stack.pop()["value"])
            else:
                schema = self.schema()
                if char == 123:
                    self.begin_container("object", schema)
                elif char == 91:
                    self.begin_container("array", schema)
                elif char == 34:
                    self.begin_string("value", schema)
                elif char in b"-0123456789tfnNI":
                    self.atom, self.atom_schema = bytearray([char]), schema
                else:
                    raise ValueError("Invalid transcript value")
            offset += 1
        return offset

    def finish(self):
        if self.atom is not None:
            self.finish_atom()
        if self.string is not None or self.stack or not self.complete:
            raise ValueError("Incomplete transcript record")
        return self.result


class _TranscriptCursor:
    """Index one transcript without skipping lifecycle records or saving text.

    A changed file first verifies the previously indexed prefix. This costs a
    bounded reread, but detects rewrites even when inode and file length grow.
    Work is committed only against an unchanged file-stat snapshot.
    """

    def __init__(self, session_id: str, captions: bool, revision=None,
                 consumer_factory=None, record_fields=None, provider="Claude"):
        self.consumer_factory = consumer_factory or _TranscriptConsumer
        self.record_fields = record_fields
        self.provider = provider
        self.consumer = self.consumer_factory(session_id, captions)
        self.indexed_signature = None
        self.indexed_size = 0
        self.indexed_hash = hashlib.sha256()
        self.expected = None
        self.phase = "idle"
        self.offset = 0
        self.buffer = b""
        self.skipping = False
        self.record = None
        self.working = None
        self.hasher = hashlib.sha256()
        self.last_used = 0
        self.file_identity = None
        self.checkpoint = None
        self.scan_restarts = 0
        self.revision = self.valid_revision(revision)
        self.anchor_prefix = None

    @staticmethod
    def valid_revision(value):
        if (not isinstance(value, dict) or type(value.get("size")) is not int
                or not 0 <= value["size"] < 2 ** 63
                or any(not isinstance(value.get(field), str)
                       or not re.fullmatch(r"[0-9a-f]{64}", value[field])
                       for field in ("file_id", "sha256", "generation"))):
            return None
        return {field: value[field] for field in ("file_id", "size", "sha256", "generation")}

    def reset_anchor(self):
        self.anchor_prefix = hashlib.sha256().hexdigest() if self.revision and not self.revision["size"] else None

    def commit_revision(self, signature):
        # Only a complete, supported history can establish replacement
        # authority. The saved prefix is checked during the same budgeted read,
        # including after restart, so ordinary appends retain their generation.
        if self.consumer.snapshot().get("background_history_unknown"):
            return
        file_id = hashlib.sha256(repr(signature[:2]).encode()).hexdigest()
        digest = self.indexed_hash.hexdigest()
        prior = self.revision
        # The independently verified bytes establish continuity. Providers can
        # atomically replace or relocate an unchanged file; its new inode alone
        # must not grant authority to replay an older lifecycle over review.
        same_prefix = (prior and self.indexed_size >= prior["size"]
                       and self.anchor_prefix == prior["sha256"])
        generation = prior["generation"] if same_prefix else hashlib.sha256(
            (file_id + ":" + str(self.indexed_size) + ":" + digest).encode()).hexdigest()
        self.revision = {"file_id": file_id, "size": self.indexed_size,
                         "sha256": digest, "generation": generation}

    @staticmethod
    def signature(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    @property
    def buffer_size(self):
        return (len(self.buffer) + (self.record.buffer_size if self.record else 0)
                + (len(self.checkpoint[3]) + (self.checkpoint[5].buffer_size if self.checkpoint[5] else 0)
                   if self.checkpoint else 0))

    @property
    def identity_count(self):
        return max(self.consumer.background.identity_count,
                   self.working.background.identity_count if self.working else 0)

    def begin(self, signature):
        self.expected = signature
        self.file_identity = signature[:2]
        self.phase = "scan"
        self.offset = 0
        self.buffer = b""
        self.skipping = False
        self.record = None
        self.hasher = hashlib.sha256()
        self.working = self.consumer_factory(self.consumer.session_id, self.consumer.captions)
        self.checkpoint = None
        self.scan_restarts += 1
        self.reset_anchor()

    def verify(self, signature, checkpoint):
        self.expected = signature
        self.file_identity = signature[:2]
        self.phase = "verify"
        self.checkpoint = checkpoint
        self.working = checkpoint[0]
        self.offset = 0
        self.buffer = b""
        self.skipping = False
        self.record = None
        self.hasher = hashlib.sha256()
        self.reset_anchor()

    def discard_uncommitted(self):
        # Only this resource offender loses its in-memory progress. Its last
        # confirmed lifecycle remains intact and its next read starts safely.
        self.expected = None
        self.phase = "idle"
        self.buffer = b""
        self.record = None
        self.checkpoint = None
        self.working = None

    def snapshot(self, checking, detail="Checking local chat history before confirming its status."):
        result = self.consumer.snapshot()
        if not checking and result.get("background_history_unknown"):
            checking = True
            detail = ("Claude's background lifecycle could not be verified from the available history."
                      if self.provider == "Claude" else
                      self.provider + "'s turn lifecycle could not be verified from the available history.")
        result["history_checking"] = checking
        if not checking and self.revision:
            result["history_revision"] = dict(self.revision)
        if checking:
            result["history_checking_detail"] = detail
        return result

    def consume_record(self, event, offset, identity_limit):
        self.working.background.limit = min(BACKGROUND_MAX_IDENTITIES, identity_limit)
        try:
            self.working.consume(event, offset)
        except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
            # Input-derived failures can occur after a consumer has changed a
            # field. Mark the whole history uncertain before saving a resumable
            # checkpoint; a later poll must not commit partial Ready state.
            self.working.unknown = True

    def parse_buffer(self, budget, identity_limit):
        while time.monotonic() < budget.deadline:
            newline = self.buffer.find(b"\n")
            if not self.skipping and (self.record is not None
                                      or (newline if newline >= 0 else len(self.buffer)) > TRANSCRIPT_RECORD_BYTES):
                if self.record is None:
                    self.record = _ProjectedJSONRecord(self.record_fields)
                end = newline if newline >= 0 else len(self.buffer)
                try:
                    consumed = self.record.feed(self.buffer[:end], budget.deadline)
                    self.buffer = self.buffer[consumed:]
                    if consumed != end:
                        return
                    if newline < 0:
                        return
                    event = self.record.finish()
                except (ValueError, UnicodeError, RecursionError):
                    self.working.unknown = True
                    self.record = None
                    # Invalid syntax cannot regain authority through a later
                    # suffix. Skip only to this physical record's newline.
                    newline = self.buffer.find(b"\n")
                    if newline < 0:
                        self.buffer = b""
                        self.skipping = True
                        return
                    self.buffer = self.buffer[newline + 1:]
                    continue
                self.record = None
                self.buffer = self.buffer[1:]
                self.consume_record(event, self.offset - len(self.buffer), identity_limit)
                continue
            if newline < 0:
                if self.skipping:
                    self.buffer = b""
                return
            line, self.buffer = self.buffer[:newline], self.buffer[newline + 1:]
            if self.skipping:
                self.skipping = False
                continue
            if not line.strip():
                continue
            try:
                event = json.loads(line, object_pairs_hook=_transcript_object,
                                   parse_constant=_transcript_constant, parse_float=_transcript_float)
            except (ValueError, UnicodeError, RecursionError):
                self.working.unknown = True
                continue
            # The offset identifies this record's complete newline, independent
            # of chunk sizes or how many polls were needed to reach it.
            self.consume_record(event, self.offset - len(self.buffer), identity_limit)

    def advance(self, path, budget, buffer_limit, identity_limit):
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb", buffering=0) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("Claude transcript is not a regular file")
                signature = self.signature(info)
                if self.signature(path.lstat()) != signature:
                    self.expected = None
                    return self.snapshot(True)
                if self.phase == "idle" and signature == self.indexed_signature:
                    return self.snapshot(False)
                if self.expected != signature:
                    same_file = signature[:2] == self.file_identity
                    if self.phase == "verify" and same_file and signature[2] >= self.checkpoint[1]:
                        self.verify(signature, self.checkpoint)
                    elif self.phase == "scan" and same_file and signature[2] >= self.offset:
                        checkpoint = (self.working, self.offset, self.hasher.copy(), self.buffer, self.skipping,
                                      self.record)
                        self.verify(signature, checkpoint)
                    elif (self.indexed_signature and signature[:2] == self.indexed_signature[:2]
                          and signature[2] >= self.indexed_size):
                        self.verify(signature, (copy.deepcopy(self.consumer), self.indexed_size,
                                                self.indexed_hash.copy(), b"", False, None))
                    else:
                        self.begin(signature)
                while time.monotonic() < budget.deadline:
                    if self.phase == "scan":
                        self.parse_buffer(budget, identity_limit)
                        if time.monotonic() >= budget.deadline:
                            break
                    target = self.checkpoint[1] if self.phase == "verify" else signature[2]
                    if self.offset == target:
                        if (self.signature(os.fstat(stream.fileno())) != signature
                                or self.signature(path.lstat()) != signature):
                            self.expected = None
                            break
                        if self.phase == "verify":
                            if self.hasher.digest() != self.checkpoint[2].digest():
                                self.begin(signature)
                            else:
                                (self.working, self.offset, self.hasher, self.buffer,
                                 self.skipping, self.record) = self.checkpoint
                                self.checkpoint = None
                                self.phase = "scan"
                            continue
                        if self.buffer or self.skipping or self.record is not None:
                            break  # An incomplete write cannot certify Ready.
                        self.consumer = self.working
                        self.indexed_size = self.offset
                        self.indexed_hash = self.hasher.copy()
                        self.indexed_signature = signature
                        self.commit_revision(signature)
                        self.working = None
                        self.checkpoint = None
                        self.phase = "idle"
                        return self.snapshot(False)
                    if budget.remaining <= 0:
                        break
                    read_size = min(TRANSCRIPT_READ_CHUNK, budget.remaining, target - self.offset)
                    if self.phase == "scan":
                        room = max(0, buffer_limit - len(self.buffer)
                                   - (self.record.buffer_size if self.record else 0))
                        if not room:
                            if not self.buffer and self.record is None:
                                break
                            # Preserve bounded memory and explicit uncertainty
                            # instead of dropping a record unnoticed.
                            self.working.unknown = True
                            self.buffer = b""
                            self.record = None
                            self.skipping = True
                            room = buffer_limit
                        read_size = min(read_size, room)
                    stream.seek(self.offset)
                    data = stream.read(read_size)
                    if not data:
                        self.expected = None
                        break
                    self.offset += len(data)
                    budget.remaining -= len(data)
                    if (self.revision and self.anchor_prefix is None
                            and self.offset - len(data) <= self.revision["size"] <= self.offset):
                        prefix = self.hasher.copy()
                        prefix.update(data[:self.revision["size"] - (self.offset - len(data))])
                        self.anchor_prefix = prefix.hexdigest()
                    self.hasher.update(data)
                    if self.phase == "scan":
                        self.buffer += data
                return self.snapshot(True)
        except (OSError, ValueError):
            self.expected = None
            return self.snapshot(True, "Local chat history could not be read. Retrying before confirming its status.")


class ClaudeSource:
    def __init__(self, root: Path | None = None, desktop_root: Path | None = None,
                 allow_commands: bool = True, confine_paths: bool = False,
                 desktop_scope_root: Path | None = None):
        default = Path.home() / ".claude"
        self.root = Path(root) if root is not None else default
        self.confine_paths = confine_paths
        # Fixture/custom roots must never accidentally query the user's sessions.
        use_desktop = root is None or desktop_root is not None
        self.command = _find_claude() if use_desktop and allow_commands else None
        self.app_available = use_desktop and any(path.is_dir() for path in
            (Path("/Applications/Claude.app"), Path.home() / "Applications/Claude.app"))
        self.desktop_root = (Path(desktop_root) if desktop_root is not None else
                             Path.home() / "Library/Application Support/Claude/claude-code-sessions"
                             if use_desktop else None)
        self.desktop_scope_root = (Path(desktop_scope_root) if desktop_scope_root is not None else self.desktop_root)
        self.desktop_only = use_desktop
        self.background_reasons: dict[str, str] = {}
        self.classifications: dict[str, str] = {}
        self._desktop_index_complete = False
        self._cli_at = float("-inf")
        self._cli_rows: dict[str, dict] = {}
        self._cli_ok = False
        self._tails: dict[Path, _TranscriptCursor] = {}
        self._scan_rotation = 0
        self._cursor_tick = 0
        self._desktop_headers: dict[Path, tuple[tuple[int, int], dict]] = {}
        # The board supplies its own already tracked IDs. This is a metadata
        # refresh allowlist, not permission to import every saved conversation.
        self.tracked_ids: set[str] = set()
        # The board's own checkpoint contains hashes and counters, never chat
        # text. A lazy lookup keeps this bounded to resident transcript cursors.
        self.revision_lookup = None

    def _desktop_sessions(self, wanted_ids: set[str] | None = None) -> dict[str, dict]:
        """Index bounded Desktop headers, then verify flags for relevant sessions.

        Every header is indexed so an older tracked mapping cannot disappear
        behind a recent-files cap. Only relevant metadata objects are decoded in
        full, with a separate size bound, to read top-level project-child flags.
        No conversation transcript is read by this method.
        """
        self._desktop_index_complete = False
        if not self.desktop_root:
            return {}
        if self.confine_paths:
            from source_access import require_source_directory
            try:
                require_source_directory(self.desktop_root, self.desktop_scope_root)
            except (OSError, ValueError):
                return {}
        elif not self.desktop_root.is_dir():
            return {}
        sessions: dict[str, dict] = {}
        complete = True
        try:
            candidates = []
            rejected = []
            if self.confine_paths:
                from source_access import source_glob
                prefix = self.desktop_root.absolute().relative_to(self.desktop_scope_root.absolute())
                pattern = str(prefix / "*/*/local_*.json")
                paths = source_glob(self.desktop_scope_root, pattern, rejected)
            else:
                paths = self.desktop_root.glob("*/*/local_*.json")
            for path in paths:
                try:
                    if self.confine_paths:
                        from source_access import require_source_path
                        require_source_path(path, self.desktop_scope_root)
                    info = path.stat()
                    if not stat.S_ISREG(info.st_mode):
                        complete = False
                        continue
                    candidates.append((info.st_mtime, path))
                except (OSError, ValueError):
                    complete = False
                    continue
            if rejected:
                complete = False
            paths = [path for _, path in sorted(candidates, key=lambda item: item[0], reverse=True)]
        except OSError:
            return sessions
        decoder = json.JSONDecoder()
        for path in paths:
            try:
                if self.confine_paths:
                    from source_access import require_source_path
                    require_source_path(path, self.desktop_scope_root)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    complete = False
                    continue
                signature = (info.st_mtime_ns, info.st_size)
                cached = self._desktop_headers.get(path)
                if cached and cached[0] == signature:
                    fields = cached[1]
                else:
                    with path.open("rb") as stream:
                        header = stream.read(DESKTOP_HEADER_BYTES).decode("utf-8", errors="replace")
                    fields = {}
                    seen = set()
                    index = len(header) - len(header.lstrip())
                    if header[index:index + 1] == "{":
                        index += 1
                        while index < len(header):
                            index += len(header[index:]) - len(header[index:].lstrip())
                            key, index = decoder.raw_decode(header, index)
                            index += len(header[index:]) - len(header[index:].lstrip())
                            if header[index:index + 1] != ":":
                                break
                            index += 1
                            index += len(header[index:]) - len(header[index:].lstrip())
                            value, index = decoder.raw_decode(header, index)
                            seen.add(key)
                            if key in ("sessionId", "cliSessionId", "title") and isinstance(value, str):
                                fields[key] = _short(value, 160)
                            elif key == "cwd" and isinstance(value, str):
                                fields[key] = _short(value, 2048)
                            elif key == "isArchived" and isinstance(value, bool):
                                fields[key] = value
                            elif key == "createdAt" and isinstance(value, (int, float)) and not isinstance(value, bool):
                                fields[key] = value
                            if {"sessionId", "cliSessionId", "title"} <= seen:
                                fields["_header_complete"] = True
                                break
                            index += len(header[index:]) - len(header[index:].lstrip())
                            if header[index:index + 1] != ",":
                                break
                            index += 1
                    self._desktop_headers[path] = (signature, fields)
                if not fields.get("_header_complete"):
                    complete = False
                if fields.get("cliSessionId"):
                    if (wanted_ids is None or fields["cliSessionId"] in wanted_ids) and not fields.get("_classification_checked"):
                        # Flags live after large tool configuration snapshots.
                        # Decode the actual top-level object, not a regex match
                        # that could accidentally classify nested user content.
                        try:
                            with path.open("rb") as stream:
                                raw = stream.read(DESKTOP_METADATA_BYTES + 1)
                            if len(raw) > DESKTOP_METADATA_BYTES:
                                raise ValueError("Desktop metadata exceeds the read bound")
                            metadata = json.loads(raw)
                            if not isinstance(metadata, dict) or metadata.get("cliSessionId") != fields["cliSessionId"]:
                                raise ValueError("Desktop identity changed while reading")
                            spawn = metadata.get("remoteControlSpawn")
                            spawn = spawn if isinstance(spawn, dict) else {}
                            fields["projectThreadChild"] = spawn.get("projectThreadChild") is True
                            fields["rcChild"] = spawn.get("rcChild")
                            fields["isArchived"] = metadata.get("isArchived") is True
                            fields["_classification_checked"] = True
                        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
                            fields["_classification_checked"] = False
                    sessions.setdefault(fields["cliSessionId"], fields)
            except (OSError, ValueError, TypeError, RecursionError):
                complete = False
                continue
        self._desktop_index_complete = complete
        return sessions

    def _live_sessions(self, now: float) -> tuple[dict[str, dict], bool]:
        if not self.command:
            return {}, False
        if now - self._cli_at < CLI_POLL_SECONDS:
            return self._cli_rows, self._cli_ok
        self._cli_at = now
        self._cli_ok = False
        self._cli_rows = {}
        try:
            from process_reader import read_process

            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("OTEL_") and key != "CLAUDE_CODE_ENABLE_TELEMETRY"}
            environment.update({"CLAUDE_CONFIG_DIR": str(self.root),
                                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                                "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
                                "DISABLE_AUTOUPDATER": "1", "OTEL_SDK_DISABLED": "true"})
            result = read_process(
                [self.command, "agents", "--json", "--all"],
                timeout=3, max_output=8 * 1024 * 1024,
                env=environment,
            )
            if result.returncode != 0 or len(result.stdout) > 8 * 1024 * 1024:
                return {}, False
            rows = json.loads(result.stdout)
            if not isinstance(rows, list):
                return {}, False
            self._cli_rows = {
                str(row.get("sessionId") or row.get("id")): row
                for row in rows if isinstance(row, dict) and (row.get("sessionId") or row.get("id"))
            }
            self._cli_ok = True
        except (OSError, ValueError, subprocess.SubprocessError, RecursionError):
            pass
        return self._cli_rows, self._cli_ok

    def _registry(self) -> dict[str, dict]:
        rows: dict[str, dict] = {}
        if self.confine_paths:
            from source_access import source_glob
            paths = source_glob(self.root, "sessions/*.json")
        else:
            paths = (self.root / "sessions").glob("*.json")
        for path in paths:
            try:
                if self.confine_paths:
                    from source_access import require_source_path
                    require_source_path(path, self.root)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024:
                    continue
                row = json.loads(path.read_text())
                if not isinstance(row, dict) or not row.get("sessionId"):
                    continue
                row = dict(row)
                row["_mtime"] = path.stat().st_mtime
                key = str(row["sessionId"])
                if _timestamp(row.get("updatedAt"), row["_mtime"]) >= _timestamp(rows.get(key, {}).get("updatedAt")):
                    rows[key] = row
            except (OSError, ValueError, RecursionError):
                continue
        return rows

    def _transcript(self, path: Path, captions: bool = True, budget=None) -> dict:
        if self.confine_paths:
            from source_access import require_source_path
            try:
                require_source_path(path, self.root)
            except (OSError, ValueError):
                return {"id": path.stem, "status": "disconnected", "updated_at": 0,
                        "history_checking": True,
                        "history_checking_detail": "Local chat history is outside the authorized folder."}
        cursor = self._tails.get(path)
        if cursor is None or cursor.consumer.captions != captions:
            if cursor is None and len(self._tails) >= TRANSCRIPT_MAX_FILES:
                if not self._tails:
                    return {"id": path.stem, "status": "disconnected", "updated_at": 0,
                            "history_checking": True, "history_checking_detail": "Local history indexing reached its resource limit."}
                oldest = min(self._tails, key=lambda key: self._tails[key].last_used)
                self._tails.pop(oldest)
            revision = self.revision_lookup(path.stem) if self.revision_lookup else None
            cursor = _TranscriptCursor(path.stem, captions, revision)
            self._tails[path] = cursor
        self._cursor_tick += 1
        cursor.last_used = self._cursor_tick
        budget = budget or _TranscriptBudget()
        other_buffers = sum(item.buffer_size for item in self._tails.values() if item is not cursor)
        reservation = min(TRANSCRIPT_RECORD_BYTES + TRANSCRIPT_READ_CHUNK, TRANSCRIPT_MAX_BUFFER_BYTES)
        if other_buffers > TRANSCRIPT_MAX_BUFFER_BYTES - reservation:
            offenders = [(other.buffer_size, -other.last_used, str(name), other)
                         for name, other in self._tails.items() if other is not cursor and other.buffer_size]
            while other_buffers > TRANSCRIPT_MAX_BUFFER_BYTES - reservation and offenders:
                size, _, _, offender = max(offenders, key=lambda entry: entry[:3])
                offender.discard_uncommitted()
                other_buffers -= size
                offenders = [entry for entry in offenders if entry[3] is not offender]
        other_identities = sum(item.identity_count for item in self._tails.values() if item is not cursor)
        return cursor.advance(path, budget, max(0, TRANSCRIPT_MAX_BUFFER_BYTES - other_buffers),
                              max(0, TRANSCRIPT_MAX_IDENTITIES - other_identities))

    def scan(self, now: float, cutoff: float) -> tuple[list[dict], dict]:
        registry = self._registry()
        live, cli_ok = self._live_sessions(now)
        wanted_ids = self.tracked_ids | registry.keys() | live.keys()
        transcript_paths: dict[str, Path] = {}
        transcript_mtimes: dict[str, int] = {}
        ambiguous_ids: set[str] = set()
        project_root = self.root / "projects"
        try:
            # Exactly two levels: nested subagent/workflow logs are excluded.
            if self.confine_paths:
                from source_access import source_glob
                paths = source_glob(self.root, "projects/*/*.jsonl")
            else:
                paths = project_root.glob("*/*.jsonl")
            for path in paths:
                try:
                    if self.confine_paths:
                        from source_access import require_source_path
                        require_source_path(path, self.root)
                    info = path.stat()
                    if (info.st_mtime < cutoff and path.stem not in registry and path.stem not in live
                            and path.stem not in self.tracked_ids):
                        continue
                    if path.stem in transcript_paths and transcript_paths[path.stem] != path:
                        ambiguous_ids.add(path.stem)
                    transcript_paths[path.stem] = path
                    transcript_mtimes[path.stem] = max(info.st_mtime_ns, transcript_mtimes.get(path.stem, 0))
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        wanted_ids |= transcript_paths.keys()
        desktop_sessions = self._desktop_sessions(wanted_ids)
        self.background_reasons = {}
        self.classifications = {}
        eligible_ids = set()
        for session_id in wanted_ids:
            desktop = desktop_sessions.get(session_id)
            reason = None
            if desktop and desktop.get("projectThreadChild") is True and desktop.get("rcChild") is True:
                reason = "internal_project_worker"
            elif self.desktop_only:
                if desktop and desktop.get("isArchived") is True:
                    reason = "archived_desktop_session"
                elif desktop is None:
                    if self._desktop_index_complete:
                        reason = "not_a_desktop_code_session"
                    else:
                        self.classifications[session_id] = "desktop_metadata_unavailable"
                elif (desktop.get("_classification_checked") and
                      _LOCAL_SESSION.fullmatch(desktop.get("sessionId", ""))):
                    eligible_ids.add(session_id)
                    self.classifications[session_id] = "desktop_code_session"
                else:
                    self.classifications[session_id] = "desktop_metadata_unavailable"
            else:
                eligible_ids.add(session_id)
                self.classifications[session_id] = "fixture_or_cli_session"
            if reason:
                self.background_reasons[session_id] = reason
                self.classifications[session_id] = reason
                eligible_ids.add(session_id)
        states: dict[str, dict] = {}
        for session_id in ambiguous_ids & eligible_ids:
            states[session_id] = {"id": session_id, "status": "disconnected",
                                  "updated_at": transcript_mtimes[session_id] / 1_000_000_000,
                                  "history_checking": True,
                                  "history_checking_detail": "Multiple local histories share this chat identity. Its current state could not be verified."}
        active_ids = {session_id for session_id, row in live.items()
                      if type(row.get("pid")) is int and row["pid"] > 0
                      and row.get("status") in ("busy", "waiting")}
        eligible_paths = sorted(((session_id, path) for session_id, path in transcript_paths.items()
                                 if session_id in eligible_ids and session_id not in ambiguous_ids),
                                key=lambda pair: (pair[0] in active_ids, transcript_mtimes.get(pair[0], 0), pair[0]), reverse=True)
        resident_paths = eligible_paths[:TRANSCRIPT_MAX_FILES]
        for session_id, path in eligible_paths[TRANSCRIPT_MAX_FILES:]:
            states[session_id] = {"id": session_id, "status": "disconnected", "updated_at": 0,
                                  "history_checking": True,
                                  "history_checking_detail": "Local history indexing reached its resource limit."}
        eligible_paths = resident_paths
        retained = {path for _, path in resident_paths}
        self._tails = {path: cursor for path, cursor in self._tails.items() if path in retained}
        budget = _TranscriptBudget()
        if eligible_paths:
            offset = self._scan_rotation % len(eligible_paths)
            eligible_paths = eligible_paths[offset:] + eligible_paths[:offset]
            self._scan_rotation += 1
        for session_id, path in eligible_paths:
            if session_id not in eligible_ids:
                continue
            try:
                states[session_id] = dict(self._transcript(path, captions=session_id not in self.background_reasons,
                                                          budget=budget))
            except OSError:
                continue
        observations = []
        metadata_only_ids = self.tracked_ids & desktop_sessions.keys()
        for session_id in states.keys() | registry.keys() | live.keys() | metadata_only_ids:
            if session_id not in eligible_ids:
                continue
            transcript = states.get(session_id, {})
            registered = registry.get(session_id, {})
            current = live.get(session_id)
            desktop = desktop_sessions.get(session_id, {})
            row = {**registered, **(current or {})}
            stamp = _timestamp(registered.get("statusUpdatedAt"), _timestamp(registered.get("updatedAt"), registered.get("_mtime", 0)))
            updated = max(transcript.get("updated_at", 0), stamp, _timestamp(row.get("startedAt")))
            status = transcript.get("status", "disconnected")
            detail = transcript.get("detail", "No completed reply was recorded.")
            token = transcript.get("event_id") or "unobserved"
            proc = _process_alive(registered.get("pid")) if registered else None
            if (self.background_reasons.get(session_id) == "not_a_desktop_code_session"
                    and not transcript.get("event_id") and session_id not in self.tracked_ids):
                # A transient CLI process with no prompt/reply lifecycle is not
                # a conversation to import. Existing board records are retained.
                self.background_reasons.pop(session_id, None)
                self.classifications[session_id] = "empty_cli_helper"
                continue
            # CLI rows are verified live; file-only registry rows can be stale.
            verified = current is not None
            # --all also returns completed background sessions whose processes
            # have exited. The supported CLI documents pid/status as present
            # only while a process is alive, so existence in its list alone
            # must not re-import an old completed conversation.
            confirmed_live = bool(current and isinstance(current.get("pid"), int)
                                  and current["pid"] > 0
                                  and current.get("status") in ("busy", "waiting", "idle"))
            registry_usable = bool(registered) and not cli_ok and (
                proc is True or (proc is None and now - stamp <= REGISTRY_FRESH_SECONDS))
            status_row = current if verified else registered if registry_usable else {}
            process_status = status_row.get("status")
            task_state = status_row.get("state")
            history_checking = (transcript.get("history_checking") is True
                and task_state not in ("failed", "stopped", "blocked", "working")
                and process_status not in ("busy", "waiting"))
            if task_state in ("failed", "stopped"):
                status, detail = "disconnected", "Claude's task failed or was stopped."
                token = "state:" + str(task_state) + ":" + str(stamp) + ":" + token
            elif task_state == "blocked" or process_status == "waiting":
                status = "needs_you"
                waiting = _short(status_row.get("waitingFor"), 80)
                detail = "Claude is waiting for " + waiting + "." if waiting else "Claude needs your input."
                token = "waiting:" + str(stamp) + ":" + token
            elif task_state == "working" or process_status == "busy":
                status, detail = "cooking", "Claude is working."
                token = "working:" + str(stamp) + ":" + token
            elif transcript.get("background_pending") and status == "cooking":
                # Foreground idle/done does not settle an asynchronous tool.
                # Nor does an alive idle process prove that tool stays alive.
                if now - transcript.get("updated_at", 0) > INFERRED_ACTIVE_SECONDS:
                    status, detail = "disconnected", "Claude's background work could not be confirmed."
                else:
                    detail = "Claude has unresolved background work. This is inferred from recent local activity."
            elif (task_state == "done" and not transcript.get("background_pending")
                  and not transcript.get("background_failed")
                  and not transcript.get("background_history_unknown") and not history_checking):
                status, detail = "ready", "Claude finished responding."
            elif (process_status == "idle" and status in ("cooking", "needs_you")
                  and not transcript.get("background_pending")):
                status, detail = "disconnected", "Claude is idle; no completed reply was recorded."
            elif status in ("cooking", "needs_you") and not registry_usable:
                # Transcript activity alone does not prove a process stays alive.
                if (cli_ok and not verified) or proc is False or now - transcript.get("updated_at", 0) > INFERRED_ACTIVE_SECONDS:
                    status, detail = "disconnected", "Claude's current state could not be confirmed."
                else:
                    detail += " This is inferred from recent local activity."
            if history_checking:
                status = "disconnected"
                detail = transcript.get("history_checking_detail", "Checking local chat history before confirming its status.")
            if (updated < cutoff and not verified and session_id not in self.tracked_ids
                    and not (registry_usable and process_status in ("busy", "waiting"))):
                continue
            cwd = _short(row.get("cwd") or transcript.get("cwd") or desktop.get("cwd"), 2048)
            candidates = ((desktop.get("title"), "desktop"), (row.get("name"), "cli"),
                          (transcript.get("title"), "transcript"))
            title, title_source = next(((_short(value), source) for value, source in candidates
                                        if _useful_title(value, cwd)), ("", ""))
            if not title:
                title = transcript.get("prompt_title") or (Path(cwd).name if cwd else "Claude Code session")
                title_source = transcript.get("caption_source", "prompt") if transcript.get("prompt_title") else "project"
                if title_source == "project":
                    # Keep otherwise unnamed sessions distinguishable without
                    # exposing machine instructions as a pretend task title.
                    title_stamp = (_timestamp(desktop.get("createdAt")) or _timestamp(row.get("startedAt"))
                                   or transcript.get("started_at") or updated)
                    if title_stamp:
                        try:
                            label = datetime.fromtimestamp(title_stamp).strftime("%b %-d, %-I:%M %p")
                        except (OSError, OverflowError, ValueError):
                            label = session_id[:8]
                        title = title[:1].upper() + title[1:] + " · " + label
                        title_source = "project_time"
            local_id = desktop.get("sessionId", "")
            open_url = ("claude://code/continue?" + urlencode({"session": local_id})
                        if self.app_available and not desktop.get("isArchived")
                        and _LOCAL_SESSION.fullmatch(local_id) else "")
            preview_fields = {}
            if (transcript.get("preview") and
                    ((status == "ready" and transcript.get("preview_kind") == "reply") or
                     (status == "needs_you" and transcript.get("preview_kind") == "permission"))):
                preview_fields = {field: transcript[field] for field in ("preview", "preview_kind", "preview_at")}
            if status == "needs_you" and status_row.get("waitingFor"):
                waiting_preview = _literal_preview(status_row["waitingFor"])
                if waiting_preview:
                    preview_fields = {"preview": waiting_preview, "preview_kind": "permission", "preview_at": stamp}
            observations.append({
                "key": "claude:" + session_id, "id": session_id, "provider": "claude",
                "title": title, "cwd": cwd, "status": status, "updated_at": updated,
                "started_at": transcript.get("started_at") or _timestamp(row.get("startedAt")) or None,
                "detail": detail, "can_open": bool(open_url),
                "confirmed_live": confirmed_live,
                "history_checking": history_checking,
                "history_verified": bool(transcript.get("history_revision")),
                **({"history_revision": transcript["history_revision"]} if transcript.get("history_revision") else {}),
                # This row can refresh an existing caption/link, but it cannot
                # confirm that previously stored work is still present/live.
                "metadata_only": not bool(transcript.get("event_id") or current is not None or registry_usable),
                "open_kind": "url", "open_label": "Open Claude", "open_url": open_url,
                "title_source": title_source,
                **({"background_reason": self.background_reasons[session_id]}
                   if session_id in self.background_reasons else {}),
                "event_id": "claude:" + session_id + ":" + status + ":" + token,
                "turn_id": transcript.get("turn_id"),
                **preview_fields,
            })
        last_seen = max((item["updated_at"] for item in observations), default=None)
        source = {"id": "claude", "name": "Claude Code", "available": self.root.is_dir() or cli_ok,
                  "detail": ("Reading status from the Claude Code CLI." if cli_ok else
                             "Reading local session metadata." if self.root.is_dir() else
                             "No local Claude Code data was found."),
                  "last_seen": last_seen}
        return observations, source
