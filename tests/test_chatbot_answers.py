# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
An attempt that ends in "I don't know" must not win the race against an
attempt that proves an answer (regression: the US elections chat answered
"I don't know" to questions two of its ten attempts had proved).
"""
import os
import sys

import globals as g
from params import Params
import chatbot_backend as cb

sys.path.insert(0, os.path.dirname(__file__))
import pool_workers as pw  # noqa: E402  (importable by the spawned children)


def ask(monkeypatch, question):
    g.p = Params(subject_ids=["t"])
    monkeypatch.setattr(cb, "answer_q_", pw.answer_worker)
    monkeypatch.setattr(cb, "THEORY", "(theory)")
    return cb.answer_q_from_chatbot(question, num_jobs=4)


def test_a_proved_answer_beats_an_earlier_i_dont_know(monkeypatch):
    assert ask(monkeypatch, "Which party won?") == "The Democratic Party."


def test_i_dont_know_only_when_every_attempt_is(monkeypatch):
    assert ask(monkeypatch, "none") == "I don't know"


def test_the_answer_comes_with_its_logical_forms(monkeypatch):
    g.p = Params(subject_ids=["t"])
    monkeypatch.setattr(cb, "answer_q_", pw.answer_worker)
    monkeypatch.setattr(cb, "THEORY", "(theory)")
    assert cb.answer_q_with_logic("Which party won?", num_jobs=4) == dict(
        answer="The Democratic Party.", question_logic="(declare-const answer E)",
        answer_logic="(define-fun answer () E dem-party)")
    assert cb.answer_q_with_logic("none", num_jobs=4) == dict(
        answer="I don't know", question_logic=None, answer_logic=None)


def test_answer_q_reports_the_logical_forms(monkeypatch):
    import auto_formalizer as af
    from utils import Statistics
    monkeypatch.setattr(af, "convert_question_to_logical_form",
                        lambda q, theory, stats: {"logical_form": "(declare-const answer E)"})
    monkeypatch.setattr(af, "run_cvc5", lambda theory, lf, stats, print_out_theories=True: {
        "validity": af.Sat.SAT, "get_model_output": "(define-fun answer () E dem-party)"})
    monkeypatch.setattr(af, "get_model_output_2_nat_lang",
                        lambda **kw: {"answer": "The Democratic Party."})
    logic = {}
    answer = af.answer_q("(theory)", "Which party won?", stats=Statistics(), logic_out=logic)
    assert answer == "The Democratic Party."
    assert logic == {"question": "(declare-const answer E)",
                     "answer": "(define-fun answer () E dem-party)"}

    monkeypatch.setattr(af, "run_cvc5", lambda theory, lf, stats, print_out_theories=True: {
        "validity": af.Sat.UNSAT, "get_model_output": None})
    logic = {}
    assert af.answer_q("(theory)", "Which party won?", stats=Statistics(), logic_out=logic) is None
    assert logic == {"question": "(declare-const answer E)", "answer": None}


def test_a_given_theory_is_answered_instead_of_the_loaded_one(monkeypatch):
    g.p = Params(subject_ids=["t"])
    monkeypatch.setattr(cb, "answer_q_", pw.echo_theory_worker)
    monkeypatch.setattr(cb, "THEORY", "(loaded)")
    assert cb.answer_q_with_logic("?", num_jobs=2, theory="(other)")["answer"] == "from (other)"
    assert cb.answer_q_with_logic("?", num_jobs=2)["answer"] == "from (loaded)"
