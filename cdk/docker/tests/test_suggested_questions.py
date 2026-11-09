# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Suggested-question variations are generated one window of the document at a
time (regression: prompting with all of a large FHIR bundle's sentences hit
Bedrock's "Input is too long", and the build retried it for 10 hours).
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent


@pytest.fixture(scope="module")
def modules(tmp_path_factory):
    # globals.py needs runs/ in the cwd and prompts.py reads its YAML from it.
    cwd = tmp_path_factory.mktemp("sq")
    (cwd / "runs").mkdir()
    for name in ("prompts.yaml", "system-prompts.yaml"):
        (cwd / name).symlink_to(REPO / name)
    old = os.getcwd()
    os.chdir(cwd)
    os.environ.setdefault("DISABLE_LANGFUSE", "1")
    sys.path[:0] = [str(HERE.parent), str(REPO)]
    try:
        import auto_formalizer
        import chatbot_backend
        import globals as g
        import store
        from sentences import TextSentence
        yield SimpleNamespace(af=auto_formalizer, cb=chatbot_backend, g=g,
                              store=store, TextSentence=TextSentence)
    finally:
        os.chdir(old)


def test_each_candidate_comes_from_one_window(modules, monkeypatch):
    m = modules
    sentences = [m.TextSentence(f"Fact {i}.", standalone=f"Fact {i}.") for i in range(24)]
    monkeypatch.setattr(m.cb, "SENTENCES", sentences)
    monkeypatch.setattr(m.g, "p", SimpleNamespace(qa_window_sentences=10), raising=False)
    prompted = []

    def fake_generate(window, exclude, temperature, stats):
        prompted.append(len(window))
        return {"question": f"Question {len(prompted)}?"}

    monkeypatch.setattr(m.af, "generate_q_and_a", fake_generate)
    monkeypatch.setattr(m.cb, "answer_q_from_chatbot", lambda q: "Yes.")
    result = {"suggested_questions": ["Original A?", "Original B?", "Original C?"]}
    out = m.store.generate_suggested_questions(result, ["subj"])
    # 24 sentences in windows of 10 -> [10, 14]; candidates cycle through them.
    assert prompted == [10, 14, 10]
    assert out == ["Original A?", "Original B?", "Original C?",
                   "Question 1?", "Question 2?", "Question 3?"]


def test_a_failing_generate_call_is_bounded(modules, monkeypatch):
    m = modules
    sentences = [m.TextSentence(f"Fact {i}.", standalone=f"Fact {i}.") for i in range(5)]
    monkeypatch.setattr(m.cb, "SENTENCES", sentences)
    calls = []

    def failing(window, exclude, temperature, stats):
        calls.append(1)
        raise RuntimeError("ValidationException: Input is too long")

    monkeypatch.setattr(m.af, "generate_q_and_a", failing)
    out = m.store.generate_suggested_questions({"suggested_questions": ["Q?"]}, ["subj"])
    assert out == ["Q?"]
    assert len(calls) == m.store._VARIATION_OVERSHOOT
