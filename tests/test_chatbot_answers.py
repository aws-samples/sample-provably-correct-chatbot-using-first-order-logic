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
