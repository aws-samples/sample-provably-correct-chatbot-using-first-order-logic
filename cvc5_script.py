# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pure, stdlib-only helpers for assembling and interpreting cvc5 scripts.

These used to live inside auto_formalizer.py. They were lifted out so that two
callers share ONE definition of "how a theory (+ optional question fragment) is
turned into a runnable cvc5 script, and how cvc5's output is classified":

  * auto_formalizer.run_cvc5 -- the pipeline's own cvc5 runner, and
  * kiro_tools/cvc5_checker.py -- the command-line checker the kiro-cli
    formalizer agent calls while it iterates on a theory.

Keeping them identical matters: the kiro-cli path accepts a theory only after
the harness re-checks it with run_cvc5, so if the agent's checker assembled the
script differently the agent could converge on a theory the harness rejects.

Nothing here imports boto3/globals/otel, so the agent-side checker starts fast.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, List, Optional, Tuple

NEWLINE = "\n"  # used by doctests below

# Validity labels, matching the names of utils.Sat members.
SAT = "SAT"
UNSAT = "UNSAT"
UNKNOWN = "UNKNOWN"
ERROR = "ERROR"


def cvc5_path() -> str:
    """
    The cvc5 binary to run: CVC5_PATH (the image sets /usr/local/bin/cvc5),
    else ~/bin/cvc5. It is used as argv[0] with no shell, so it must be an
    absolute path to an executable file named cvc5*; anything else raises
    rather than run some other program.
    """
    raw = os.environ.get("CVC5_PATH") or str(Path.home() / "bin" / "cvc5")
    path = Path(raw)
    if not (path.is_absolute() and path.name.startswith("cvc5")
            and path.is_file() and os.access(path, os.X_OK)):
        raise ValueError(f"CVC5_PATH must be an absolute path to the cvc5 "
                         f"executable, got {raw!r}")
    return str(path)


def find_spans(s: str, span_pattern: str) -> List[Tuple[str, Tuple[int, int]]]:
    """
    Finds all occurrences of (set-logic X) and returns X and their spans.

    Args:
        input_string (str): The input string to parse.

    Returns:
        list of tuples: A list where each tuple contains (X, (start, end))
    """
    assert isinstance(s, str), f"{type(s)}: {s}"
    assert isinstance(span_pattern, str), f"{type(span_pattern)}: {span_pattern}"
    results = [(match.group(1), match.span()) for match in re.finditer(span_pattern, s)]
    return results


def find_set_logic_spans(s: str) -> List[Tuple[str, Tuple[int, int]]]:
    """
    Finds all occurrences of (set-logic X) and returns X and their spans.

    >>> find_set_logic_spans(f"(set-logic ALL){NEWLINE}(set-logic QF)")
    [('ALL', (0, 15)), ('QF', (16, 30))]
    """
    assert isinstance(s, str), f"{type(s)}: {s}"
    return find_spans(s, r"\(set-logic\s+(\w+)\)")


def find_set_option_spans(s: str) -> List[Tuple[str, Tuple[int, int]]]:
    """
    Finds all occurrences of (set-option X) and returns X and their spans.

    >>> find_set_option_spans("(set-option :finite-model-find true)(set-option :produce-models true)(set-option :fmf-bound true)")
    [(':finite-model-find true', (0, 36)), (':produce-models true', (36, 69)), (':fmf-bound true', (69, 97))]
    """
    return find_spans(s, r"\(set-option\s+(.+?)\)")


def find_get_value_spans(s: str) -> List[Tuple[str, Tuple[int, int]]]:
    """
    Finds all occurrences of (get-value X) and returns X and their spans.

    >>> find_get_value_spans(f"(get-value (appropriate-mdm-level))")
    [('(appropriate-mdm-level)', (0, 35))]
    """
    return find_spans(s, r"\(get-value\s+(.+)\)")


def remove_spans(s: str, spans: List[Tuple]):
    """
    >>> print(remove_spans(f"(set-logic ALL){NEWLINE}(set-logic QF)", [(0, 15), (16, 30)]))
    <BLANKLINE>
    <BLANKLINE>
    """
    sorted_spans = sorted(spans, key=lambda x: x[0], reverse=True)
    result = s
    for start, end in sorted_spans:
        result = result[:start] + result[end:]
    return result


def find_and_remove_spans(
        theory: str,
        span_finder: Callable[[str], List[tuple]]) -> Tuple[str, List[str]]:
    """
    Return 1/ the new theory, 2/ a list of all the things (e.g. logics) that we removed.
    """
    spans = span_finder(theory)
    new_theory = remove_spans(theory, [span for _theory, span in spans])
    return new_theory, [theory for theory, _span in spans]


