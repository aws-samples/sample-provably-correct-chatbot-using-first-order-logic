#!/usr/bin/env python3
"""
cvc5 checker for the kiro-cli formalizer agent.

The agent never calls cvc5 directly. It calls this script, which assembles the
script with the SAME rules the pipeline's run_cvc5 uses (cvc5_script.py), so a
theory the agent sees as SAT is a theory the harness will also see as SAT.

Usage:
    cvc5_checker.py check THEORY.smt2
        Is the theory on its own satisfiable (i.e. valid)?
    cvc5_checker.py ask THEORY.smt2 FRAGMENT.smt2 [FRAGMENT.smt2 ...]
        Run the theory with each question fragment appended (up to
        MAX_FRAGMENTS per call, checked in parallel); if SAT, print the model
        values the natural-language answer must be read from.

Prints one JSON object on stdout per theory/fragment, kept small because every
result lands in the agent's context (a full get-model of a large theory is
huge, and ~100 of them were seen to stall sessions). With several fragments,
each line also carries "fragment" (its path), in the order given:
    check: {"validity": ...} plus "detail" (cvc5's error text) when not SAT.
    ask:   {"validity": ..., "values": "<define-fun ...> ..."} when SAT, where
           values are the model entries of the symbols the FRAGMENT declares
           (the question's unknowns) plus the theory's named constants equal
           to any abstract value among them (so `@Plant_0` can be read as
           `rose`); "detail" when not SAT.
validity is "SAT"|"UNSAT"|"UNKNOWN"|"ERROR"|"TIMEOUT". The script cvc5 runs is
unchanged (cvc5_script.build_cvc5_script), so the SAT verdict is the one the
harness will see. Exit status is 0 whenever a verdict was produced, 2 on bad
usage.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# cvc5_script.py lives in the repo root, one level up from kiro_tools/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cvc5_script import build_cvc5_script, classify_cvc5_output  # noqa: E402

CVC5_PATH = os.environ.get("CVC5_PATH", f"{Path.home()}/bin/cvc5")
TIMEOUT_SECS = float(os.environ.get("CHATBOT_CVC5_TIMEOUT", "60"))
MAX_STREAM_CHARS = 1500  # stderr shown to the agent
MAX_VALUES_CHARS = 2000  # the "values" an ask returns
MAX_RELATED = 8          # named constants shown per abstract value
MAX_FRAGMENTS = 50       # fragments one `ask` may check


def _trim(s: str, limit: int = MAX_STREAM_CHARS) -> str:
    s = (s or "").strip()
    if len(s) > limit:
        return s[:limit] + f"\n...[truncated {len(s) - limit} chars]"
    return s


def split_sexprs(text: str) -> list[str]:
    """
    The top-level parenthesised expressions in `text` (string literals and
    |quoted symbols| respected).

    >>> split_sexprs('(define-fun x () Int 4) (define-fun s () String "a)b")')
    ['(define-fun x () Int 4)', '(define-fun s () String "a)b")']
    """
    out, depth, start, i, n = [], 0, None, 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"|":
            end = text.find(c, i + 1)
            i = n if end < 0 else end + 1
            continue
        if c == ";":
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
            continue
        if c == "(":
            if depth == 0:
                start = i
            depth += 1
        elif c == ")" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                out.append(text[start:i + 1])
        i += 1
    return out


_DECL_RE = re.compile(r"^\(\s*(?:declare-const|declare-fun|define-fun|define-const)"
                      r"\s+(\|[^|]*\||[^\s()]+)")


def _declared_name(expr: str) -> str | None:
    m = _DECL_RE.match(expr)
    return m.group(1) if m else None


def _abstract_values(expr: str) -> set[str]:
    """cvc5's abstract elements of uninterpreted sorts, e.g. @Plant_0."""
    return set(re.findall(r"@[A-Za-z0-9_.$-]+", expr))


