# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The chatbot loads a store-backed theory from its stored YAML, not from the
SMT-LIB text store.get_theory exposes as `computed_theory` (regression: the
Cape Canaveral theory's "I'm feeling lazy" picker returned 500 because an SMT
comment like `; (days-after e1 e2 n): ...` is not valid YAML).
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent

THEORY = """\
(declare-sort Event 0)
; (days-after e1 e2 n): e1 happened n days after e2
(declare-fun days-after (Event Event Int) Bool)
(declare-const launch Event)
"""
QUESTIONS = ["When was the first launch?", "What is Cape Canaveral?"]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    # globals.py needs runs/ in the cwd and prompts.py reads its YAML from it.
    cwd = tmp_path_factory.mktemp("srv")
    (cwd / "runs").mkdir()
    for name in ("prompts.yaml", "system-prompts.yaml"):
        (cwd / name).symlink_to(REPO / name)
    old = os.getcwd()
    os.chdir(cwd)
    os.environ.setdefault("DISABLE_LANGFUSE", "1")
    sys.path[:0] = [str(HERE.parent), str(REPO)]
    try:
        import server as srv  # noqa: E402
        yield srv
    finally:
        os.chdir(old)


def stored_detail():
    """What store.get_theory returns for a ready theory."""
    raw = yaml.safe_dump({"theory": THEORY, "sentences": ["Launches happen here."],
                          "suggested_questions": QUESTIONS}, sort_keys=False)
    return {"id": "t-cape", "name": "Cape Canaveral", "status": "ready",
            "subject_id": "cape_canaveral_t", "computed_theory_yaml": raw,
            "computed_theory": THEORY, "suggested_questions": QUESTIONS}


def test_smt_text_is_not_parseable_as_the_result_yaml(server):
    # The old code parsed `computed_theory` (the SMT-LIB) -- this is why it failed.
    with pytest.raises(yaml.YAMLError):
        server._parse_theory_result(THEORY)


def test_lucky_questions_load_the_theory_from_its_yaml(server, monkeypatch):
    monkeypatch.setattr(server.store, "get_theory", lambda tid: stored_detail())
    monkeypatch.setattr(server, "_loaded_theory_id", None)
    out = asyncio.run(server.lucky_questions(theory_id="t-cape", user={}))
    assert out == {"questions": QUESTIONS}
    assert server.chatbot_backend.THEORY == THEORY
    assert server.chatbot_backend.SUBJECT_IDS == ["cape_canaveral_t"]


def test_theory_detail_keeps_the_real_yaml(server, monkeypatch):
    detail = stored_detail()
    monkeypatch.setattr(server.store, "get_theory", lambda tid: dict(detail))
    out = asyncio.run(server.api_get_theory("t-cape", user={}))
    assert out["computed_theory"] == THEORY
    assert out["computed_theory_yaml"] == detail["computed_theory_yaml"]


def test_stored_theory_yaml_falls_back_for_older_stores(server):
    assert server._stored_theory_yaml({"computed_theory": "theory: x"}) == "theory: x"
    assert server._stored_theory_yaml({}) == ""


def test_chatbot_endpoints_need_a_theory(server, monkeypatch):
    """No theory is baked in: with none selected or loaded, /lucky* is a 409."""
    from fastapi import HTTPException
    monkeypatch.setattr(server, "_loaded_theory_id", "")
    for call in (server.lucky_questions(theory_id="", user={}),
                 server.feeling_lucky(theory_id="", user={})):
        with pytest.raises(HTTPException) as err:
            asyncio.run(call)
        assert err.value.status_code == 409
