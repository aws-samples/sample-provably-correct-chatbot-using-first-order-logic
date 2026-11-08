# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Job introspection: record the progress of a theory build and read it back for
the "Job Introspection" tab.

A build is ONE unit of work (auto_formalizer.formalize_document): it loads
the document's sentences, generates the test set, then runs one or more
kiro-cli sessions (the first build, then resumes with the harness's
verification report, or with a nudge after a stall) until the theory is
verified or the budget runs out.

Builder side (Recorder)
  store.run_job installs Recorder.handle as auto_formalizer's progress
  callback. It keeps one JSON document per job in the theory bucket,

      jobs/<job_id>/introspection/state.json

  rewritten by a background thread at most every `flush_secs` (and at once on
  phase changes and session boundaries), plus

      jobs/<job_id>/introspection/theory.smt2   the latest re-verified
                                                theory (the final one at the
                                                end of the build)
      jobs/<job_id>/introspection/qas.json      the test set, each question
                                                marked with whether the
                                                latest verification credited
                                                it

Read side
  load_state / load_theory / load_qas / cost_summary / build_logs, used by
  store.py. The build log is the builder task's whole CloudWatch stream.

All S3 / CloudWatch clients are passed in, so this module has no AWS setup of
its own and is testable with fakes.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

STATE_VERSION = 2
# Build statuses written by the recorder. The UI adds "stopped" (the job
# ended while the build was still running, e.g. it was killed).
RUNNING, SUCCEEDED, FAILED = "running", "succeeded", "failed"
# Phases, in order.
PHASES = ("sentences", "test_set", "theory", "done")

# Bounds on one log request, so the endpoint stays well inside the CloudFront
# origin timeout even for a very chatty build.
MAX_LOG_PAGES = 40
MAX_LOG_SECONDS = 20.0


def state_key(job_id: str) -> str:
    return f"jobs/{job_id}/introspection/state.json"


def theory_key(job_id: str) -> str:
    return f"jobs/{job_id}/introspection/theory.smt2"


def qas_key(job_id: str) -> str:
    return f"jobs/{job_id}/introspection/qas.json"


