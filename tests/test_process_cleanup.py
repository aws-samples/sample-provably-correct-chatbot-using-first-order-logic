# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests that kiro-cli process trees and leftover pool workers really die
(the Hypertension final-reducer hang). Linux only: uses real processes.
"""
import os
import subprocess
import sys
import time

import pytest

import globals as g
from params import Params
import kiro_formalizer as kf
import utils

sys.path.insert(0, os.path.dirname(__file__))
import pool_workers as pw  # noqa: E402  (importable by the spawned children)

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")


def alive(pid: int) -> bool:
    """True if pid exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf8") as f:
            stat = f.read()
    except OSError:
        return False
    return stat[stat.rfind(")") + 2] != "Z"


def wait_dead(pids, timeout=5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


def read_pids(path, n, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            pids = [int(x) for x in path.read_text().split()]
            if len(pids) >= n:
                return pids
        time.sleep(0.05)
    raise AssertionError(f"only got {path.read_text() if path.exists() else ''!r}")


# A fake kiro-cli: an orphaned grandchild in its own session (like the node
# server kiro-cli-chat starts) plus one in its own process group, both
# writing their pids to argv[1].
TREE = r"""
import os, subprocess, sys
out = sys.argv[1]
a = subprocess.Popen(["sleep", "300"])                         # same session
b = subprocess.Popen(["sleep", "300"], process_group=0)        # own pgroup
c = subprocess.Popen(["sh", "-c", "sleep 300 & echo $! >> " + out])  # orphan
c.wait()
with open(out, "a") as f:
    f.write(f"{a.pid}\n{b.pid}\n")
if sys.argv[2] == "stay":
    os.execvp("sleep", ["sleep", "300"])
"""


@pytest.mark.parametrize("root_stays", [True, False])
def test_kill_process_tree_gets_orphans_and_other_groups(tmp_path, root_stays):
    out = tmp_path / "pids"
    # Test fixture: a fixed Python script under this interpreter.
    root = subprocess.Popen([sys.executable, "-c", TREE, str(out),  # nosec B603 # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
                             "stay" if root_stays else "exit"],
                            start_new_session=True)
    pids = read_pids(out, 3)
    if not root_stays:
        root.wait(timeout=10)   # root gone; its children are re-parented
    try:
        utils.kill_process_tree(root.pid)
        assert wait_dead(pids + ([root.pid] if root_stays else []))
    finally:
        for p in pids:
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        root.kill()
        root.wait()


def test_kill_process_tree_spares_unrelated_processes_in_our_session():
    bystander = subprocess.Popen(["sleep", "300"])   # our session, not the tree
    root = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        assert utils.kill_process_tree(root.pid) == [root.pid]
        assert wait_dead([root.pid])
        assert alive(bystander.pid) and alive(os.getpid())
    finally:
        for p in (bystander, root):
            p.kill()
            p.wait()


def test_run_kiro_timeout_kills_the_tree_and_returns(monkeypatch, tmp_path):
    """A kiro-cli whose child holds stdout open must not hang _run_kiro."""
    g.p = Params()
    out = tmp_path / "pids"
    fake = tmp_path / "kiro-cli"
    fake.write_text("#!/bin/sh\n"
                    f"sleep 300 &\necho $! > {out}\n"
                    "echo started\nsleep 300\n")
    fake.chmod(0o755)
    monkeypatch.setenv("KIRO_CLI_PATH", str(fake))
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY_SECRET_ID", raising=False)
    start = time.time()
    rc, credits = kf._run_kiro(tmp_path, "hi", resume=False, timeout=1.5)
    assert rc == -9 and credits is None
    assert time.time() - start < 15
    assert wait_dead(read_pids(out, 1))


def test_run_kiro_normal_exit_reaps_orphans(monkeypatch, tmp_path):
    g.p = Params()
    out = tmp_path / "pids"
    fake = tmp_path / "kiro-cli"
    # Exits at once but leaves a background child holding stdout.
    fake.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {out}\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("KIRO_CLI_PATH", str(fake))
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY_SECRET_ID", raising=False)
    start = time.time()
    assert kf._run_kiro(tmp_path, "hi", resume=False, timeout=60) == (0, None)
    assert time.time() - start < 15
    assert wait_dead(read_pids(out, 1))


def test_pool_kills_leftover_runs_on_return(tmp_path):
    """A duplicate still running when the pool returns must be killed."""
    g.p = Params(subject_ids=["t"])
    out = tmp_path / "pids"
    start = time.time()
    res = utils.starmap_with_dupes(
        pw.straggler_worker, [[str(out)]], job_prefix="l0", pool_size=2,
        do_spawn_dupes=True, job_successful_p=pw.has_theory)
    assert [r[0]["theory"] for r in res] == ["(t)"]
    assert time.time() - start < 60
    # The duplicate's worker and the kiro-like session it started.
    assert wait_dead(read_pids(out, 2), timeout=10)


def test_run_kiro_returns_the_sessions_metered_credits(monkeypatch, tmp_path):
    g.p = Params()
    fake = tmp_path / "kiro-cli"
    meta = ('{"type":"metadata","data":{"turnDurationMs":1000,"meteringUsage":'
            '[{"value":0.5,"unit":"credit"},{"value":0.25,"unit":"credit"}]}}')
    fake.write_text(f"#!/bin/sh\necho '{meta}'\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("KIRO_CLI_PATH", str(fake))
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY_SECRET_ID", raising=False)
    assert kf._run_kiro(tmp_path, "hi", resume=False, timeout=60) == (0, 0.75)


def test_run_kiro_watchdog_kills_a_silent_session(monkeypatch, tmp_path):
    """No output for idle_timeout: killed (with its children) as STALLED_RC."""
    g.p = Params()
    out = tmp_path / "pids"
    fake = tmp_path / "kiro-cli"
    fake.write_text("#!/bin/sh\n"
                    f"sleep 300 &\necho $! > {out}\n"
                    "echo '{\"type\":\"runStarted\",\"data\":{}}'\nsleep 300\n")
    fake.chmod(0o755)
    monkeypatch.setenv("KIRO_CLI_PATH", str(fake))
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY_SECRET_ID", raising=False)
    start = time.time()
    rc, credits = kf._run_kiro(tmp_path, "hi", resume=False, timeout=120,
                               idle_timeout=2)
    assert rc == kf.STALLED_RC and credits is None
    assert time.time() - start < 20
    assert wait_dead(read_pids(out, 1))


def test_run_kiro_stops_when_the_build_is_cancelled(monkeypatch, tmp_path):
    """A kill of the build ends the running session (and its children)."""
    g.p = Params()
    out = tmp_path / "pids"
    fake = tmp_path / "kiro-cli"
    fake.write_text("#!/bin/sh\n"
                    f"sleep 300 &\necho $! > {out}\n"
                    "while true; do echo '{\"type\":\"metadata\",\"data\":{}}'; sleep 0.2; done\n")
    fake.chmod(0o755)
    monkeypatch.setenv("KIRO_CLI_PATH", str(fake))
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY_SECRET_ID", raising=False)
    start = time.time()
    utils.set_cancel_check(lambda: time.time() - start > 1.5)
    try:
        rc, _credits = kf._run_kiro(tmp_path, "hi", resume=False, timeout=120,
                                    idle_timeout=60)
    finally:
        utils.set_cancel_check(None)
    assert rc == kf.CANCELLED_RC
    assert time.time() - start < 20
    assert wait_dead(read_pids(out, 1))


def test_run_kiro_watchdog_spares_a_chatty_session(monkeypatch, tmp_path):
    g.p = Params()
    fake = tmp_path / "kiro-cli"
    meta = ('{"type":"metadata","data":{"meteringUsage":[{"value":0.5,"unit":"credit"}]}}')
    # Prints something every second for 5s, longer than the 3s idle limit.
    fake.write_text("#!/bin/sh\nfor i in 1 2 3 4 5; do echo tick; sleep 1; done\n"
                    f"echo '{meta}'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("KIRO_CLI_PATH", str(fake))
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY_SECRET_ID", raising=False)
    assert kf._run_kiro(tmp_path, "hi", resume=False, timeout=60,
                        idle_timeout=3) == (0, 0.5)
