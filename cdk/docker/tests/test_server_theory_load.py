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


# --- Super Chat (/ask-multi) ----------------------------------------------------
def _detail(tid, name, theory, status="ready"):
    raw = yaml.safe_dump({"theory": theory, "sentences": ["s."]}, sort_keys=False)
    return {"id": tid, "name": name, "status": status, "subject_id": tid,
            "computed_theory_yaml": raw, "computed_theory": theory}


def test_ask_multi_answers_each_theory_against_its_own_theory(server, monkeypatch):
    details = {"a": _detail("a", "Elections", "(thy-a)"),
               "b": _detail("b", "Hypertension", "(thy-b)")}
    monkeypatch.setattr(server.store, "get_theory", details.get)
    seen = []

    def fake_answer(text, num_jobs=10, theory=None):
        seen.append((theory, num_jobs))
        if theory == "(thy-a)":
            return dict(answer="The Democrats.", question_logic="(q-a)", answer_logic="(m-a)")
        return dict(answer="I don't know", question_logic=None, answer_logic=None)

    monkeypatch.setattr(server.chatbot_backend, "answer_q_with_logic", fake_answer)
    loaded_before = server.chatbot_backend.THEORY
    out = server.ask_multiple_theories(
        server.MultiQuestion(text="Who won?", theory_ids=["a", "b", "a"]), user={})
    assert out["answers"] == [
        dict(theory_id="a", theory_name="Elections", answer="The Democrats.",
             verified=True, question_logic="(q-a)", answer_logic="(m-a)"),
        dict(theory_id="b", theory_name="Hypertension", answer="I don't know",
             verified=False, question_logic=None, answer_logic=None)]
    # The attempts are shared between the theories; /ask's theory is untouched.
    assert sorted(seen) == [("(thy-a)", 5), ("(thy-b)", 5)]
    assert server.chatbot_backend.THEORY == loaded_before


def test_ask_multi_refuses_no_theories_and_unready_ones(server, monkeypatch):
    monkeypatch.setattr(server.store, "get_theory",
                        {"c": _detail("c", "Tides", "(t)", status="building")}.get)
    with pytest.raises(server.HTTPException) as ex:
        server.ask_multiple_theories(server.MultiQuestion(text="?", theory_ids=[]), user={})
    assert ex.value.status_code == 400
    with pytest.raises(server.HTTPException) as ex:
        server.ask_multiple_theories(server.MultiQuestion(text="?", theory_ids=["c"]), user={})
    assert ex.value.status_code == 409 and "Tides" in ex.value.detail


def test_ask_multi_takes_up_to_eight_theories(server, monkeypatch):
    details = {str(i): _detail(str(i), f"T{i}", f"(t{i})") for i in range(9)}
    monkeypatch.setattr(server.store, "get_theory", details.get)
    seen = []
    monkeypatch.setattr(server.chatbot_backend, "answer_q_with_logic",
                        lambda text, num_jobs=10, theory=None: seen.append(num_jobs) or dict(
                            answer="Yes.", question_logic=None, answer_logic=None))
    out = server.ask_multiple_theories(
        server.MultiQuestion(text="?", theory_ids=[str(i) for i in range(8)]), user={})
    assert len(out["answers"]) == 8 and seen == [2] * 8
    with pytest.raises(server.HTTPException) as ex:
        server.ask_multiple_theories(
            server.MultiQuestion(text="?", theory_ids=[str(i) for i in range(9)]), user={})
    assert ex.value.status_code == 400 and "at most 8" in ex.value.detail


def test_no_cors_unless_an_origin_is_configured(server):
    # The deployed app is same-origin; CORS is only for a local dev server.
    from fastapi.middleware.cors import CORSMiddleware
    if os.environ.get("CORS_ALLOW_ORIGINS"):
        pytest.skip("CORS_ALLOW_ORIGINS is set in this environment")
    assert not any(m.cls is CORSMiddleware for m in server.app.user_middleware)