# --- Builder side ------------------------------------------------------------
class Recorder:
    """Accumulates build events for one job and persists them to S3."""

    def __init__(self, s3, bucket: str, job_id: str, flush_secs: float = 3.0,
                 clock: Callable[[], float] = time.time,
                 start_thread: bool = True):
        self._s3 = s3
        self._bucket = bucket
        self._job_id = job_id
        self._clock = clock
        self._lock = threading.Lock()
        self._dirty = True
        self._stop = threading.Event()
        self._qas: List[List[str]] = []
        now = int(clock())
        self.state: Dict[str, Any] = {
            "version": STATE_VERSION, "job_id": job_id, "updated_at": now,
            "status": RUNNING, "phase": "sentences", "started_at": now,
            # When each phase started and ended (epoch seconds; ended is None
            # while it runs). The build starts in "sentences".
            # A finished phase also has bedrock_dollars and kiro_credits:
            # what it spent.
            "phase_times": {"sentences": {
                "started_at": now, "ended_at": None,
                "spent_at_start": {"bedrock_dollars": 0.0, "kiro_credits": 0.0}}},
            "finished_at": None, "duration": None,
            "n_sentences": None, "sentences": [],
            "raw_sentences": [], "standalone_sentences": [],
            "n_questions": None, "n_required": None, "n_correct": None,
            "theory_chars": None, "sessions": [],
            "kiro_credits": 0.0, "kiro_sessions": 0,
            "kiro_unmetered_sessions": 0}
        self._thread: Optional[threading.Thread] = None
        if start_thread:
            self._thread = threading.Thread(
                target=self._flush_loop, args=(flush_secs,), daemon=True,
                name=f"introspection-{job_id[:8]}")
            self._thread.start()

    # -- events --------------------------------------------------------------
    def handle(self, event: dict) -> None:
        """auto_formalizer progress callback."""
        handlers = {"start": self._start, "phase": self._phase,
                    "test_set": self._test_set,
                    "session_start": self._session_start,
                    "session_end": self._session_end, "end": self._end}
        handler = handlers.get(event.get("type"))
        if handler is None:
            return
        with self._lock:
            now = int(self._clock())
            puts = handler(event, now) or []
            self.state["updated_at"] = now
            self._dirty = True
        for key, body, content_type in puts:
            self._put(key, body, content_type)
        self.flush()  # events are rare (a handful per build): write at once

    def _start(self, event: dict, now: int) -> None:
        sentences = list(event.get("sentences") or [])
        raw = list(event.get("raw_sentences") or [])
        standalone = list(event.get("standalone_sentences") or [])
        if len(standalone) != len(raw):
            standalone = [None] * len(raw)
        self.state.update(sentences=sentences, raw_sentences=raw,
                          standalone_sentences=standalone,
                          n_sentences=len(raw) or len(sentences))

    def _enter_phase(self, phase: str, now: int,
                     spent: Optional[dict] = None) -> None:
        """
        End the current phase at `now` and start `phase` (unless done).
        `spent` is the build's running spend at this moment; a phase's own
        spend is the difference between its end and its start.
        """
        times = self.state.setdefault("phase_times", {})
        current = times.get(self.state["phase"])
        if current is not None and current.get("ended_at") is None \
                and self.state["phase"] != phase:
            current["ended_at"] = now
            start = current.pop("spent_at_start", None)
            if spent is not None and start is not None:
                for key in ("bedrock_dollars", "kiro_credits"):
                    current[key] = max(0.0, float(spent.get(key) or 0.0)
                                       - float(start.get(key) or 0.0))
        self.state["phase"] = phase
        if phase != "done" and phase not in times:
            times[phase] = {"started_at": now, "ended_at": None,
                            "spent_at_start": dict(spent) if spent is not None
                            else {"bedrock_dollars": 0.0, "kiro_credits": 0.0}}

    def _phase(self, event: dict, now: int) -> None:
        if event.get("phase") in PHASES:
            self._enter_phase(event["phase"], now, event.get("spent"))

    def _test_set(self, event: dict, now: int) -> List[tuple]:
        self._qas = [list(qa) for qa in event.get("qs_and_as") or [] if len(qa) >= 2]
        self.state.update(n_questions=len(self._qas),
                          n_required=event.get("n_required"))
        return [self._qas_put(set())]

    def _session_start(self, event: dict, now: int) -> None:
        self.state["sessions"].append(dict(
            session=int(event.get("session") or len(self.state["sessions"]) + 1),
            kind=event.get("kind") or "build", status=RUNNING,
            started_at=now, finished_at=None, duration=None, rc=None,
            stalled=None, credits=None, valid=None, n_correct=None))

    def _session_end(self, event: dict, now: int) -> List[tuple]:
        n = int(event.get("session") or 0)
        session = next((s for s in self.state["sessions"] if s["session"] == n), None)
        if session is None:  # missed its start; record it anyway
            self._session_start(event, now)
            session = self.state["sessions"][-1]
        credits = event.get("credits")
        session.update(status="done", finished_at=now,
                       duration=event.get("duration"), rc=event.get("rc"),
                       stalled=bool(event.get("stalled")),
                       credits=None if credits is None else float(credits),
                       valid=bool(event.get("valid")),
                       n_correct=event.get("n_correct"))
        self.state["kiro_sessions"] += 1
        if credits is None:
            self.state["kiro_unmetered_sessions"] += 1
        else:
            self.state["kiro_credits"] += float(credits)
        puts = [self._qas_put(set(event.get("correct_questions") or []))]
        theory = event.get("theory")
        if event.get("valid") and theory:
            self.state.update(n_correct=event.get("n_correct"),
                              theory_chars=len(theory))
            puts.append((theory_key(self._job_id), theory,
                         "text/plain; charset=utf-8"))
        return puts

    def _end(self, event: dict, now: int) -> List[tuple]:
        ok = bool(event.get("ok"))
        self._enter_phase("done", now, event.get("spent"))
        self.state.update(status=SUCCEEDED if ok else FAILED,
                          finished_at=now, duration=event.get("duration"),
                          n_correct=event.get("n_correct"),
                          n_required=event.get("n_required",
                                               self.state["n_required"]))
        for session in self.state["sessions"]:
            if session["status"] == RUNNING:
                session.update(status="done", finished_at=now)
        theory = event.get("theory")
        puts = []
        if ok and theory:
            self.state["theory_chars"] = len(theory)
            puts.append((theory_key(self._job_id), theory,
                         "text/plain; charset=utf-8"))
        if self._qas:
            puts.append(self._qas_put(set(event.get("correct_questions") or [])))
        return puts

    def _qas_put(self, correct: set) -> tuple:
        doc = {"n_required": self.state["n_required"],
               "questions": [{"question": q, "expected_answer": a,
                              "correct": q in correct}
                             for q, a, *_ in self._qas]}
        return (qas_key(self._job_id), json.dumps(doc), "application/json")

    # -- persistence -------------------------------------------------------
    def _put(self, key: str, body: str, content_type: str) -> None:
        try:
            self._s3.put_object(Bucket=self._bucket, Key=key,
                                Body=body.encode("utf-8"),
                                ContentType=content_type)
        except Exception as ex:  # pylint: disable=broad-except
            print(f"introspection: could not write {key}: {ex}", flush=True)

    def flush(self) -> None:
        """Write the state document if it changed since the last write."""
        with self._lock:
            if not self._dirty:
                return
            body = json.dumps(self.state, separators=(",", ":"))
            self._dirty = False
        self._put(state_key(self._job_id), body, "application/json")

    def _flush_loop(self, every: float) -> None:
        while not self._stop.wait(every):
            self.flush()

    def close(self) -> None:
        """Stop the background writer and write the final state."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.flush()


# --- Read side ---------------------------------------------------------------
# $ per kiro credit, applied when reading (so the rate can change without
# rebuilding). Default: the add-on / overage price, i.e. the marginal cost of
# a build's credits. Credits included in a plan work out cheaper.
DEFAULT_DOLLARS_PER_CREDIT = 0.04


def dollars_per_credit() -> float:
    try:
        return float(os.environ.get("KIRO_DOLLARS_PER_CREDIT")
                     or DEFAULT_DOLLARS_PER_CREDIT)
    except ValueError:
        return DEFAULT_DOLLARS_PER_CREDIT


def cost_summary(state: Optional[dict], rate: float) -> Optional[dict]:
    """
    kiro-cli spend for the build, in total and per session, or None before
    any session finished. `unmetered_sessions` were killed or crashed before
    reporting usage, so credits are a lower bound when it is non-zero.

    >>> s = {"sessions": [{"session": 1, "credits": 1.5, "status": "done"},
    ...                   {"session": 2, "credits": None, "status": "done"},
    ...                   {"session": 3, "credits": None, "status": "running"}]}
    >>> c = cost_summary(s, 0.04)
    >>> (c["credits"], round(c["dollars"], 2), c["sessions"], c["unmetered_sessions"])
    (1.5, 0.06, 2, 1)
    >>> [(x["session"], x["credits"]) for x in c["per_session"]]
    [(1, 1.5), (2, None)]
    >>> cost_summary({"sessions": []}, 0.04) is None
    True
    """
    if not state:
        return None
    done = [s for s in state.get("sessions") or [] if s.get("status") == "done"]
    if not done:
        return None
    credits = sum(float(s["credits"]) for s in done if s.get("credits") is not None)
    per_session = [dict(session=s["session"], credits=s.get("credits"),
                        dollars=None if s.get("credits") is None
                        else float(s["credits"]) * rate)
                   for s in done]
    return dict(credits=credits, dollars=credits * rate, dollars_per_credit=rate,
                sessions=len(done),
                unmetered_sessions=sum(1 for s in done if s.get("credits") is None),
                per_session=per_session)


def _get(s3, bucket: str, key: str) -> Optional[str]:
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except Exception as ex:  # pylint: disable=broad-except
        if _is_missing(ex):
            return None
        raise
    return obj["Body"].read().decode("utf-8")


def load_state(s3, bucket: str, job_id: str) -> Optional[dict]:
    """The job's state document, or None if it never recorded one."""
    body = _get(s3, bucket, state_key(job_id))
    return None if body is None else json.loads(body)


