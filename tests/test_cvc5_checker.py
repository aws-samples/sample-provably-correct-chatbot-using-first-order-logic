# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for kiro_tools/cvc5_checker.py, the kiro agent's cvc5 checker."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from utils import Statistics
import auto_formalizer as af

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "kiro_tools" / "cvc5_checker.py"
CVC5 = os.environ.get("CVC5_PATH", f"{Path.home()}/bin/cvc5")
needs_cvc5 = pytest.mark.skipif(not Path(CVC5).is_file(), reason="cvc5 not installed")

THEORY = """\
(set-logic ALL)
(declare-sort Plant 0)
(declare-const oak Plant)
(define-fun height-m ((p Plant) (h Int)) Bool (and (= p oak) (= h 30)))
(check-sat)
"""


def check(cwd, *args):
    proc = subprocess.run([sys.executable, str(CHECKER), *args], cwd=cwd,
                          capture_output=True, text=True, check=False,
                          env={**os.environ, "CVC5_PATH": CVC5})
    return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else None)


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "theory.smt2").write_text(THEORY)
    (tmp_path / "fragments").mkdir()
    (tmp_path / "fragments/q1.smt2").write_text(
        "(declare-const answer Int)\n(assert (height-m oak answer))\n")
    (tmp_path / "fragments/bad.smt2").write_text("(assert (height-m oak 31))\n")
    (tmp_path / "fragments/err.smt2").write_text("(assert (oops\n")
    return tmp_path


@needs_cvc5
def test_check_and_ask_verdicts(ws):
    # A check is just the verdict: no model to fill the agent's context.
    assert check(ws, "check", "theory.smt2")[1] == {"validity": "SAT"}
    rc, out = check(ws, "ask", "theory.smt2", "fragments/q1.smt2")
    assert rc == 0 and out == {"validity": "SAT", "values": "(define-fun answer () Int 30)"}
    assert check(ws, "ask", "theory.smt2", "fragments/bad.smt2")[1] == {"validity": "UNSAT"}
    err = check(ws, "ask", "theory.smt2", "fragments/err.smt2")[1]
    assert err["validity"] == "ERROR" and "Parse Error" in err["detail"]


@needs_cvc5
def test_ask_names_the_constant_an_abstract_answer_equals(ws):
    (ws / "plants.smt2").write_text(
        "(declare-sort Plant 0)\n(declare-const rose Plant)\n(declare-const oak Plant)\n"
        "(assert (distinct rose oak))\n(declare-fun is-red (Plant) Bool)\n"
        "(assert (is-red rose))\n(assert (not (is-red oak)))\n"
        "(declare-fun filler (Plant) Int)\n")
    (ws / "fragments/which.smt2").write_text("(declare-const answer Plant)\n(assert (= answer oak))\n")
    (ws / "fragments/red.smt2").write_text("(declare-const answer Plant)\n(assert (is-red answer))\n")
    values = check(ws, "ask", "plants.smt2", "fragments/which.smt2")[1]["values"]
    assert "named constants with the same value" in values and "(define-fun oak ()" in values
    assert "rose" not in values and "filler" not in values  # nothing else from the model
    # is-red is only pinned for rose and oak, so cvc5 may pick a fresh plant.
    red = check(ws, "ask", "plants.smt2", "fragments/red.smt2")[1]["values"]
    assert "(define-fun rose ()" in red or "does not pin the answer down" in red


@needs_cvc5
def test_checker_agrees_with_run_cvc5(ws):
    """A theory/fragment the agent sees as SAT is SAT for the harness too."""
    theory = (ws / "theory.smt2").read_text()
    for frag in ("q1", "bad"):
        fragment = (ws / f"fragments/{frag}.smt2").read_text()
        harness = af.run_cvc5(theory, fragment, Statistics(), print_out_theories=False)
        agent = check(ws, "ask", "theory.smt2", f"fragments/{frag}.smt2")[1]
        assert agent["validity"] == harness["validity"].name


def test_refuses_files_outside_working_directory(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.smt2"
    outside.write_text("(assert true)")
    rc, out = check(ws, "check", str(outside))
    assert rc == 0 and out["validity"] == "ERROR"
    assert "inside the working directory" in out["detail"]
    assert check(ws, "check", "../" + outside.parent.name + "/secret.smt2")[1]["validity"] == "ERROR"


def test_bad_usage_exits_2(ws):
    assert check(ws, "frobnicate")[0] == 2
