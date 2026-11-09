# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for the single-unit build driver (auto_formalizer.formalize_document)
and its test-set generation. No kiro-cli, cvc5 or Bedrock needed.
"""
import threading

import numpy as np
import pytest

import globals as g
from params import Params
from sentences import TextSentence
from utils import Statistics
import auto_formalizer as af
import kiro_formalizer as kf

SENTENCES = [TextSentence(f"Fact number {i}.", standalone=f"Fact number {i}.")
             for i in range(25)]


class FakeDocument:
    def __init__(self, sentences):
        self._sentences = sentences

    def sentences(self):
        return list(self._sentences)


class FakeSimilarity:
    """QuestionSimilarity without Bedrock: questions sharing a word are 'similar'."""

    def __init__(self, *_a, **_k):
        self.questions = []

    def question_is_sufficiently_diverse(self, q):
        clash = next((x for x in self.questions if x.split()[0] == q.split()[0]), None)
        return clash is None, np.zeros(1), clash

    def add_question(self, q, _embedding):
        self.questions.append(q)


@pytest.fixture(autouse=True)
def params(monkeypatch, tmp_path):
    g.p = Params(subject_ids=["facts"], qa_window_sentences=10)
    monkeypatch.setattr(g, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(af, "QuestionSimilarity", FakeSimilarity)
    monkeypatch.setattr(af, "subject_list_type", lambda ids: "default")
    yield
    g.p = None


def fake_window_generator(calls):
    """A gen_diverse_set_of_qs_and_as that names each question after its window."""
    lock = threading.Lock()

    def gen(sentences, num_new_questions_reqd, stats, max_attempts=None, **_kw):
        first = sentences[0].original.split()[-1].rstrip(".")
        with lock:
            calls.append((first, len(sentences), num_new_questions_reqd, max_attempts))
        return [(f"w{first}q{i} about the window?", f"a{i}")
                for i in range(num_new_questions_reqd)]
    return gen


# --- test set ----------------------------------------------------------------
def test_test_set_is_split_across_windows_by_size(monkeypatch):
    calls = []
    monkeypatch.setattr(af, "gen_diverse_set_of_qs_and_as", fake_window_generator(calls))
    qs = af.generate_test_set(SENTENCES[:24], 24, Statistics())
    # 24 sentences in windows of 10: [0..9] and [10..23] (short tail merged).
    assert sorted(c[:3] for c in calls) == [("0", 10, 10), ("10", 14, 14)]
    assert all(c[3] == 4 * c[2] + 4 for c in calls)  # every window is bounded
    assert len(qs) == 24
    assert qs[0][0].startswith("w0q") and qs[-1][0].startswith("w10q")  # document order


def test_test_set_drops_questions_similar_to_another_windows(monkeypatch):
    def gen(sentences, num_new_questions_reqd, stats, **_kw):
        first = sentences[0].original.split()[-1].rstrip(".")
        return [("Shared opening question?", "x"), (f"Own{first} question?", "y")]

    monkeypatch.setattr(af, "gen_diverse_set_of_qs_and_as", gen)
    qs = af.generate_test_set(SENTENCES[:20], 4, Statistics())
    assert [q for q, _ in qs] == ["Shared opening question?", "Own0 question?",
                                  "Own10 question?"]


def test_windows_run_in_parallel(monkeypatch):
    barrier = threading.Barrier(3, timeout=5)

    def gen(sentences, num_new_questions_reqd, stats, **_kw):
        barrier.wait()  # only passes if all three windows run at once
        first = sentences[0].original.split()[-1].rstrip(".")
        return [(f"Q{first}?", "a")]

    monkeypatch.setattr(af, "gen_diverse_set_of_qs_and_as", gen)
    g.p.qa_pool_size = 3
    thirty = [TextSentence(f"Fact number {i}.", standalone=f"Fact number {i}.")
              for i in range(30)]
    assert [q for q, _ in af.generate_test_set(thirty, 3, Statistics())] == \
        ["Q0?", "Q10?", "Q20?"]


def test_test_set_size_and_target():
    g.subject_type = "default"
    g.p = Params(qas_per_sentence=1.0, fhir_qas_per_sentence=2.0, ncar_ratio=0.8)
    assert af.num_qs_and_as(40) == 40 and af.num_qs_and_as(1) == 3
    g.subject_type = "fhir"
    assert af.num_qs_and_as(40) == 80
    assert af.num_correct_answers_reqd(40, 40) == 32
    assert af.num_correct_answers_reqd(40, 20) == 18  # allows two errors


def test_max_sentences_caps_the_document(monkeypatch):
    monkeypatch.setattr(af, "load_all_documents",
                        lambda stats: [FakeDocument(SENTENCES[:15]), FakeDocument(SENTENCES[15:])])
    assert len(af.load_all_sentences(Statistics())) == 25
    g.p.max_sentences = 12
    assert af.load_all_sentences(Statistics()) == SENTENCES[:12]


# --- the build -------------------------------------------------------------------
def test_formalize_document_builds_one_theory_and_saves_it(monkeypatch):
    monkeypatch.setattr(af, "load_all_documents",
                        lambda stats: [FakeDocument(SENTENCES[:10]), FakeDocument(SENTENCES[10:20])])
    monkeypatch.setattr(af, "gen_diverse_set_of_qs_and_as", fake_window_generator([]))
    seen = {}

    def fake_kiro(sentences, qs_and_as, n_correct_answers_reqd, stats, on_event):
        seen.update(sentences=sentences, qs=qs_and_as, n=n_correct_answers_reqd)
        on_event(dict(type="session_start", session=1, kind="build"))
        return dict(theory="(declare-const x Int)", n_correct_answers_reqd=n_correct_answers_reqd,
                    correctly_answered_questions=[q for q, _ in qs_and_as[:17]])

    monkeypatch.setattr(kf, "kiro_create_valid_useful_theory", fake_kiro)
    events = []
    af.set_progress_cb(events.append)
    try:
        summary = af.formalize_document()
    finally:
        af.set_progress_cb(None)

    # ONE unit over every sentence of both documents.
    assert seen["sentences"] == SENTENCES[:20]
    assert len(seen["qs"]) == 20 and seen["n"] == 17  # ceil(20 x the default ncar_ratio 0.85)
    assert summary["ok"] is True and summary["n_correct"] == 17
    assert summary["num_sentences"] == 20 and summary["num_qs"] == 20
    assert [e["type"] for e in events] == ["start", "phase", "test_set", "phase",
                                           "session_start", "end"]
    assert events[0]["sentences"] == [s.standalone for s in SENTENCES[:20]]
    assert events[0]["raw_sentences"] == [s.original for s in SENTENCES[:20]]
    assert events[0]["standalone_sentences"] == [s.standalone for s in SENTENCES[:20]]
    assert [e["phase"] for e in events if e["type"] == "phase"] == ["test_set", "theory"]
    # Phase and end events carry the build's running spend.
    for e in events:
        if e["type"] in ("phase", "end"):
            assert set(e["spent"]) == {"bedrock_dollars", "kiro_credits"}
    assert events[2]["n_required"] == 17 and len(events[2]["qs_and_as"]) == 20
    assert events[-1]["ok"] is True and events[-1]["n_correct"] == 17

    # The run file round-trips into what the chatbot loads.
    loaded = af.load_run_result(af.Path(summary["runs_path"]))
    assert loaded["theory"] == "(declare-const x Int)"
    assert len(loaded["suggested_questions"]) == 17
    # Each verified question comes with the expected answer it was checked against.
    assert len(loaded["verified_qs_and_as"]) == 17
    assert all(q == pair[0] for q, pair in zip(loaded["suggested_questions"],
                                              loaded["verified_qs_and_as"]))
    assert loaded["verified_qs_and_as"][0][1].startswith("a")
    assert len(loaded["sentences"]) == 20
    assert loaded["p"].subject_ids == ["facts"]
    p, result = af.load_run_file(af.Path(summary["runs_path"]))
    assert p.qa_window_sentences == 10 and len(result["all_qs_and_as"]) == 20


def test_formalize_document_without_a_verified_theory(monkeypatch):
    monkeypatch.setattr(af, "load_all_documents", lambda stats: [FakeDocument(SENTENCES[:5])])
    monkeypatch.setattr(af, "gen_diverse_set_of_qs_and_as", fake_window_generator([]))
    monkeypatch.setattr(kf, "kiro_create_valid_useful_theory",
                        lambda **kw: dict(theory=None, n_correct_answers_reqd=3,
                                          correctly_answered_questions=[]))
    summary = af.formalize_document()
    assert summary["ok"] is False
    assert af.load_run_result(af.Path(summary["runs_path"]))["theory"] is None


def test_formalize_document_refuses_an_empty_document(monkeypatch):
    monkeypatch.setattr(af, "load_all_documents", lambda stats: [FakeDocument([])])
    with pytest.raises(ValueError, match="no sentences"):
        af.formalize_document()


def test_run_files_are_numbered(monkeypatch):
    first = af.generate_uniq_run_file(["a", "b"])
    assert first.name == "run-a-b-0000.yaml"
    first.write_text("")
    assert af.generate_uniq_run_file(["a"]).name == "run-a-0001.yaml"


def test_progress_callback_errors_never_escape():
    def boom(_event):
        raise RuntimeError("recorder down")

    af.set_progress_cb(boom)
    try:
        af.report_progress(dict(type="phase", phase="theory"))
    finally:
        af.set_progress_cb(None)


def test_module_doctests():
    import doctest
    result = doctest.testmod(af)
    assert result.attempted > 0 and result.failed == 0


# --- the test set reaches its target -------------------------------------------
def test_test_set_keeps_generating_until_it_reaches_the_target(monkeypatch):
    """Each call yields only 2 new questions per window, so it takes rounds."""
    calls = []
    lock = threading.Lock()

    def gen(sentences, num_new_questions_reqd, stats, existing_qs_and_as=(), **_kw):
        first = sentences[0].original.split()[-1].rstrip(".")
        before = [q for q, _ in existing_qs_and_as]
        with lock:
            calls.append((first, len(before)))
        n = len(before)
        new = [(f"W{first}n{n + k} question?", "a") for k in range(2)]
        return list(existing_qs_and_as) + new  # like the real one: old + new

    monkeypatch.setattr(af, "gen_diverse_set_of_qs_and_as", gen)
    qs = af.generate_test_set(SENTENCES[:20], 13, Statistics())
    assert len(qs) == 13 and len({q for q, _ in qs}) == 13
    # Two windows x 2 per round: four rounds, each told what it produced before.
    assert max(n for _, n in calls) >= 6
    assert ("0", 0) in calls and ("0", 2) in calls and ("0", 4) in calls
    # Document order: window 0's questions before window 10's.
    assert qs[0][0].startswith("W0n") and qs[-1][0].startswith("W10n")


def test_similar_questions_do_not_count_toward_the_target(monkeypatch):
    """Questions sharing a first word are 'similar' (FakeSimilarity)."""
    seq = iter(range(1000))

    def gen(sentences, num_new_questions_reqd, stats, existing_qs_and_as=(), **_kw):
        # Every other new question repeats the first word "Same".
        new = []
        for _ in range(num_new_questions_reqd):
            k = next(seq)
            new.append((f"Same{'' if k % 2 else k} filler {k}?", "a") if k % 2 == 0
                       else (f"Unique{k} question?", "a"))
        return list(existing_qs_and_as) + new

    monkeypatch.setattr(af, "gen_diverse_set_of_qs_and_as", gen)
    qs = af.generate_test_set(SENTENCES[:10], 6, Statistics())
    assert len(qs) == 6
    first_words = [q.split()[0] for q, _ in qs]
    assert len(set(first_words)) == 6  # all semantically distinct


def test_stops_short_when_the_document_runs_out_of_questions(monkeypatch):
    """A window that keeps adding nothing drops out; the loop ends."""
    def gen(sentences, num_new_questions_reqd, stats, existing_qs_and_as=(), **_kw):
        first = sentences[0].original.split()[-1].rstrip(".")
        pool = [(f"Only{first}x{k} question?", "a") for k in range(3)]
        before = {q for q, _ in existing_qs_and_as}
        return list(existing_qs_and_as) + [qa for qa in pool if qa[0] not in before]

    monkeypatch.setattr(af, "gen_diverse_set_of_qs_and_as", gen)
    qs = af.generate_test_set(SENTENCES[:20], 50, Statistics())
    assert len(qs) == 6  # 3 distinct per window is all there is