def load_theory(s3, bucket: str, job_id: str) -> Optional[str]:
    """The latest re-verified theory, or None before there is one."""
    return _get(s3, bucket, theory_key(job_id))


def load_qas(s3, bucket: str, job_id: str) -> Optional[dict]:
    """
    The test set: {"n_required", "questions": [{"question",
    "expected_answer", "correct"}]}, `correct` as of the latest
    verification. None before the test set exists.
    """
    body = _get(s3, bucket, qas_key(job_id))
    return None if body is None else json.loads(body)


def _is_missing(ex: Exception) -> bool:
    code = getattr(ex, "response", {}).get("Error", {}).get("Code", "")
    return code in ("NoSuchKey", "404", "NotFound")


def builder_stream(builder_task_arn: str, prefix: str = "builder") -> str:
    """CloudWatch stream of a builder task (awslogs prefix + container name)."""
    return f"{prefix}/builder/{builder_task_arn.rsplit('/', 1)[-1]}"


def _log_pages(logs, log_group: str, stream: str, start_ms: int, end_ms: int):
    """Yield each CloudWatch page's lines of the build log, in order."""
    token = None
    while True:
        kwargs = dict(logGroupName=log_group, logStreamNames=[stream],
                      startTime=start_ms, endTime=end_ms, limit=10000)
        if token:
            kwargs["nextToken"] = token
        resp = logs.filter_log_events(**kwargs)
        page = [ev.get("message", "").rstrip("\n") for ev in resp.get("events", [])]
        token = resp.get("nextToken")
        yield page, bool(token)
        if not token:
            return