def find_and_remove_set_logic_spans(theory: str) -> Tuple[str, List[str]]:
    """
    Return 1/ the new theory, 2/ a list of all the logics (e.g. "ALL") that we removed.

    >>> find_and_remove_set_logic_spans("(set-logic ALL)foo(set-logic HO_ALL)bar")
    ('foobar', ['ALL', 'HO_ALL'])

    """
    assert isinstance(theory, str), f"{type(theory)}: {theory}"
    return find_and_remove_spans(theory, find_set_logic_spans)


def find_and_remove_set_option_spans(theory: str) -> Tuple[str, List[str]]:
    """
    Return 1/ the new theory, 2/ a list of all the logics (e.g. "ALL") that we removed.

    >>> find_and_remove_set_option_spans("foo(set-option :finite-model-find true)baz(set-option :produce-models true)bar(set-option :fmf-bound true)")
    ('foobazbar', [':finite-model-find true', ':produce-models true', ':fmf-bound true'])

    """
    return find_and_remove_spans(theory, find_set_option_spans)


def most_general_logic(logics: List[str]) -> str:
    """
    Given a list of logics, return the one logic that is the most general.

    >>> most_general_logic(["ALL", "QF"])
    'ALL'

    >>> most_general_logic(["UNKNOWN"])
    'ALL'

    """
    # This is a first start, we need to update this because there isn't
    # a linear ordering and the list is incomplete
    all_logics_ordered = ["ALL", "QF_ALL", "QF_BV", "QF_AUFBV"]
    try:
        most_general_idx = sorted([all_logics_ordered.index(logic) for logic in logics])[0]
    except (IndexError, ValueError):
        return "ALL"
    return all_logics_ordered[most_general_idx]


def build_cvc5_script(theory: str, question: Optional[str]) -> str:
    """
    Turn a theory (and optionally a question fragment appended to it) into the
    script cvc5 actually runs: one merged (set-logic ...) and the collected
    (set-option ...)s first, every (check-sat)/(get-model)/(get-value ...)
    stripped from the body, and a single (check-sat)(get-model) plus the
    (get-value ...)s on the end.

    >>> print(build_cvc5_script("(set-logic ALL)(declare-const x Int)(check-sat)", "(assert (> x 1))"))
    (set-logic ALL)
    (declare-const x Int) 
    (assert (> x 1))
    <BLANKLINE>
    (check-sat)
    (get-model)
    """
    augmented_theory, logics = find_and_remove_set_logic_spans(theory)
    augmented_theory, options = find_and_remove_set_option_spans(augmented_theory)
    if question is not None:
        question, q_logics = find_and_remove_set_logic_spans(question)
        logics += q_logics
        question, q_options = find_and_remove_set_option_spans(question)
        options += q_options
        augmented_theory += f"\n{question}\n"
    preamble = f"(set-logic {most_general_logic(logics)})\n"
    for option in options:
        preamble += f"(set-option {option})\n"
    augmented_theory = f"{preamble}{augmented_theory}"
    # remove any (check-sat) and (get-model)s and append them on the end
    augmented_theory = augmented_theory.replace("(check-sat)", " ")
    augmented_theory = augmented_theory.replace("(get-model)", " ")
    get_value_spans = find_get_value_spans(augmented_theory)
    augmented_theory = remove_spans(augmented_theory,
                                    [span for _arg, span in get_value_spans])
    postamble = "\n(check-sat)\n(get-model)"
    for arg, _span in get_value_spans:
        postamble += f"(get-value {arg})\n"
    augmented_theory += postamble
    return augmented_theory


def classify_cvc5_output(stdout: str, stderr: str) -> Tuple[str, Optional[str]]:
    """
    Classify cvc5's output as SAT / UNSAT / UNKNOWN / ERROR (the names of the
    utils.Sat members) and pull out the (get-model) body, if any.

    >>> classify_cvc5_output("sat\\n(\\n(define-fun x () Int 4)\\n)\\n", "")
    ('SAT', '(define-fun x () Int 4)')
    >>> classify_cvc5_output("unsat\\n", "")
    ('UNSAT', None)
    >>> classify_cvc5_output("", '(error "Parse Error")')
    ('ERROR', None)
    """
    if "(error" in stderr.lower():
        validity = ERROR
    elif re.match(r"^\s*sat\s*\((.*)\)\s*$", stdout.lower(), re.MULTILINE | re.DOTALL):
        validity = SAT
    elif stdout.lower().startswith("unsat"):
        validity = UNSAT
    elif stdout.lower().startswith("unknown"):
        validity = UNKNOWN
    else:
        validity = ERROR
    stdout = stdout.strip()
    m = re.search(r"sat\s*\n\((.*)\)\s*$", stdout, re.MULTILINE | re.DOTALL)
    get_model_output = m.group(1).strip() if m else None
    return validity, get_model_output
