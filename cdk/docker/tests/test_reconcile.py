# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for store._reconcile_builder_jobs (stale builder-job detection).

Uses in-memory fakes for the DynamoDB jobs table and the ECS client, so no AWS
access is needed.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import store  # noqa: E402

CLUSTER = "ProvablyCorrectChatbotStack-Cluster"
ARN_PREFIX = f"arn:aws:ecs:us-east-1:111111111111:task/{CLUSTER}/"


class ConditionalCheckFailed(Exception):
    pass


class FakeJobsTable:
    def __init__(self, items):
        self.items = {i["id"]: dict(i) for i in items}

    def get_item(self, Key):
        item = self.items.get(Key["id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues, ConditionExpression=None):
        item = self.items[Key["id"]]
        if ConditionExpression and item.get("status") in (
                "finished", "failed", "cancelled"):
            raise ConditionalCheckFailed()
        for assignment in UpdateExpression[len("SET "):].split(", "):
            name, value = (s.strip() for s in assignment.split("="))
            item[ExpressionAttributeNames[name]] = ExpressionAttributeValues[value]


class FakeEcs:
    def __init__(self, tasks=(), missing=()):
        self.tasks = list(tasks)
        self.missing = list(missing)
        self.calls = 0

    def describe_tasks(self, cluster, tasks):
        self.calls += 1
        return {
            "tasks": [t for t in self.tasks if t["taskArn"] in tasks],
            "failures": [{"arn": a, "reason": "MISSING"}
                         for a in self.missing if a in tasks],
        }


class FakeDdb:
    class meta:  # noqa: N801 -- mirrors boto3 resource.meta.client.exceptions
        class client:  # noqa: N801
            class exceptions:  # noqa: N801
                ConditionalCheckFailedException = ConditionalCheckFailed


@pytest.fixture
def env(monkeypatch):
    theories = {}
    monkeypatch.setattr(store, "ENABLED", True)
    monkeypatch.setattr(store, "BUILDER_CLUSTER", CLUSTER)
    monkeypatch.setattr(store, "_ddb", FakeDdb)
    monkeypatch.setattr(store, "_last_reconcile", {})
    monkeypatch.setattr(store, "_jlog", lambda *a, **k: None)
    monkeypatch.setattr(store, "_get_theory_item",
                        lambda tid: dict(theories[tid]) if tid in theories else None)
    monkeypatch.setattr(store, "_update_theory",
                        lambda tid, **f: theories[tid].update(f))

    def setup(jobs, ecs, theory_items=()):
        table = FakeJobsTable(jobs)
        monkeypatch.setattr(store, "_jobs", table)
        monkeypatch.setattr(store, "_ecs_client", ecs)
        theories.update({t["id"]: dict(t) for t in theory_items})
        return table, theories

    return setup


def job(jid, status="running", task="t1", theory="th1"):
    return {"id": jid, "status": status, "theory_id": theory,
            "theory_name": "Photosynthesis-1",
            "builder_task_arn": ARN_PREFIX + task if task else None}


def test_missing_task_fails_job_and_theory(env):
    table, theories = env([job("j1")], FakeEcs(missing=[ARN_PREFIX + "t1"]),
                          [{"id": "th1", "status": "building", "job_id": "j1"}])
    items = [table.get_item(Key={"id": "j1"})["Item"]]
    store._reconcile_builder_jobs(items)
    assert items[0]["status"] == "failed"
    assert table.items["j1"]["status"] == "failed"
    assert "no longer known to ECS" in table.items["j1"]["error"]
    assert theories["th1"]["status"] == "failed"


def test_stopped_task_records_reason(env):
    task = {"taskArn": ARN_PREFIX + "t1", "lastStatus": "STOPPED",
            "stoppedReason": "Essential container in task exited",
            "stopCode": "EssentialContainerExited",
            "containers": [{"exitCode": 137, "reason": "OutOfMemoryError"}]}
    table, _ = env([job("j1")], FakeEcs(tasks=[task]))
    store._reconcile_builder_jobs([table.get_item(Key={"id": "j1"})["Item"]])
    err = table.items["j1"]["error"]
    assert "exitCode=137" in err and "OutOfMemoryError" in err


def test_running_task_left_alone(env):
    task = {"taskArn": ARN_PREFIX + "t1", "lastStatus": "RUNNING"}
    table, _ = env([job("j1")], FakeEcs(tasks=[task]))
    store._reconcile_builder_jobs([table.get_item(Key={"id": "j1"})["Item"]])
    assert table.items["j1"]["status"] == "running"


def test_terminal_and_arnless_jobs_skip_ecs(env):
    ecs = FakeEcs()
    table, _ = env([job("j1", status="finished"), job("j2", task=None)], ecs)
    store._reconcile_builder_jobs(list(table.items.values()))
    assert ecs.calls == 0


def test_throttled_within_interval(env):
    ecs = FakeEcs(tasks=[{"taskArn": ARN_PREFIX + "t1", "lastStatus": "RUNNING"}])
    table, _ = env([job("j1")], ecs)
    for _ in range(3):
        store._reconcile_builder_jobs([table.get_item(Key={"id": "j1"})["Item"]])
    assert ecs.calls == 1


def test_builder_result_wins_race(env):
    """Builder writes 'finished' between our read and our conditional update."""
    table, theories = env([job("j1")], FakeEcs(missing=[ARN_PREFIX + "t1"]),
                          [{"id": "th1", "status": "ready", "job_id": "j1"}])
    stale = table.get_item(Key={"id": "j1"})["Item"]
    table.items["j1"]["status"] = "finished"
    store._reconcile_builder_jobs([stale])
    assert stale["status"] == "finished"
    assert table.items["j1"]["status"] == "finished"
    assert theories["th1"]["status"] == "ready"


def test_newer_rebuild_theory_untouched(env):
    table, theories = env([job("j1")], FakeEcs(missing=[ARN_PREFIX + "t1"]),
                          [{"id": "th1", "status": "building", "job_id": "j2"}])
    store._reconcile_builder_jobs([table.get_item(Key={"id": "j1"})["Item"]])
    assert table.items["j1"]["status"] == "failed"
    assert theories["th1"]["status"] == "building"