def omitted_marker(n: int, n_milestones: int = 0) -> str:
    if n_milestones:
        return (f"… {n:,} lines omitted (use Download all lines for the full "
                f"log); the {n_milestones:,} kiro milestone line(s) among "
                f"them follow …")
    return f"… {n:,} lines omitted (use Download all lines for the full log) …"


MILESTONES_END = "… end of omitted lines …"

# Lines worth keeping from an omitted middle section: where each kiro-cli
# session starts and ends and what the harness concluded, so a session deep
# in a long log is still visible.
_MILESTONE_RE = re.compile(
    r"^(kc: \$ |kcvut: |end span: kcvut|kc: session exceeded"
    r"|kc: session credits|kc: session stalled|gts: test set has|fd: )")
MAX_MILESTONES = 300


def build_logs(logs, log_group: str, stream: str, start_ms: int, end_ms: int,
               head: int = 500, tail: int = 1500,
               clock: Callable[[], float] = time.monotonic) -> dict:
    """
    The build log for display: its first `head` lines and its newest `tail`
    lines. When the log is longer, an omitted-lines marker sits between them,
    followed by the milestone lines (session starts/ends, verification
    results) from the omitted part. Same shape as store.job_logs ({source,
    lines, truncated, note}) plus total_lines and omitted (lines not shown,
    milestones aside).
    """
    first: List[str] = []
    last: deque = deque()
    tail = max(0, tail)
    milestones: List[str] = []  # kept from lines that fall out of the tail
    total, pages = 0, 0
    deadline = clock() + MAX_LOG_SECONDS
    incomplete = False
    for page, more in _log_pages(logs, log_group, stream, start_ms, end_ms):
        pages += 1
        for msg in page:
            total += 1
            if len(first) < head:
                first.append(msg)
                continue
            last.append(msg)
            if len(last) > tail:
                dropped = last.popleft()
                if _MILESTONE_RE.match(dropped) and len(milestones) < MAX_MILESTONES:
                    milestones.append(dropped)
        if more and (pages >= MAX_LOG_PAGES or clock() >= deadline):
            incomplete = True
            break
    omitted = total - len(first) - len(last)
    middle: List[str] = []
    if omitted > 0:
        middle = [omitted_marker(omitted, len(milestones))]
        if milestones:
            middle += milestones + [MILESTONES_END]
    lines = first + middle + list(last)
    note = None
    if incomplete:
        note = ("Stopped reading after the time limit; the newest lines may be "
                "missing. Use Download all lines for the full log.")
    elif not total:
        note = "No log lines for this build yet."
    return dict(source="cloudwatch", lines=lines, truncated=omitted > 0 or incomplete,
                note=note, total_lines=total, omitted=max(0, omitted))


