"""Bound local read-only helpers and clean up their entire process group."""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time
import atexit

_processes: set[subprocess.Popen] = set()
_lock = threading.Lock()
_watchdog_lock = threading.RLock()
_watchdog: subprocess.Popen | None = None
_watchdog_write: int | None = None
_stopping = False


class OutputLimitExceeded(subprocess.SubprocessError):
    pass


def start_process(args, **kwargs) -> subprocess.Popen:
    # Start the death watcher before creating a reader. Its only input is a
    # private descriptor with numeric process-group registrations, never data.
    with _watchdog_lock:
        if _stopping:
            raise OSError("The local reader is shutting down.")
        _ensure_watchdog()
        gate_read, gate_write = os.pipe()
        command = [sys.executable, *([] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve())]),
                   "--reader-launch", str(gate_read), "--", *args]
        process = None
        try:
            inherited = (*kwargs.pop("pass_fds", ()), gate_read)
            process = subprocess.Popen(command, start_new_session=True, pass_fds=inherited, **kwargs)
            os.write(_watchdog_write, f"+{process.pid}\n".encode("ascii"))
            # The target cannot execute before its group is registered. If this
            # process dies first, gate EOF makes the wrapper exit harmlessly.
            os.write(gate_write, b"G")
        except OSError:
            if process:
                stop_process(process)
            raise OSError("The local reader could not be supervised.") from None
        finally:
            os.close(gate_read)
            os.close(gate_write)
        with _lock:
            _processes.add(process)
        return process


def launch_reader(fd: int, args: list[str]) -> None:
    """Gate a target exec until the supervising collector registered its group."""
    try:
        go = os.read(fd, 1)
    finally:
        os.close(fd)
    if go != b"G" or not args:
        return
    try:
        os.execvpe(args[0], args, os.environ)
    except OSError:
        os._exit(127)


def _ensure_watchdog() -> None:
    global _watchdog, _watchdog_write
    if _watchdog is not None and _watchdog.poll() is None:
        return
    if _watchdog_write is not None:
        os.close(_watchdog_write)
        _watchdog_write = None
    read_fd, write_fd = os.pipe()
    args = [sys.executable, *([] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve())]),
            "--reader-watchdog", str(read_fd)]
    try:
        _watchdog = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, pass_fds=(read_fd,),
                                     start_new_session=True)
        _watchdog_write = write_fd
        with _lock:
            active = tuple(_processes)
        # Replacing an unexpectedly stopped watcher also restores supervision
        # of readers that were already running before that replacement.
        for process in active:
            os.write(write_fd, f"+{process.pid}\n".encode("ascii"))
    except BaseException:
        os.close(write_fd)
        _watchdog_write = None
        raise
    finally:
        os.close(read_fd)


def watch_reader_groups(fd: int) -> None:
    """Detached helper: EOF also arrives after abrupt collector death."""
    groups: set[int] = set()
    try:
        with os.fdopen(fd, "rb", buffering=0) as pipe:
            while True:
                record = pipe.readline(64)
                if not record:
                    break
                if not record.endswith(b"\n") or len(record) > 32:
                    break
                action, number = record[:1], record[1:-1]
                if action not in (b"+", b"-") or not number.isdigit():
                    break
                group = int(number)
                if not 1 < group <= 2**31 - 1 or group == os.getpgrp():
                    break
                if action == b"+":
                    groups.add(group)
                else:
                    groups.discard(group)
                if len(groups) > 4096:
                    break
    finally:
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def stop_process(process: subprocess.Popen) -> None:
    """Also stop descendants which kept inherited pipes open after parent exit."""
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, signum)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=0.2)
        except (OSError, subprocess.TimeoutExpired):
            pass
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe:
            try:
                pipe.close()
            except OSError:
                pass
    with _lock:
        _processes.discard(process)
    with _watchdog_lock:
        if _watchdog_write is not None:
            try:
                os.write(_watchdog_write, f"-{process.pid}\n".encode("ascii"))
            except OSError:
                pass


def stop_all_processes() -> None:
    """Collector shutdown calls this before exiting, including SIGTERM."""
    global _stopping, _watchdog_write
    with _watchdog_lock:
        _stopping = True
        with _lock:
            processes = tuple(_processes)
    for process in processes:
        stop_process(process)
    with _watchdog_lock:
        if _watchdog_write is not None:
            try:
                os.close(_watchdog_write)
            except OSError:
                pass
            _watchdog_write = None
    if _watchdog is not None:
        try:
            _watchdog.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def read_process(args, *, input: bytes = b"", timeout: float, max_output: int,
                 env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Drain bounded stdout while feeding stdin without a blocking pipe write.

    stderr is discarded because third-party helper diagnostics may contain
    account data. No captured output is included in timeout/limit exceptions.
    """
    process = start_process(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env, bufsize=0)
    output = bytearray()
    position = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            if input:
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                for key, _ in selector.select(remaining):
                    if key.fileobj is process.stdin:
                        try:
                            position += os.write(process.stdin.fileno(), input[position:position + 65536])
                        except BrokenPipeError:
                            position = len(input)
                        if position == len(input):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(process.stdout.fileno(), min(65536, max_output + 1 - len(output)))
                        if not chunk:
                            selector.unregister(process.stdout)
                        else:
                            output.extend(chunk)
                            if len(output) > max_output:
                                raise OutputLimitExceeded("The local reader exceeded its output limit.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            process.wait(timeout=remaining)
            return subprocess.CompletedProcess(args, process.returncode, bytes(output), b"")
    finally:
        stop_process(process)


atexit.register(stop_all_processes)

if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--reader-watchdog":
    watch_reader_groups(int(sys.argv[2]))
elif __name__ == "__main__" and len(sys.argv) >= 5 and sys.argv[1] == "--reader-launch" and sys.argv[3] == "--":
    launch_reader(int(sys.argv[2]), sys.argv[4:])
