"""Real process-lifetime checks using only disposable Python executables.

Run from the repository root. Optional first argument selects a frozen collector
executable for watchdog/gated-launch dispatch, never a provider account.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CHILD = """import json,os,signal,subprocess,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
grand = subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])
with open(sys.argv[1],'w') as output: json.dump([os.getpid(),grand.pid],output)
time.sleep(60)
"""


def running(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2)
    return bool(result.stdout.strip() and not result.stdout.strip().startswith("Z"))


def wait_for(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate(): return True
        time.sleep(0.02)
    return predicate()


def check(mode, frozen=None):
    with tempfile.TemporaryDirectory() as folder:
        marker = Path(folder) / "fixture-pids.json"
        # When testing a frozen watchdog, only its executable dispatch is used;
        # the monitored reader and its descendants are still harmless Python.
        bootstrap = """import os,sys,time,process_reader
if sys.argv[4]:
 original = process_reader.subprocess.Popen
 def popen(args,*a,**kw):
  mode = next((mode for mode in ('--reader-watchdog','--reader-launch') if mode in args),None)
  if mode: args=[sys.argv[4],*args[args.index(mode):]]
  return original(args,*a,**kw)
 process_reader.subprocess.Popen=popen
process = process_reader.start_process([sys.executable,'-c',sys.argv[2],sys.argv[1]])
print(process_reader._watchdog.pid,flush=True)
while not os.path.exists(sys.argv[1]): time.sleep(.01)
if sys.argv[3]=='normal': process_reader.stop_process(process)
elif sys.argv[3]=='shutdown': process_reader.stop_all_processes()
else: time.sleep(60)
"""
        process = subprocess.Popen([sys.executable, "-c", bootstrap, str(marker), CHILD, mode, frozen or ""],
                                   cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pids, watchdog = [], None
        try:
            watchdog = int(process.stdout.readline())
            if not wait_for(marker.exists): raise AssertionError("Fixture reader never started")
            pids = json.loads(marker.read_text())
            if mode == "forced": process.kill()
            process.wait(timeout=5)
            if not wait_for(lambda: not any(running(pid) for pid in [*pids, watchdog])):
                raise AssertionError("A fixture descendant or watchdog survived " + mode)
            return {"mode": mode, "passed": True, "reader_and_grandchild_reaped": True,
                    "watchdog_reaped": True, "frozen_watchdog": bool(frozen)}
        finally:
            for pid in [*pids, watchdog, process.pid]:
                if pid:
                    try: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError: pass


def check_pre_registration_death(frozen=None):
    with tempfile.TemporaryDirectory() as folder:
        marker, paused = Path(folder) / "target-must-not-run", Path(folder) / "paused.json"
        bootstrap = """import json,os,sys,time,process_reader
original=process_reader.subprocess.Popen
def popen(args,*a,**kw):
 mode=next((mode for mode in ('--reader-watchdog','--reader-launch') if mode in args),None)
 if mode and sys.argv[3]: args=[sys.argv[3],*args[args.index(mode):]]
 process=original(args,*a,**kw)
 if mode=='--reader-launch':
  with open(sys.argv[2],'w') as output: json.dump([process.pid,process_reader._watchdog.pid],output)
  time.sleep(60)
 return process
process_reader.subprocess.Popen=popen
process_reader.start_process([sys.executable,'-c','import sys; open(sys.argv[1],"w").write("BAD")',sys.argv[1]])
"""
        process = subprocess.Popen([sys.executable, "-c", bootstrap, str(marker), str(paused), frozen or ""], cwd=ROOT)
        pids = []
        try:
            if not wait_for(paused.exists): raise AssertionError("Fixture parent did not pause before registration")
            pids = json.loads(paused.read_text())
            # Give the launch wrapper time to reach its gate before parent death.
            time.sleep(0.1)
            assert not marker.exists()
            process.kill()
            process.wait(timeout=5)
            if not wait_for(lambda: not any(running(pid) for pid in pids)):
                raise AssertionError("An unregistered launch wrapper survived parent death")
            assert not marker.exists()
            return {"mode": "forced-before-registration", "passed": True,
                    "target_never_executed": True, "wrapper_and_watchdog_reaped": True,
                    "frozen_watchdog": bool(frozen)}
        finally:
            for pid in [*pids, process.pid]:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass


if __name__ == "__main__":
    frozen = sys.argv[1] if len(sys.argv) > 1 else None
    print(json.dumps({"provider_calls": 0, "checks": [*[check(mode, frozen) for mode in ("normal", "shutdown", "forced")],
                                                       check_pre_registration_death(frozen)]}, indent=2))