# --- Live kiro-cli view ------------------------------------------------------
# The kiro-cli session transcript (kiro_formalizer prints it with a `kc: `
# prefix) and the harness's verdicts between sessions (`kcvut: `). CloudWatch
# narrows to lines containing either term; the regex then keeps only lines
# that START with one, so a test question quoting "kc:" is not mistaken for one.
KIRO_FILTER = '?"kc:" ?"kcvut:"'
_KIRO_LINE_RE = re.compile(r"^(kc|kcvut): ")
MAX_KIRO_LINES = 2000


def encode_cursor(ts: int, event_ids) -> str:
    """
    A read position: the newest timestamp read and the ids of the events read
    at exactly that timestamp (several events can share a millisecond).

    >>> encode_cursor(1700, ["b", "a"])
    '1700:a,b'
    """
    return f"{int(ts)}:{','.join(sorted(event_ids))}"


def decode_cursor(cursor: Optional[str]):
    """
    (timestamp, ids) from encode_cursor, or (None, set()) for none or junk.

    >>> ts, ids = decode_cursor("1700:a,b")
    >>> ts, sorted(ids)
    (1700, ['a', 'b'])
    >>> decode_cursor("nonsense")
    (None, set())
    """
    if not cursor:
        return None, set()
    ts, _, ids = cursor.partition(":")
    try:
        return int(ts), {i for i in ids.split(",") if i}
    except ValueError:
        return None, set()


def kiro_log_tail(logs, log_group: str, stream: str, start_ms: int, end_ms: int,
                  cursor: Optional[str] = None, limit: int = MAX_KIRO_LINES,
                  clock: Callable[[], float] = time.monotonic) -> dict:
    """
    The kiro-cli lines of a build log newer than `cursor`, for the live view.

    Without a cursor it returns the newest `limit` of them (the view's first
    load); with one, only what was logged since. Returns lines, the cursor
    for the next call (None until any line was read), omitted (older lines
    beyond `limit` that were not returned) and incomplete (the page or time
    cap was hit; the cursor marks how far it got, so the next call carries on
    from there and nothing is skipped).
    """
    after_ts, seen = decode_cursor(cursor)
    last_ts, last_ids = after_ts, set(seen)
    lines: deque = deque(maxlen=max(1, limit))
    omitted, pages, token, incomplete = 0, 0, None, False
    deadline = clock() + MAX_LOG_SECONDS
    begin = start_ms if after_ts is None else max(start_ms, after_ts)
    while True:
        kwargs = dict(logGroupName=log_group, logStreamNames=[stream],
                      startTime=begin, endTime=end_ms, limit=10000,
                      filterPattern=KIRO_FILTER)
        if token:
            kwargs["nextToken"] = token
        resp = logs.filter_log_events(**kwargs)
        pages += 1
        for ev in resp.get("events", []):
            ts, eid = int(ev.get("timestamp", 0)), str(ev.get("eventId", ""))
            if after_ts is not None and (ts < after_ts or (ts == after_ts and eid in seen)):
                continue
            if last_ts is None or ts > last_ts:
                last_ts, last_ids = ts, {eid}
            elif ts == last_ts:
                last_ids.add(eid)
            msg = ev.get("message", "").rstrip("\n")
            if _KIRO_LINE_RE.match(msg):
                if len(lines) == lines.maxlen:
                    omitted += 1
                lines.append(msg)
        token = resp.get("nextToken")
        if not token:
            break
        if pages >= MAX_LOG_PAGES or clock() >= deadline:
            incomplete = True
            break
    note = None
    if incomplete:
        note = "Catching up with a long log; newer lines follow on the next refresh."
    elif last_ts is None:
        note = "No kiro-cli output yet: the first session starts after the test set is ready."
    return dict(lines=list(lines),
                cursor=None if last_ts is None else encode_cursor(last_ts, last_ids),
                omitted=omitted, incomplete=incomplete, note=note)


def iter_build_log_text(logs, log_group: str, stream: str, start_ms: int,
                        end_ms: int):
    """
    Every line of the build log as text chunks (one per CloudWatch page), for
    the "Download all lines" file. Streamed, so a long log keeps the
    connection busy instead of waiting out the CloudFront origin timeout.
    """
    for page, _more in _log_pages(logs, log_group, stream, start_ms, end_ms):
        if page:
            yield "\n".join(page) + "\n"
