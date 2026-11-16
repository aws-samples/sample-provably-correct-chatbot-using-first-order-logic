# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# pylint: disable=too-many-arguments, too-many-positional-arguments, too-many-locals
# pylint: disable=not-context-manager # doesn't get context from otel.py
"""
kiro-cli theory engine: build ONE valid and useful cvc5 theory of a whole
document with a kiro-cli agent session (auto_formalizer.formalize_document
calls kiro_create_valid_useful_theory once per build).

How a build runs
----------------
1. A throw-away workspace is laid out: sentences.txt (every sentence of the
   document, standalone), test_set.json (the question / expected-answer
   pairs), an empty fragments/ dir, and a workspace-local agent config.
2. `kiro-cli chat --no-interactive` runs the agent there. The agent writes
   theory.smt2, converts each test question into an SMT-LIB fragment, runs
   theory + fragment through kiro_tools/cvc5_checker.py (the same script
   assembly run_cvc5 uses), turns the model into an answer, judges it, and
   revises the theory until enough questions pass. It records its evaluation
   in answers.json.
3. The harness does NOT trust the agent's report. It re-runs cvc5 on the
   theory and on every fragment, and by default (kiro_reverify_answers)
   re-derives each answer from the cvc5 model and judges it with the existing
   get_model_output_2_nat_lang + llm_as_judge, verify_pool_size questions at a
   time. Only that count decides success.
4. If the count falls short, the SAME session is resumed with the harness's
   report, up to kiro_max_attempts sessions within kiro_timeout_seconds. A
   session that goes silent for kiro_idle_timeout_seconds is killed and
   resumed with a nudge (up to kiro_max_stall_resumes times), which does not
   use up an attempt.

Sandbox
-------
The source sentences come from user-supplied documents, so the agent is
treated as untrusted:
  * fs_read / fs_write are auto-allowed only inside the workspace, and the only
    auto-allowed shell command is the checker (anchored regex). Anything else
    needs an approval that headless mode cannot give, so it is refused.
  * AWS credential variables are removed from the kiro-cli environment, so
    even a permitted command cannot reach the task role.

Auth: KIRO_API_KEY (headless mode). In the builder it is read from Secrets
Manager (KIRO_API_KEY_SECRET_ID) when not already in the environment. Without a
key, kiro-cli uses whatever login the host already has (local development).
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import time
from typing import Callable, Dict, List, Optional, Tuple

import globals as g
from otel import start_span
from prompts import get_prompt
from sentences import Document, Sentence
from utils import Sat, Statistics, cancel_requested, kill_process_tree

REPO_ROOT = Path(__file__).resolve().parent
CHECKER = REPO_ROOT / "kiro_tools" / "cvc5_checker.py"
AGENT_NAME = "theory-formalizer"
# What the agent types to run the checker. The allow-list regex is built from
# this same string, so the two cannot drift apart.
CHECKER_CMD = f"python3 {CHECKER}"

# Bounds on the harness's own cvc5 re-checks.
VERIFY_THEORY_TIMEOUT_SECS = 120.0
VERIFY_FRAGMENT_TIMEOUT_SECS = 60.0
# Leave at least this much budget for a resumed session to be worth starting.
MIN_ATTEMPT_SECS = 120.0
# _run_kiro's return codes for sessions it killed, and how often it checks.
TIMED_OUT_RC = -9
STALLED_RC = -14
CANCELLED_RC = -15
WATCHDOG_POLL_SECS = 2.0

# Removed from the kiro-cli environment so the agent's shell can never reach
# AWS. (kiro-cli authenticates with KIRO_API_KEY or its own login, not AWS.)
_AWS_CREDENTIAL_VARS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
    "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
)

_api_key_cache: Optional[str] = None
_api_key_lock = threading.Lock()


# --- Public entry point ----------------------------------------------------
def kiro_create_valid_useful_theory(
        sentences: List[Sentence],
        qs_and_as: List[Tuple[str, str]],
        n_correct_answers_reqd: int,
        stats: Statistics,
        on_event: Optional[Callable[[dict], None]] = None) -> dict:
    """
    Build a valid, useful theory of `sentences` with kiro-cli.

    Returns theory (None when no verified theory was found),
    n_correct_answers_reqd and correctly_answered_questions. `on_event`
    receives session_start / session_end events (see
    auto_formalizer.report_progress for their fields).
    """
    emit = on_event or (lambda event: None)
    # Allow for two errors.
    if n_correct_answers_reqd > len(qs_and_as) - 2:
        n_correct_answers_reqd = max(1, len(qs_and_as) - 2)
    with start_span("kcvut: kiro build") as span:
        start = time()
        deadline = start + g.p.kiro_timeout_seconds
        ws = _make_workspace(sentences, qs_and_as, n_correct_answers_reqd)
        print(f"kcvut: workspace {ws}; {len(sentences):,} sentences; need "
              f"{n_correct_answers_reqd:,}/{len(qs_and_as):,} correct; budget "
              f"{g.p.kiro_timeout_seconds:,.0f}s, {g.p.kiro_max_attempts} attempts")
        best: Optional[dict] = None
        try:
            message = "Complete the task described in TASK.md in this directory."
            # A stalled session (killed by the watchdog) is resumed with a
            # nudge and does not use up one of the attempts, up to
            # kiro_max_stall_resumes times; the deadline still applies.
            attempt, stalls, sessions, kind = 0, 0, 0, "build"
            while attempt < g.p.kiro_max_attempts:
                remaining = deadline - time()
                if sessions > 0 and remaining < MIN_ATTEMPT_SECS:
                    print(f"kcvut: {remaining:.0f}s left, not starting session "
                          f"#{sessions + 1}")
                    break
                sessions += 1
                emit(dict(type="session_start", session=sessions, kind=kind))
                session_start = time()
                rc, credits = _run_kiro(
                    ws, message, resume=sessions > 1,
                    timeout=max(remaining, 1.0),
                    idle_timeout=g.p.kiro_idle_timeout_seconds)
                stats.add_kiro_session(credits)
                if rc == CANCELLED_RC:
                    emit(dict(type="session_end", session=sessions, kind=kind,
                              rc=rc, stalled=False, credits=credits,
                              duration=time() - session_start, valid=False,
                              n_correct=0, correct_questions=[], theory=None))
                    span.result("cancelled")
                    return dict(theory=None,
                                n_correct_answers_reqd=n_correct_answers_reqd,
                                correctly_answered_questions=[])
                label = f"attempt #{attempt + 1}" + (f" (resume #{stalls})" if stalls else "")
                print(f"kcvut: {label} kiro-cli rc={rc}"
                      f"{' (stalled)' if rc == STALLED_RC else ''} "
                      f"credits={'?' if credits is None else f'{credits:.3f}'} "
                      f"[{(time() - start) / 60:.1f}m elapsed]")
                verdict = _verify(ws, qs_and_as, n_correct_answers_reqd, stats)
                print(f"kcvut: {label} verified: valid="
                      f"{verdict['valid']} correct={len(verdict['correct']):,}"
                      f"/{n_correct_answers_reqd:,} needed")
                emit(dict(type="session_end", session=sessions, kind=kind,
                          rc=rc, stalled=rc == STALLED_RC, credits=credits,
                          duration=time() - session_start,
                          valid=verdict["valid"],
                          n_correct=len(verdict["correct"]),
                          correct_questions=list(verdict["correct"]),
                          theory=verdict["theory"]))
                if verdict["valid"] and (best is None or
                                         len(verdict["correct"]) > len(best["correct"])):
                    best = verdict
                if verdict["valid"] and \
                        len(verdict["correct"]) >= n_correct_answers_reqd:
                    span.result(f"verified theory in {sessions} session(s)")
                    return dict(theory=verdict["theory"],
                                n_correct_answers_reqd=n_correct_answers_reqd,
                                correctly_answered_questions=verdict["correct"])
                if rc == STALLED_RC and stalls < g.p.kiro_max_stall_resumes:
                    stalls += 1
                    kind = "stall_resume"
                    message = get_prompt(
                        "kiro_stall_nudge", report=verdict["report"],
                        idle_minutes=round(g.p.kiro_idle_timeout_seconds / 60))
                    continue
                attempt += 1
                kind = "feedback"
                message = get_prompt("kiro_verification_feedback",
                                     report=verdict["report"])
            span.result("no verified theory")
            print("kcvut: giving up: no theory met the target"
                  + (f" (best valid one answered {len(best['correct']):,})"
                     if best else ""))
            return dict(theory=None,
                        n_correct_answers_reqd=n_correct_answers_reqd,
                        correctly_answered_questions=[])
        finally:
            if os.environ.get("CHATBOT_KIRO_KEEP_WORKDIR") == "1":
                print(f"kcvut: keeping workspace {ws}")
            else:
                shutil.rmtree(ws, ignore_errors=True)


# --- Workspace -------------------------------------------------------------
def _workspace_root() -> Path:
    root = Path(os.environ.get("CHATBOT_KIRO_WORKDIR")
                or tempfile.gettempdir()) / "chatbot-kiro"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _question_ids(qs_and_as: List[Tuple[str, str]]) -> List[str]:
    return [f"q{i + 1}" for i in range(len(qs_and_as))]


def _make_workspace(sentences: List[Sentence],
                    qs_and_as: List[Tuple[str, str]],
                    n_required: int) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="build-", dir=_workspace_root()))
    (ws / "fragments").mkdir()
    lines = Document.make_standalone_lines(sentences)
    (ws / "sentences.txt").write_text("\n".join(lines) + "\n")
    test_set = [dict(id=qid, question=q, expected_answer=a)
                for qid, (q, a) in zip(_question_ids(qs_and_as), qs_and_as)]
    (ws / "test_set.json").write_text(json.dumps(test_set, indent=2))
    task = get_prompt("kiro_task", n_sentences=len(lines),
                      **_target(n_required, qs_and_as))
    (ws / "TASK.md").write_text(task)
    agents = ws / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / f"{AGENT_NAME}.json").write_text(
        json.dumps(agent_config(ws), indent=2))
    return ws


def _target(n_required: int, qs_and_as: List[Tuple[str, str]]) -> dict:
    n_total = len(qs_and_as)
    return dict(n_required=n_required, n_total=n_total,
                percent=round(100 * n_required / max(1, n_total)))


def agent_config(ws: Path) -> dict:
    """
    The workspace-local kiro-cli agent. Nothing is blanket-trusted:
    toolsSettings auto-allows reads/writes inside `ws` and the checker command
    only; everything else would need an interactive approval, which
    --no-interactive refuses.
    """
    ws = ws.resolve()
    # A relative .smt2 path made of plain name segments: no leading "/", no
    # "..", no shell metacharacters. So the checker can only read files in the
    # workspace, and nothing can be chained after it.
    arg = r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*\.smt2"
    # `ask` takes the theory and 1-50 fragments (the checker's MAX_FRAGMENTS).
    checker_re = (f"^{re.escape(CHECKER_CMD)} "
                  f"(check {arg}|ask {arg}( {arg}){{1,50}})$")
    return {
        "name": AGENT_NAME,
        "description": "Builds and tests the SMT-LIB theory of one document",
        "prompt": get_prompt("kiro_agent_rules", checker_cmd=CHECKER_CMD),
        "tools": ["fs_read", "fs_write", "execute_bash"],
        "allowedTools": [],
        "toolsSettings": {
            "execute_bash": {"allowedCommands": [checker_re],
                             "denyByDefault": True},
            "fs_read": {"allowedPaths": [str(ws), f"{ws}/**"]},
            "fs_write": {"allowedPaths": [str(ws), f"{ws}/**"]},
        },
        "mcpServers": {},
        "includeMcpJson": False,
        "resources": [],
    }


# --- Running kiro-cli ------------------------------------------------------
def _kiro_api_key() -> Optional[str]:
    """KIRO_API_KEY from the environment, else from Secrets Manager (cached)."""
    global _api_key_cache  # pylint: disable=global-statement
    if os.environ.get("KIRO_API_KEY"):
        return os.environ["KIRO_API_KEY"]
    secret_id = os.environ.get("KIRO_API_KEY_SECRET_ID")
    if not secret_id:
        return None
    with _api_key_lock:
        if _api_key_cache is None:
            import boto3  # pylint: disable=import-outside-toplevel
            client = boto3.client("secretsmanager",
                                  region_name=os.environ.get("AWS_REGION"))
            _api_key_cache = client.get_secret_value(
                SecretId=secret_id)["SecretString"].strip()
        return _api_key_cache


def _kiro_env(ws: Path) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _AWS_CREDENTIAL_VARS}
    api_key = _kiro_api_key()
    if api_key:
        env["KIRO_API_KEY"] = api_key
        # Headless sessions need no login state, so give each build its own
        # HOME: concurrent builds on one host never share kiro-cli's session
        # store, and --resume finds this build's conversation.
        home = ws / ".home"
        home.mkdir(exist_ok=True)
        env["HOME"] = str(home)
    # The checker resolves cvc5 the same way run_cvc5 does.
    env.setdefault("CVC5_PATH", f"{Path.home()}/bin/cvc5")
    return env


def _die_with_parent() -> None:
    """preexec_fn: SIGKILL kiro-cli if this worker dies (Linux only)."""
    try:
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def kiro_command(message: str, resume: bool) -> List[str]:
    """The kiro-cli argv for one session (or a resume of it)."""
    # stream-json (JSON Lines) carries the session's metered credit usage;
    # _Transcript turns it back into a readable transcript for CloudWatch.
    cmd = [os.environ.get("KIRO_CLI_PATH", "kiro-cli"), "chat",
           "--no-interactive", "--agent", AGENT_NAME, "--wrap", "never",
           "--output-format", "stream-json"]
    if g.p.kiro_model:
        cmd += ["--model", g.p.kiro_model]
    if resume:
        cmd.append("--resume")
    cmd.append(message)
    return cmd


class _Transcript:
    """
    Turns kiro-cli's stream-json output into readable transcript lines and
    collects the session's metered credits.

    Events (one JSON object per line, {"type", "data"}): sessionUpdate
    (agent_message_chunk text, tool_call, tool_call_update), metadata (the
    one at the end of each turn carries meteringUsage: a list of {value,
    unit: "credit"}, one per model request), runFinished. Lines that are not
    JSON (kiro-cli's own errors) pass through unchanged.
    """
    MAX_TOOL_OUTPUT_LINES = 40
    MAX_LINE_CHARS = 2000

    def __init__(self) -> None:
        self.credits: Optional[float] = None
        # When kiro-cli last printed anything (the stall watchdog reads this).
        self.last_event = time()
        self._text = ""

    def feed(self, line: str) -> List[str]:
        """The transcript lines for one line of kiro-cli output."""
        self.last_event = time()
        line = line.rstrip("\n")
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict) or "type" not in event:
            return self._flush_text() + ([line] if line.strip() else [])
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "metadata":
            usage = data.get("meteringUsage")
            if isinstance(usage, list):
                spent = sum(float(u.get("value") or 0) for u in usage
                            if isinstance(u, dict) and u.get("unit") == "credit")
                self.credits = (self.credits or 0.0) + spent
                return self._flush_text() + [
                    f"[turn used {spent:.4f} credits over {len(usage)} "
                    f"request(s), {data.get('turnDurationMs', 0) / 1000:.0f}s]"]
            return []
        if kind == "sessionUpdate":
            return self._session_update(data.get("update") or {})
        if kind == "runFinished":
            return self._flush_text() + [
                f"[run finished: {data.get('status')}, {data.get('stopReason')}]"]
        return []

    def close(self) -> List[str]:
        """Any agent text still buffered (no trailing newline)."""
        return self._flush_text()

    def _session_update(self, update: dict) -> List[str]:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                self._text += content.get("text") or ""
                *done, self._text = self._text.split("\n")
                return [self._clip(t) for t in done]
            return []
        if kind == "tool_call":
            return self._flush_text() + [self._clip(
                f"● {update.get('title') or update.get('kind') or 'tool'}"
                f" {json.dumps(update.get('rawInput'), ensure_ascii=False)}")]
        if kind == "tool_call_update" and update.get("status") in ("completed", "failed"):
            out = [f"  ↳ {update.get('status')}"]
            out += [f"    {self._clip(t)}" for t in
                    self._tool_output(update.get("rawOutput"))]
            return self._flush_text() + out
        return []

    def _tool_output(self, raw) -> List[str]:
        if raw is None:
            return []
        parts: List[str] = []
        items = raw.get("items") if isinstance(raw, dict) else None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("Text"), str):
                    parts.append(item["Text"])
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
        else:
            parts.append(raw if isinstance(raw, str)
                         else json.dumps(raw, ensure_ascii=False))
        lines = "\n".join(parts).splitlines()
        if len(lines) > self.MAX_TOOL_OUTPUT_LINES:
            n = len(lines) - self.MAX_TOOL_OUTPUT_LINES
            lines = lines[:self.MAX_TOOL_OUTPUT_LINES] + [f"… {n} more line(s)"]
        return lines

    def _flush_text(self) -> List[str]:
        if not self._text:
            return []
        text, self._text = self._text, ""
        return [self._clip(text)]

    def _clip(self, text: str) -> str:
        if len(text) <= self.MAX_LINE_CHARS:
            return text
        return text[:self.MAX_LINE_CHARS] + f" … (+{len(text) - self.MAX_LINE_CHARS} chars)"


def _run_kiro(ws: Path, message: str, resume: bool, timeout: float,
              idle_timeout: Optional[float] = None) -> Tuple[int, Optional[float]]:
    """
    Run one kiro-cli session in `ws`, streaming its transcript to stdout
    (CloudWatch) with a `kc:` prefix. Returns (exit code -- or TIMED_OUT_RC if
    the session was killed for running past `timeout`, STALLED_RC if it was
    killed for emitting nothing for `idle_timeout` --, credits the session
    reported, or None if it reported none, e.g. when killed). CANCELLED_RC
    when the build was killed (utils.cancel_requested) mid-session.
    """
    cmd = kiro_command(message, resume)
    print(f"kc: $ {' '.join(cmd[:-1])} <message>  (timeout {timeout:.0f}s)")
    # start_new_session: kiro-cli leads its own session (sid == its pid), so
    # kill_process_tree can find everything it started -- kiro-cli-chat and
    # the node server it runs in a separate process group -- even after those
    # are re-parented. Killing kiro-cli alone left them alive holding our
    # stdout pipe, and the old `with Popen` then blocked forever closing the
    # pipe under the reader thread, hanging the build.
    # argv list, no shell: kiro_command builds a fixed kiro-cli command line
    # and passes the message as one argument, so it cannot inject a command.
    proc = subprocess.Popen(cmd, cwd=ws, env=_kiro_env(ws),  # nosec B603 # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            start_new_session=True,
                            preexec_fn=_die_with_parent)

    transcript = _Transcript()

    def pump() -> None:
        try:
            for line in proc.stdout:
                for out in transcript.feed(line):
                    print(f"kc: {out}", flush=True)
        except (OSError, ValueError):
            pass
        for out in transcript.close():
            print(f"kc: {out}", flush=True)
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    # Watchdog: stream-json emits an event for every message chunk, tool call
    # and tool result, so a session that emits nothing for idle_timeout is
    # stuck (sessions were seen silent for 30+ minutes until the time budget
    # ran out). Kill it so the caller can resume with a nudge.
    started = time()
    while True:
        try:
            rc = proc.wait(timeout=WATCHDOG_POLL_SECS)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time()
        if now - started >= timeout:
            print(f"kc: session exceeded {timeout:.0f}s; killing its process tree")
            rc = TIMED_OUT_RC
            break
        if cancel_requested():
            print("kc: build cancelled; killing the session's process tree")
            rc = CANCELLED_RC
            break
        if idle_timeout and now - transcript.last_event >= idle_timeout:
            print(f"kc: session stalled: no output for "
                  f"{now - transcript.last_event:.0f}s; killing its process tree")
            rc = STALLED_RC
            break
    # Always sweep the session: on timeout it kills the whole tree; after a
    # normal exit it reaps any orphans kiro-cli left behind.
    killed = kill_process_tree(proc.pid)
    if rc in (TIMED_OUT_RC, STALLED_RC, CANCELLED_RC) or len(killed) > 0:
        print(f"kc: killed {len(killed)} kiro-cli process(es)")
    proc.wait()
    # The pipe hits EOF once every holder is dead. Join BEFORE closing stdout:
    # closing a file another thread is blocked reading deadlocks.
    reader.join(timeout=30)
    if reader.is_alive():
        print("kc: transcript reader still blocked; abandoning it")
    else:
        proc.stdout.close()
    credits = transcript.credits
    print("kc: session credits: "
          + ("not reported" if credits is None else f"{credits:.4f}"))
    return rc, credits


# --- Independent verification ----------------------------------------------
def _read_answers(ws: Path) -> Tuple[Dict[str, dict], Optional[str]]:
    path = ws / "answers.json"
    if not path.exists():
        return {}, "answers.json was not written"
    try:
        entries = json.loads(path.read_text())
        return {str(e["id"]): e for e in entries
                if isinstance(e, dict) and "id" in e}, None
    except (ValueError, TypeError) as ex:
        return {}, f"answers.json is not a JSON list of objects ({ex})"


def _read_fragment(ws: Path, entry: dict, qid: str) -> Tuple[Optional[str], str]:
    rel = entry.get("fragment") or f"fragments/{qid}.smt2"
    path = (ws / rel).resolve()
    if not path.is_relative_to(ws.resolve()):
        return None, f"fragment path {rel!r} is outside the workspace"
    if not path.is_file():
        return None, f"fragment {rel} does not exist"
    return path.read_text(), rel


def _verify(ws: Path, qs_and_as: List[Tuple[str, str]], n_required: int,
            stats: Statistics) -> dict:
    """
    Re-check the agent's result. Returns valid, theory, correct (question
    strings that independently verified), and a report for the agent.
    """
    # Imported here: auto_formalizer imports this module lazily.
    from auto_formalizer import (  # pylint: disable=import-outside-toplevel
        run_cvc5, get_model_output_2_nat_lang, llm_as_judge)
    with start_span("kv: verify kiro theory"):
        theory_path = ws / "theory.smt2"
        if not theory_path.is_file():
            return dict(valid=False, theory=None, correct=[],
                        report="theory.smt2 was not written.")
        theory = theory_path.read_text()
        r = run_cvc5(theory, None, stats, print_out_theories=False,
                     timeout=VERIFY_THEORY_TIMEOUT_SECS)
        if r["validity"] != Sat.SAT:
            return dict(valid=False, theory=theory, correct=[],
                        report=f"theory.smt2 on its own is {r['validity']}, "
                               f"not SAT. cvc5 stderr: {(r['stderr'] or '')[:1500]}")
        answers, problem = _read_answers(ws)
        if problem:
            return dict(valid=True, theory=theory, correct=[], report=problem)
        # Re-check the questions in parallel. Once enough have verified, the
        # rest are skipped (they would only cost cvc5 runs and LLM calls).
        lock = threading.Lock()
        n_ok = [0]

        def check(qid: str, question: str, expected: str) -> Tuple[Optional[bool], str]:
            """(True | False | None = skipped, note for a failure)."""
            with lock:
                if n_ok[0] >= n_required:
                    return None, ""
            entry = answers.get(qid)
            if entry is None:
                return False, f"{qid}: no entry in answers.json"
            fragment, where = _read_fragment(ws, entry, qid)
            if fragment is None:
                return False, f"{qid}: {where}"
            q_stats = Statistics()
            try:
                fr = run_cvc5(theory, fragment, q_stats, print_out_theories=False,
                              timeout=VERIFY_FRAGMENT_TIMEOUT_SECS)
                model = fr["get_model_output"]
                if fr["validity"] != Sat.SAT or not model:
                    return False, (f"{qid}: theory + {where} is {fr['validity']} "
                                   "when re-checked, so the answer is I don't know")
                if g.p.kiro_reverify_answers:
                    answer = get_model_output_2_nat_lang(
                        valid_theory=theory, question=question,
                        q_logical_form=fragment, get_model_output=model,
                        stats=q_stats)["answer"] or "I don't know"
                    ok = llm_as_judge(question, expected, answer, q_stats)["are_same"]
                else:
                    answer = str(entry.get("answer", ""))
                    ok = entry.get("correct") is True
            finally:
                with lock:
                    stats.add_another(q_stats)
            if ok:
                with lock:
                    n_ok[0] += 1
                return True, ""
            return False, (f"{qid}: reading the model of {where} gives the "
                           f"answer {answer!r}, which does not match the "
                           f"expected answer {expected!r}")

        qids = _question_ids(qs_and_as)
        with ThreadPoolExecutor(max_workers=max(1, g.p.verify_pool_size)) as pool:
            outcomes = list(pool.map(check, qids, [q for q, _ in qs_and_as],
                                     [a for _, a in qs_and_as]))
        correct = [q for (q, _), (ok, _) in zip(qs_and_as, outcomes) if ok]
        notes = [note for ok, note in outcomes if ok is False]
        report = (f"{len(correct)} of the {len(qs_and_as)} questions verified "
                  f"correct; {n_required} are needed.")
        if notes:
            report += "\n" + "\n".join(f"- {n}" for n in notes)
        return dict(valid=True, theory=theory, correct=correct, report=report)
