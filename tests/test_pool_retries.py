# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for starmap_with_dupes' duplicate policy, with real spawned workers."""
import os
import sys

import globals as g
from params import Params
import utils

sys.path.insert(0, os.path.dirname(__file__))
import pool_workers as pw  # noqa: E402  (importable by the spawned children)


def run_pool(**policy):
    g.p = Params(subject_ids=["t"])
    events = []
    out = utils.starmap_with_dupes(
        pw.flaky_worker, [[i] for i in range(3)], job_prefix="l0", pool_size=8,
        job_successful_p=pw.has_theory, on_event=events.append, **policy)
    return out, events


def runs(events, kind="start"):
    counts = {}
    for e in events:
        if e["event"] == kind:
            counts[e["job"]] = counts.get(e["job"], 0) + 1
    return counts


def test_no_duplicates_but_failed_runs_are_retried():
    out, events = run_pool(do_spawn_dupes=False, retry_failed_runs=True)
    assert [r[0]["theory"] for r in out] == ["(t0)", "(t1)", "(t2)"]
    # Jobs 0 and 2 succeed first time and get exactly one run despite 8 free
    # slots; job 1 fails its first run and is retried once.
    assert runs(events) == {0: 1, 1: 2, 2: 1}
    assert [e["ok"] for e in events if e["event"] == "end" and e["job"] == 1] == [False, True]


def test_default_policy_still_spawns_speculative_duplicates():
    out, events = run_pool(do_spawn_dupes=True)
    assert [r[0]["theory"] for r in out] == ["(t0)", "(t1)", "(t2)"]
    # With 8 slots for 3 jobs the pool fills the spare slots with copies.
    assert sum(runs(events).values()) > 4

