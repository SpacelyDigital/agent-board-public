#!/usr/bin/env python3
"""Measure idle collector disk writes with disposable data and no provider access.

Run on macOS: python3 tests/collector_disk_budget.py --seconds 120
The default acceptance limit is 10 KiB/s. This bounded fixture is separate from
the release requirement to observe an installed app for an hour of normal use.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_source import CodexSource
from server import Monitor


class RusageInfoV2(ctypes.Structure):
    # Exact RUSAGE_INFO_V2 layout from the macOS SDK sys/resource.h.
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups",
        "ri_pageins", "ri_wired_size", "ri_resident_size", "ri_phys_footprint",
        "ri_proc_start_abstime", "ri_proc_exit_abstime", "ri_child_user_time",
        "ri_child_system_time", "ri_child_pkg_idle_wkups", "ri_child_interrupt_wkups",
        "ri_child_pageins", "ri_child_elapsed_abstime", "ri_diskio_bytesread", "ri_diskio_byteswritten")]


def disk_written():
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pid_rusage.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    library.proc_pid_rusage.restype = ctypes.c_int
    result = RusageInfoV2()
    if library.proc_pid_rusage(os.getpid(), 2, ctypes.byref(result)) != 0:
        raise OSError("The fixture's own disk counter could not be read.")
    return result.ri_diskio_byteswritten


class FixtureCodex(CodexSource):
    def __init__(self, root, rows):
        super().__init__(root)
        self.rows = rows

    def scan(self, now, cutoff):
        return self.rows, {"id": "codex", "name": "Codex", "available": True, "last_seen": now}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--rows", type=int, default=200)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("The disk counter measurement requires macOS.")
    if not 2 <= args.seconds <= 3600 or not 1 <= args.rows <= 500:
        parser.error("Use 2 to 3600 seconds and 1 to 500 disposable rows.")
    with tempfile.TemporaryDirectory(prefix="agent-board-disk-fixture-") as folder:
        root = Path(folder)
        monitor = Monitor(root / "data", codex_root=root / "codex", claude_root=root / "claude",
                          usage_enabled=False, projects_enabled=False, interval=2)
        stamp = monitor.store.activated_at + .001
        rows = [dict(provider="codex", id=f"fixture-{i}", title=f"Disposable fixture {i}",
                     status="cooking" if i % 2 else "ready", event_id="stable-event",
                     updated_at=stamp, detail="Unchanged fixture state.",
                     preview="A disposable reply. " * 40 if not i % 2 else "") for i in range(args.rows)]
        monitor.sources = [FixtureCodex(root / "codex", rows)]
        try:
            monitor.scan()
            assert len(monitor.snapshot()["sessions"]) == args.rows
            initial_changes = monitor.store._db.total_changes
            wal = Path(str(monitor.store.path) + "-wal")
            initial_wal = wal.stat().st_size
            initial_written = disk_written()
            start = time.monotonic()
            deadline = start + args.seconds
            scans = 0
            print(json.dumps({"phase": "measuring", "seconds": args.seconds, "rows": args.rows,
                              "provider_reads": 0, "live_data_writes": False}), flush=True)
            while time.monotonic() < deadline:
                tick = time.monotonic()
                monitor.scan()
                snapshot = monitor.snapshot()
                assert len(snapshot["sessions"]) == args.rows
                assert monitor.refresh_health()["status"]
                scans += 1
                time.sleep(max(0, min(deadline, tick + 2) - time.monotonic()))
            changes = monitor.store._db.total_changes - initial_changes
            final_wal = wal.stat().st_size
        finally:
            monitor.stop.set()
            for source in monitor.sources:
                close = getattr(source, "close", None)
                if close is not None:
                    close()
            monitor.store.close()
        elapsed = time.monotonic() - start
        written = disk_written() - initial_written
        rate = written / elapsed
        print(json.dumps({"duration_seconds": round(elapsed, 3), "scans": scans, "rows": args.rows,
                          "database_changes": changes, "wal_growth_bytes": final_wal - initial_wal,
                          "process_disk_bytes_written": written, "bytes_per_second": round(rate, 2),
                          "budget_bytes_per_second": 10 * 1024, "passed": rate < 10 * 1024,
                          "includes_final_checkpoint": True, "provider_reads": 0,
                          "live_data_writes": False}), flush=True)
        if rate >= 10 * 1024:
            raise SystemExit("Idle collector disk-write budget exceeded.")


if __name__ == "__main__":
    main()
