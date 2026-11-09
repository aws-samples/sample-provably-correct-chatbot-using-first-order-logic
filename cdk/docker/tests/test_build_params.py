# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for build_params (the Build parameters dialog) and its store wiring."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", ".."))  # repo root: params.py
import build_params as bp  # noqa: E402


def test_defaults_are_the_params_defaults(monkeypatch):
    monkeypatch.delenv("CHATBOT_KIRO_MODEL", raising=False)
    d = bp.defaults()
    assert d == {"qas_per_sentence": 1.0, "ncar_ratio": 0.85,
                 "qa_window_sentences": 10, "kiro_model": "",
                 "kiro_max_attempts": 3, "kiro_timeout_seconds": 14400,
                 "kiro_idle_timeout_seconds": 480}


def test_model_default_follows_the_environment(monkeypatch):
    monkeypatch.setenv("CHATBOT_KIRO_MODEL", "claude-sonnet-4.5")
    assert bp.defaults()["kiro_model"] == "claude-sonnet-4.5"


def test_specs_are_complete_and_serializable():
    specs = bp.specs()
    json.dumps(specs)
    for s in specs:
        assert {"key", "label", "type", "default", "help", "group"} <= set(s)
        if s["type"] in ("int", "float"):
            assert s["min"] <= s["default"] <= s.get("max", float("inf"))
        if s["type"] == "choice":
            assert s["default"] in s["choices"]


def test_validate_fills_defaults_and_coerces():
    v = bp.validate({"qa_window_sentences": "6", "kiro_max_attempts": 4.0,
                     "ncar_ratio": "0.5"})
    assert v["qa_window_sentences"] == 6 and v["kiro_max_attempts"] == 4
    assert isinstance(v["kiro_max_attempts"], int) and v["ncar_ratio"] == 0.5
    assert v["kiro_idle_timeout_seconds"] == 480
    assert bp.validate(None) == bp.defaults()


@pytest.mark.parametrize("raw, message", [
    ({"qa_window_sentences": 1}, "between 2 and 200"),
    ({"ncar_ratio": 1.5}, "between 0.1 and 1.0"),
    ({"kiro_max_attempts": 2.5}, "whole number"),
    ({"ncar_ratio": "lots"}, "must be a number"),
    ({"ncar_ratio": float("nan")}, "must be a number"),
    ({"kiro_model": "x; rm -rf /"}, "may only contain"),
    # Parameters of the removed map-reduce pipeline are refused, not ignored.
    ({"sentences_per_chunk": 4}, "unknown build parameter"),
    ({"theory_engine": "agentic"}, "unknown build parameter"),
])
def test_validate_rejects_bad_values(raw, message):
    with pytest.raises(ValueError, match=message):
        bp.validate(raw)


def test_apply_builds_params():
    v = bp.validate({"qas_per_sentence": 1.5, "qa_window_sentences": 20,
                     "kiro_model": "claude-sonnet-4.5", "kiro_timeout_seconds": 3600})
    p = bp.apply(v, "subj", max_sentences=12)
    assert p.subject_ids == ["subj"] and p.max_sentences == 12
    assert p.qas_per_sentence == 1.5 and p.fhir_qas_per_sentence == 3.0
    assert p.qa_window_sentences == 20 and p.kiro_model == "claude-sonnet-4.5"
    assert p.kiro_timeout_seconds == 3600
    p = bp.apply(bp.validate({}), "s")
    assert p.kiro_model is None and p.max_sentences is None


# --- store wiring ------------------------------------------------------------
class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["id"]] = dict(Item)

    def scan(self):
        return {"Items": [dict(i) for i in self.items.values()]}

    def get_item(self, Key):
        item = self.items.get(Key["id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues, **_kw):
        item = self.items.setdefault(Key["id"], {"id": Key["id"]})
        for part in UpdateExpression[len("SET "):].split(", "):
            name, value = (s.strip() for s in part.split("="))
            item[ExpressionAttributeNames[name]] = ExpressionAttributeValues[value]


@pytest.fixture
def store_env(monkeypatch):
    import store
    jobs, theories = FakeTable(), FakeTable()
    dispatched = {}
    monkeypatch.setattr(store, "ENABLED", True)
    monkeypatch.setattr(store, "DISPATCH_ENABLED", True)
    monkeypatch.setattr(store, "_jobs", jobs)
    monkeypatch.setattr(store, "_theories", theories)
    monkeypatch.setattr(store, "_put_text", lambda key, text: None)
    monkeypatch.setattr(store, "_reconcile_builder_jobs", lambda items: None)

    def fake_dispatch(jid, tid, subject_id, name, doc_type, src_key, params_json=None):
        dispatched.update(jid=jid, params_json=params_json)
        return "arn:aws:ecs:r:1:task/C/t1"

    monkeypatch.setattr(store, "_dispatch_build", fake_dispatch)
    return store, jobs, dispatched


def test_create_theory_stores_and_dispatches_params(store_env):
    store, jobs, dispatched = store_env
    out = store.create_theory("Doc", "One. Two. Three.", doc_type="text",
                              build_params={"qa_window_sentences": 5, "kiro_max_attempts": 2})
    sent = json.loads(dispatched["params_json"])
    assert sent["qa_window_sentences"] == 5 and sent["kiro_max_attempts"] == 2
    assert out["job"]["build_params"]["kiro_max_attempts"] == 2
    assert json.loads(jobs.items[dispatched["jid"]]["build_params_json"]) == sent


def test_create_theory_rejects_bad_params_before_writing(store_env):
    store, jobs, dispatched = store_env
    with pytest.raises(ValueError, match="Sessions"):
        store.create_theory("Doc", "One. Two.", build_params={"kiro_max_attempts": 0})
    assert jobs.items == {} and dispatched == {}


def test_job_view_tolerates_jobs_without_params():
    import store
    view = store._job_view({"id": "j", "created_at": 1})
    assert view["build_params"] is None
    assert store._job_view({"id": "j", "build_params_json": "{bad"})["build_params"] is None
