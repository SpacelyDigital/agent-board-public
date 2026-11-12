"""Resolve per-launch, read-only source grants inside the sandboxed collector.

App Sandbox inheritance excludes dynamically granted file access. The native
owner therefore sends transferable URL bookmarks, and the receiving collector
resolves them in its own process before constructing any source readers.
This module never locates provider folders, reads credentials, or grants access.
"""

import atexit
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


_SCOPES = []


def _foundation():
    if sys.platform != "darwin":
        raise ValueError("Sandbox source grants require macOS.")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
    cf.CFDataCreate.restype = ctypes.c_void_p
    cf.CFURLCreateByResolvingBookmarkData.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_bool), ctypes.POINTER(ctypes.c_void_p)]
    cf.CFURLCreateByResolvingBookmarkData.restype = ctypes.c_void_p
    cf.CFURLGetFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p, ctypes.c_long]
    cf.CFURLGetFileSystemRepresentation.restype = ctypes.c_bool
    cf.CFURLStartAccessingSecurityScopedResource.argtypes = [ctypes.c_void_p]
    cf.CFURLStartAccessingSecurityScopedResource.restype = ctypes.c_bool
    cf.CFURLStopAccessingSecurityScopedResource.argtypes = [ctypes.c_void_p]
    cf.CFURLStopAccessingSecurityScopedResource.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    return cf


def release_source_bookmarks():
    while _SCOPES:
        cf, url = _SCOPES.pop()
        cf.CFURLStopAccessingSecurityScopedResource(url)
        cf.CFRelease(url)


atexit.register(release_source_bookmarks)


def activate_source_bookmarks(manifest_path, expected_roots, support, observation=None, *, require_unselected=False):
    """Validate the native manifest and retain transferred scopes until exit.

    Unselected sources must point at dedicated empty folders in the app's own
    container. There is no fallback to a real home directory on any failure.
    Call this only in the strict sandbox candidate, before constructing readers.
    """
    support = Path(support).resolve(strict=True)
    manifest_path = Path(manifest_path)
    if manifest_path.is_symlink() or manifest_path.parent.resolve(strict=True) != support / "data":
        raise ValueError("Invalid sandbox source manifest location.")
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate source manifest field.")
            value[key] = item
        return value
    descriptor = os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or metadata.st_size > 262_144):
            raise ValueError("Sandbox source manifest is not a private regular file.")
        raw_manifest = bytearray()
        while len(raw_manifest) <= 262_144:
            chunk = os.read(descriptor, min(65_536, 262_145 - len(raw_manifest)))
            if not chunk:
                break
            raw_manifest.extend(chunk)
        if len(raw_manifest) > 262_144:
            raise ValueError("Sandbox source manifest is too large.")
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if signature(metadata) != signature(os.fstat(descriptor)):
            raise ValueError("Sandbox source manifest changed while being read.")
        fingerprint = signature(metadata) + (hashlib.sha256(raw_manifest).digest(),)
    finally:
        os.close(descriptor)
    value = json.loads(raw_manifest.decode("utf-8"), object_pairs_hook=object_pairs)
    providers = {"codex", "claude", "claude_desktop"}
    if (not isinstance(value, dict) or set(value) != {"version", "sources"}
            or type(value.get("version")) is not int or value["version"] != 1 or not isinstance(value.get("sources"), dict)
            or set(value["sources"]) != providers):
        raise ValueError("Invalid sandbox source manifest.")
    if set(expected_roots) != providers:
        raise ValueError("Sandbox launches require every explicit source root.")
    cf = None
    activated = {}
    try:
        for provider in ("codex", "claude", "claude_desktop"):
            expected = Path(expected_roots[provider])
            if not expected.is_absolute() or ".." in expected.parts:
                raise ValueError("Sandbox source paths must be absolute and canonical.")
            item = value["sources"][provider]
            if (not isinstance(item, dict) or set(item) - {"path", "bookmark"}
                    or not isinstance(item.get("path"), str) or len(item["path"]) > 32768
                    or Path(item["path"]) != expected):
                raise ValueError("Sandbox source path does not match its native grant.")
            if require_unselected and "bookmark" in item:
                raise ValueError("Sample mode cannot activate a provider source bookmark.")
            bookmark = item.get("bookmark")
            if not bookmark:
                sentinel = support / "sources-unavailable" / provider
                if (expected != sentinel or expected.parent.is_symlink() or expected.is_symlink()
                        or "bookmark" in item or any(expected.iterdir())):
                    raise ValueError("An unselected source must use its empty container folder.")
                activated[provider] = False
                continue
            if not isinstance(bookmark, str):
                raise ValueError("Invalid source bookmark.")
            if provider == "claude_desktop" and expected.name != "claude-code-sessions":
                raise ValueError("Choose the Claude Code sessions folder again; the previous app folder grant is no longer used.")
            raw = base64.b64decode(bookmark, validate=True)
            if not raw or len(raw) > 65_536:
                raise ValueError("Invalid source bookmark size.")
            cf = cf or _foundation()
            buffer = ctypes.create_string_buffer(raw)
            data = cf.CFDataCreate(None, buffer, len(raw))
            if not data:
                raise ValueError("Source bookmark could not be loaded.")
            error = ctypes.c_void_p()
            stale = ctypes.c_bool()
            # No UI; defer implicit transfer consumption until the explicit
            # start call below, which is balanced at process shutdown.
            url = cf.CFURLCreateByResolvingBookmarkData(None, data, (1 << 8) | (1 << 15), None,
                                                       None, ctypes.byref(stale), ctypes.byref(error))
            cf.CFRelease(data)
            if error.value:
                cf.CFRelease(error)
            if not url:
                raise ValueError("Source bookmark could not be resolved.")
            path_buffer = ctypes.create_string_buffer(32768)
            started = False
            try:
                if not cf.CFURLStartAccessingSecurityScopedResource(url):
                    raise ValueError("Source folder access is unavailable. Select the folder again.")
                started = True
                if not cf.CFURLGetFileSystemRepresentation(url, True, path_buffer, len(path_buffer)):
                    raise ValueError("Source bookmark path is unavailable.")
                resolved = Path(path_buffer.value.decode("utf-8")).resolve(strict=True)
                if resolved != expected:
                    raise ValueError("Resolved source grant does not match its native path.")
            except Exception:
                if started:
                    cf.CFURLStopAccessingSecurityScopedResource(url)
                cf.CFRelease(url)
                raise
            _SCOPES.append((cf, url))
            activated[provider] = True
        if observation is not None:
            observation["fingerprint"] = fingerprint
        return activated
    except Exception:
        release_source_bookmarks()
        raise
