# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Decontextualization ("make standalone") of TextDocument sentences: each
sentence gets only the previous N raw sentences as context, and the rewrites
run once each on a concurrency-capped thread pool.
"""
import json
import threading
import time

import pytest
import yaml

import globals as g
import sentences as sm
import utils
from params import Params


def _doc(tmp_path, lines):
    """A TextDocument over `lines` whose standalones are NOT precomputed."""
    # Bypass __init__: it resolves the subject registry and would compute.
    doc = sm.TextDocument.__new__(sm.TextDocument)
    doc._stats = utils.Statistics()
    doc._raw_sentences_path = tmp_path / "subj.txt"
    doc._standalones_path = tmp_path / "subj-standalone.txt"
    doc._originals = list(lines)
    return doc


@pytest.fixture(autouse=True)
def _params():
    g.p = Params(standalone_context_sentences=3, standalone_pool_size=4)
    yield
    g.p = None


def _blocks(prompt):
    """(previous sentences, sentence) from the two fenced blocks of the prompt."""
    parts = prompt.split("```")
    return parts[1].strip(), parts[3].strip()


class FakeLLM:
    """Stands in for do_inference; records calls and peak concurrency."""
    def __init__(self, fail_first_n=0, reply=None, delay=0.02):
        self.lock = threading.Lock()
        self.calls = []
        self.active = self.peak = 0
        self.fail_first_n = fail_first_n
        self.reply = reply
        self.delay = delay

    def __call__(self, prompt, stats, system_prompt=None, **_kw):
        with self.lock:
            self.calls.append(prompt)
            self.active += 1
            self.peak = max(self.peak, self.active)
            n = len(self.calls)
        time.sleep(self.delay)
        stats.n_inferences += 1
        with self.lock:
            self.active -= 1
        if n <= self.fail_first_n:
            return "no code block here"
        sentence = _blocks(prompt)[1]
        return self.reply or f"```\nS:{sentence}\n```"


def test_jobs_use_last_n_raw_sentences(tmp_path):
    lines = [f"s{i}" for i in range(6)]
    jobs = _doc(tmp_path, lines).standalone_jobs(context_sentences=2)
    assert jobs == [
        ("s0", []),
        ("s1", ["s0"]),
        ("s2", ["s0", "s1"]),
        ("s3", ["s1", "s2"]),
        ("s4", ["s2", "s3"]),
        ("s5", ["s3", "s4"]),
    ]


def test_zero_context_sends_no_prior_sentences(tmp_path):
    jobs = _doc(tmp_path, ["a", "b"]).standalone_jobs(context_sentences=0)
    assert [ctx for _s, ctx in jobs] == [[], []]


def test_default_window_is_ten():
    assert Params().standalone_context_sentences == 10


def test_runs_each_sentence_once_capped_and_in_order(tmp_path, monkeypatch):
    lines = [f"s{i}" for i in range(30)]
    doc = _doc(tmp_path, lines)
    llm = FakeLLM()
    monkeypatch.setattr(sm, "do_inference", llm)
    out = doc.compute_standalones()
    # sentence 0 has no context, so no LLM call; every other sentence exactly one
    assert len(llm.calls) == 29
    assert 1 < llm.peak <= 4
    assert out == ["s0"] + [f"S:{s}" for s in lines[1:]]
    assert (tmp_path / "subj-standalone.txt").read_text().split("\n") == out
    assert doc.stats.n_inferences == 29  # per-job stats merged in
    # the window: sentence 10's prompt holds s7..s9 and nothing earlier
    contexts = {_blocks(c)[1]: _blocks(c)[0] for c in llm.calls}
    assert contexts["s10"] == "s7\ns8\ns9"
    assert contexts["s2"] == "s0\ns1"


def test_unparseable_reply_is_retried(tmp_path, monkeypatch):
    doc = _doc(tmp_path, ["a", "b"])
    llm = FakeLLM(fail_first_n=2)
    monkeypatch.setattr(sm, "do_inference", llm)
    assert doc.compute_standalones() == ["a", "S:b"]
    assert len(llm.calls) == 3


def test_gives_up_after_max_attempts(tmp_path, monkeypatch):
    doc = _doc(tmp_path, ["a", "b"])
    llm = FakeLLM(reply="never a code block")
    monkeypatch.setattr(sm, "do_inference", llm)
    with pytest.raises(RuntimeError, match="failed 3 times"):
        doc.compute_standalones()
    assert len(llm.calls) == sm.STANDALONE_MAX_ATTEMPTS
    assert not (tmp_path / "subj-standalone.txt").exists()


def test_cancel_stops_the_pool(tmp_path, monkeypatch):
    doc = _doc(tmp_path, [f"s{i}" for i in range(50)])
    llm = FakeLLM(delay=0.2)
    monkeypatch.setattr(sm, "do_inference", llm)
    utils.set_cancel_check(lambda: len(llm.calls) > 0)
    try:
        with pytest.raises(RuntimeError, match="cancelled"):
            doc.compute_standalones()
    finally:
        utils.set_cancel_check(None)
    assert len(llm.calls) < 50


# --- pairing raw sentences with their rewrites --------------------------------
def test_a_repeated_sentence_keeps_each_rewrite_by_position(tmp_path):
    """A sentence that occurs twice is fine; each copy gets its own rewrite."""
    doc = _doc(tmp_path, ["It rained.", "The match went on.", "It rained."])
    doc._standalones = ["In Leeds it rained.", "The match went on.",
                        "In York it rained."]
    got = doc.sentences()
    assert [s.original for s in got] == ["It rained.", "The match went on.", "It rained."]
    assert [s.standalone for s in got] == ["In Leeds it rained.", "The match went on.",
                                           "In York it rained."]


def test_mismatched_standalone_file_is_reported(tmp_path):
    doc = _doc(tmp_path, ["One.", "Two."])
    doc._standalones = ["One."]
    with pytest.raises(ValueError, match="delete the standalone file"):
        doc.sentences()


def test_a_multi_line_rewrite_is_saved_on_one_line(tmp_path, monkeypatch):
    """The saved file stays one rewrite per line, aligned with the raw sentences."""
    doc = _doc(tmp_path, ["A.", "B.", "C."])
    monkeypatch.setattr(sm, "make_text_sentence_standalone",
                        lambda sentence, context, stats: f"{sentence}\n(rewritten)")
    out = doc.compute_standalones()
    assert out == ["A. (rewritten)", "B. (rewritten)", "C. (rewritten)"]
    assert utils.load_lines(doc._standalones_path) == out


# --- FHIR entries share their patient's entries ---------------------------

def _patient(pid):
    return [{"resource": {"resourceType": "Patient", "id": pid,
                          "name": [{"family": f"Fam-{pid}"}]}}]


def _entries(pid, n):
    """n FHIRsentences of patient `pid`, each built from a fresh copy of its
    entries (as a run file written before sharing would load them)."""
    return [sm.FHIRsentence(json.dumps({"resource": {"resourceType": "Observation",
                                                     "id": f"{pid}-{i}"}}),
                            patient_entries=json.dumps(_patient(pid)))
            for i in range(n)]


def test_one_patients_entries_share_one_list():
    a, b = _entries("p1", 2)
    assert a.patient_entries is b.patient_entries


def test_a_run_file_holds_each_patient_once_and_round_trips():
    sents = _entries("p1", 5) + _entries("p2", 3)
    text = utils.yaml_dump({"sentences": sents})
    assert text.count("Fam-p1") == 1
    assert text.count("Fam-p2") == 1
    loaded = yaml.safe_load(text)["sentences"]
    assert loaded == sents
    assert loaded[0].patient_entries == _patient("p1")
    assert loaded[0].patient_entries is loaded[4].patient_entries


def test_old_run_files_with_a_json_string_per_entry_still_load():
    old = ("sentences:\n- !FHIRsentence\n  original: '{\"id\": \"o1\"}'\n"
           f"  patient_entries: '{json.dumps(_patient('p9'))}'\n")
    [s] = yaml.safe_load(old)["sentences"]
    assert s.patient_entries == _patient("p9")


def test_each_patient_is_its_own_run_with_the_patient_once():
    sents = _entries("p1", 3) + _entries("p2", 2)
    runs = sm.Document.document_runs(sents)
    assert [len(r) for r in runs] == [3, 2]
    lines = sm.Document.make_standalone_lines(sents)
    assert lines == ([json.dumps(e) for e in _patient("p1")] + [s.original for s in sents[:3]] +
                     [json.dumps(e) for e in _patient("p2")] + [s.original for s in sents[3:]])
    assert all(json.loads(line) for line in lines)  # JSON, not Python reprs


def test_the_stored_theory_keeps_only_the_entry():
    [s] = _entries("p1", 1)
    assert s.to_dict() == {"_original": s.original}