def answer_values(model: str | None, fragment: str) -> str:
    """
    The model entries an answer is read from: the symbols the fragment
    declares, then the 0-ary entries (named constants) whose value is one of
    the abstract elements those entries mention.

    >>> m = ('(define-fun rose () Plant (as @Plant_0 Plant)) '
    ...      '(define-fun oak () Plant (as @Plant_1 Plant)) '
    ...      '(define-fun answer () Plant (as @Plant_0 Plant)) '
    ...      '(define-fun big ((p Plant)) Bool true)')
    >>> print(answer_values(m, '(declare-const answer Plant) (assert (is-red answer))'))
    (define-fun answer () Plant (as @Plant_0 Plant))
    ; named constants with the same value(s):
    (define-fun rose () Plant (as @Plant_0 Plant))
    >>> answer_values(m, '(assert true)')
    '(the fragment declares no symbols, so the verdict is all there is)'
    """
    wanted = {name for name in map(_declared_name, split_sexprs(fragment)) if name}
    if not wanted:
        return "(the fragment declares no symbols, so the verdict is all there is)"
    entries = split_sexprs(model or "")
    own = [e for e in entries if _declared_name(e) in wanted]
    abstract = set().union(*map(_abstract_values, own)) if own else set()
    related = []
    if abstract:
        for e in entries:
            name = _declared_name(e)
            if name in wanted or not re.match(r"^\(\s*define-fun\s+\S+\s+\(\)", e):
                continue
            if _abstract_values(e) & abstract:
                related.append(e)
                if len(related) >= MAX_RELATED * len(abstract):
                    break
    text = "\n".join(own) or "(the model gives no value for the fragment's unknowns)"
    if related:
        text += "\n; named constants with the same value(s):\n" + "\n".join(related)
    unnamed = sorted(abstract - set().union(*map(_abstract_values, related))) \
        if related else sorted(abstract)
    if unnamed:
        text += (f"\n; no named constant has the value(s) {', '.join(unnamed)}: "
                 "the theory does not pin the answer down")
    return _trim(text, MAX_VALUES_CHARS)


def run(theory: str, fragment: str | None) -> dict:
    script = build_cvc5_script(theory, fragment)
    with tempfile.NamedTemporaryFile("w", suffix=".smt2", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        proc = subprocess.run(
            [CVC5_PATH, "--produce-models", "--incremental", path],
            capture_output=True, text=True, timeout=TIMEOUT_SECS, check=False)
    except subprocess.TimeoutExpired:
        return {"validity": "TIMEOUT",
                "detail": f"cvc5 did not finish within {TIMEOUT_SECS:.0f}s; "
                          "simplify the theory (avoid quantifiers)."}
    finally:
        os.unlink(path)
    # run_cvc5 reports ANY non-zero cvc5 exit as UNSAT. Here a non-zero exit
    # with an (error ...) on stderr stays ERROR, which tells the agent it has a
    # syntax problem rather than a logical contradiction. The SAT verdict, the
    # only one the harness accepts, is identical in both.
    validity, model = classify_cvc5_output(proc.stdout, proc.stderr)
    if proc.returncode != 0 and validity != "ERROR":
        validity = "UNSAT"
        model = None
    result: dict = {"validity": validity}
    if validity != "SAT":
        # Why it failed. cvc5 prints parse errors on stdout; a bare verdict
        # (unsat/unknown) adds nothing to "validity", so drop it.
        detail = "\n".join(
            line for line in (proc.stdout + "\n" + proc.stderr).splitlines()
            if line.strip()
            and line.strip().lower() not in ("unsat", "unknown")
            and "cannot get model" not in line)
        if detail:
            result["detail"] = _trim(detail)
    elif fragment is not None:
        result["values"] = answer_values(model, fragment)
    return result


def _read_in_cwd(name: str) -> str:
    """Read a file that must live under the current (workspace) directory."""
    path = Path(name).resolve()
    if not path.is_relative_to(Path.cwd().resolve()):
        raise ValueError(f"{name}: only files inside the working directory "
                         "can be checked")
    return path.read_text()


def _ask_one(theory: str, name: str, tag: bool) -> dict:
    try:
        result = run(theory, _read_in_cwd(name))
    except (OSError, ValueError) as ex:
        result = {"validity": "ERROR", "detail": str(ex)}
    return {"fragment": name, **result} if tag else result


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "check":
        try:
            result = run(_read_in_cwd(argv[1]), None)
        except (OSError, ValueError) as ex:
            result = {"validity": "ERROR", "detail": str(ex)}
        print(json.dumps(result))
        return 0
    if len(argv) >= 3 and argv[0] == "ask" and len(argv) - 2 <= MAX_FRAGMENTS:
        try:
            theory = _read_in_cwd(argv[1])
        except (OSError, ValueError) as ex:
            print(json.dumps({"validity": "ERROR", "detail": str(ex)}))
            return 0
        names = argv[2:]
        workers = max(1, min(len(names), os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(
                lambda n: _ask_one(theory, n, tag=len(names) > 1), names))
        for result in results:
            print(json.dumps(result))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
