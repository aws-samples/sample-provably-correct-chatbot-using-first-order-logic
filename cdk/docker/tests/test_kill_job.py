# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Killing a job marks the job and its theory cancelled, never failed."""
from test_build_params import store_env  # noqa: F401  (pytest fixture)


def _building(store, jobs):
    out = store.create_theory("Tides", "One. Two.", doc_type="text")
    jid, tid = out["job"]["id"], out["theory"]["id"]
    store._update_job(jid, status="running", builder_task_arn="arn:aws:ecs:r:1:task/C/t1")
    return jid, tid


class FakeEcs:
    def __init__(self):
        self.stopped = []

    def stop_task(self, cluster, task, reason):
        self.stopped.append(task)


def test_kill_marks_the_job_and_theory_cancelled(store_env, monkeypatch):  # noqa: F811
    store, jobs, _ = store_env
    ecs = FakeEcs()
    monkeypatch.setattr(store, "_ecs", lambda: ecs)
    monkeypatch.setattr(store, "_jlog", lambda jid, msg: None)
    jid, tid = _building(store, jobs)
    out = store.kill_job(jid)
    assert out["ok"] and out["job"]["status"] == "cancelled"
    assert store._get_theory_item(tid)["status"] == "cancelled"
    assert ecs.stopped == ["arn:aws:ecs:r:1:task/C/t1"]


def test_a_builder_torn_down_by_a_kill_stays_cancelled(store_env, monkeypatch):  # noqa: F811
    store, jobs, _ = store_env
    monkeypatch.setattr(store, "_jlog", lambda jid, msg: None)
    jid, tid = _building(store, jobs)
    store._update_job(jid, status="cancelled")

    def torn_down(*a, **k):
        raise RuntimeError("worker pool terminated")

    monkeypatch.setattr(store, "run_job", torn_down)
    store._run_job_safe(jid, tid, "subj", "Tides", "One. Two.", "text")
    assert jobs.items[jid]["status"] == "cancelled"
    assert store._get_theory_item(tid)["status"] == "cancelled"


def test_a_real_failure_is_still_failed(store_env, monkeypatch):  # noqa: F811
    store, jobs, _ = store_env
    monkeypatch.setattr(store, "_jlog", lambda jid, msg: None)
    jid, tid = _building(store, jobs)

    def broken(*a, **k):
        raise RuntimeError("no verified theory")

    monkeypatch.setattr(store, "run_job", broken)
    store._run_job_safe(jid, tid, "subj", "Tides", "One. Two.", "text")
    assert jobs.items[jid]["status"] == "failed"
    assert store._get_theory_item(tid)["status"] == "failed"
