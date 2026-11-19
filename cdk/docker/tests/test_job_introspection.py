"""Tests for job_introspection (recorder + read side), with fake S3/CloudWatch."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import job_introspection as ji  # noqa: E402

JOB = "job-1"


class NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = 0

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body
        self.puts += 1

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey()
        return {"Body": Body(self.objects[Key])}


@pytest.fixture
def rec():
    clock = iter(range(1000, 100000))
    s3 = FakeS3()
    r = ji.Recorder(s3, "bucket", JOB, clock=lambda: next(clock), start_thread=False)
    return r, s3


def state_of(s3):
    return json.loads(s3.objects[ji.state_key(JOB)])


def qas_of(s3):
    return json.loads(s3.objects[ji.qas_key(JOB)])


QAS = [["Q1?", "A1"], ["Q2?", "A2"], ["Q3?", "A3"]]


def start_build(r):
    r.handle(dict(type="start", sentences=["s0", "s1", "s2"]))
    r.handle(dict(type="phase", phase="test_set"))
    r.handle(dict(type="test_set", qs_and_as=QAS, n_required=2))
    r.handle(dict(type="phase", phase="theory"))


def session(r, n, kind="build", **end):
    r.handle(dict(type="session_start", session=n, kind=kind))
    r.handle(dict(dict(type="session_end", session=n, kind=kind, rc=0, stalled=False,
                       credits=0.5, duration=60.0, valid=True, n_correct=1,
                       correct_questions=["Q1?"], theory="(t1)"), **end))


# --- recorder ------------------------------------------------------------------
def test_new_build_is_running_in_the_sentences_phase(rec):
    r, s3 = rec
    r.flush()
    st = state_of(s3)
    assert st["version"] == 2 and st["status"] == "running" and st["phase"] == "sentences"
    assert st["sessions"] == [] and st["started_at"] == 1000


def test_phases_sentences_and_test_set(rec):
    r, s3 = rec
    start_build(r)
    st = state_of(s3)
    assert st["phase"] == "theory" and st["sentences"] == ["s0", "s1", "s2"]
    assert st["n_sentences"] == 3 and st["n_questions"] == 3 and st["n_required"] == 2
    assert qas_of(s3) == {"n_required": 2, "questions": [
        {"question": "Q1?", "expected_answer": "A1", "correct": False},
        {"question": "Q2?", "expected_answer": "A2", "correct": False},
        {"question": "Q3?", "expected_answer": "A3", "correct": False}]}


def test_sessions_record_usage_theory_and_credit(rec):
    r, s3 = rec
    start_build(r)
    r.handle(dict(type="session_start", session=1, kind="build"))
    assert state_of(s3)["sessions"][0]["status"] == "running"
    r.handle(dict(type="session_end", session=1, kind="build", rc=0, stalled=False,
                  credits=0.75, duration=61.0, valid=True, n_correct=1,
                  correct_questions=["Q2?"], theory="(t1)"))
    st = state_of(s3)
    [s] = st["sessions"]
    assert (s["status"], s["credits"], s["n_correct"], s["valid"]) == ("done", 0.75, 1, True)
    assert st["n_correct"] == 1 and st["theory_chars"] == 4
    assert s3.objects[ji.theory_key(JOB)] == b"(t1)"
    assert [q["correct"] for q in qas_of(s3)["questions"]] == [False, True, False]
    assert (st["kiro_credits"], st["kiro_sessions"], st["kiro_unmetered_sessions"]) == (0.75, 1, 0)


def test_an_invalid_session_keeps_the_last_valid_theory(rec):
    r, s3 = rec
    start_build(r)
    session(r, 1)
    session(r, 2, kind="feedback", valid=False, theory="(broken)", n_correct=0,
            correct_questions=[], credits=None, stalled=True)
    st = state_of(s3)
    assert s3.objects[ji.theory_key(JOB)] == b"(t1)"
    assert st["n_correct"] == 1
    assert [x["kind"] for x in st["sessions"]] == ["build", "feedback"]
    assert st["sessions"][1]["stalled"] is True
    assert (st["kiro_credits"], st["kiro_sessions"], st["kiro_unmetered_sessions"]) == (0.5, 2, 1)


def test_end_marks_the_build_and_stores_the_final_theory(rec):
    r, s3 = rec
    start_build(r)
    session(r, 1)
    r.handle(dict(type="end", ok=True, theory="(final)", n_correct=2, n_required=2,
                  correct_questions=["Q1?", "Q3?"], duration=120.0))
    st = state_of(s3)
    assert (st["status"], st["phase"], st["n_correct"], st["duration"]) == \
        ("succeeded", "done", 2, 120.0)
    assert st["finished_at"] is not None
    assert s3.objects[ji.theory_key(JOB)] == b"(final)"
    assert [q["correct"] for q in qas_of(s3)["questions"]] == [True, False, True]


def test_failed_build_and_unfinished_sessions(rec):
    r, s3 = rec
    start_build(r)
    r.handle(dict(type="session_start", session=1, kind="build"))
    r.handle(dict(type="end", ok=False, theory=None, n_correct=0, n_required=2,
                  correct_questions=[], duration=5.0))
    st = state_of(s3)
    assert st["status"] == "failed" and st["sessions"][0]["status"] == "done"
    assert ji.theory_key(JOB) not in s3.objects


def test_session_end_without_its_start_is_still_recorded(rec):
    r, s3 = rec
    r.handle(dict(type="session_end", session=3, kind="feedback", credits=1.0,
                  valid=False, n_correct=0))
    assert state_of(s3)["sessions"][0]["session"] == 3


def test_flush_only_writes_when_changed(rec):
    r, s3 = rec
    start_build(r)
    puts = s3.puts
    r.flush()
    r.flush()
    assert s3.puts == puts


def test_unknown_events_are_ignored(rec):
    r, s3 = rec
    r.handle(dict(type="bogus"))
    r.handle(dict(type="phase", phase="no-such-phase"))
    r.close()
    assert state_of(s3)["phase"] == "sentences"


def test_put_errors_never_raise(rec):
    r, s3 = rec

    def boom(**_kwargs):
        raise RuntimeError("s3 down")

    s3.put_object = boom
    start_build(r)
    session(r, 1)
    r.close()


# --- read side -----------------------------------------------------------------
def test_cost_summary_counts_finished_sessions(rec):
    r, s3 = rec
    start_build(r)
    session(r, 1, credits=1.5)
    session(r, 2, kind="feedback", credits=None)
    r.handle(dict(type="session_start", session=3, kind="feedback"))
    cost = ji.cost_summary(state_of(s3), 0.04)
    assert cost["credits"] == 1.5 and cost["dollars"] == pytest.approx(0.06)
    assert cost["sessions"] == 2 and cost["unmetered_sessions"] == 1
    assert [x["session"] for x in cost["per_session"]] == [1, 2]
    assert ji.cost_summary(None, 0.04) is None


def test_load_missing_objects_return_none():
    s3 = FakeS3()
    assert ji.load_state(s3, "b", JOB) is None
    assert ji.load_theory(s3, "b", JOB) is None
    assert ji.load_qas(s3, "b", JOB) is None


def test_builder_stream():
    arn = "arn:aws:ecs:us-east-1:111111111111:task/Cluster/488fb12cbbe74d5c92ccc00a6f740b5f"
    assert ji.builder_stream(arn, "pcc-builder") == \
        "pcc-builder/builder/488fb12cbbe74d5c92ccc00a6f740b5f"


class FakeLogs:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        i = int(kwargs.get("nextToken", 0))
        resp = {"events": [{"message": m} for m in self.pages[i]]}
        if i + 1 < len(self.pages):
            resp["nextToken"] = str(i + 1)
        return resp


def test_build_logs_reads_the_whole_stream():
    logs = FakeLogs([["start", "kc: $ kiro-cli chat"], ["end"]])
    out = ji.build_logs(logs, "grp", "stream", 0, 1)
    assert out["lines"] == ["start", "kc: $ kiro-cli chat", "end"]
    assert out["truncated"] is False and out["omitted"] == 0 and out["total_lines"] == 3
    assert "filterPattern" not in logs.calls[0]
    assert logs.calls[0]["logStreamNames"] == ["stream"]


def test_build_logs_keeps_head_and_tail_with_marker():
    lines = [f"line {i}" for i in range(10)]
    out = ji.build_logs(FakeLogs([lines[:6], lines[6:]]), "g", "s", 0, 1, head=2, tail=3)
    assert out["lines"] == ["line 0", "line 1", ji.omitted_marker(5),
                            "line 7", "line 8", "line 9"]
    assert out["total_lines"] == 10 and out["omitted"] == 5 and out["truncated"] is True


def test_build_logs_keeps_kiro_milestones_from_the_omitted_middle():
    lines = (["gdsoq: generating"] * 3
             + ["gts: test set has 120 questions",
                "kc: $ kiro-cli chat --no-interactive --agent theory-formalizer",
                "kc: ● Running checker",
                "di: prompt_prefix noise"]
             + ["di: verify noise"] * 4)
    out = ji.build_logs(FakeLogs([lines]), "g", "s", 0, 1, head=1, tail=2)
    assert out["lines"] == [
        "gdsoq: generating",
        ji.omitted_marker(8, 2),
        "gts: test set has 120 questions",
        "kc: $ kiro-cli chat --no-interactive --agent theory-formalizer",
        ji.MILESTONES_END,
        "di: verify noise", "di: verify noise"]
    assert out["omitted"] == 8  # milestones are shown but still counted as omitted


def test_build_logs_stops_at_time_budget():
    ticks = iter([0.0, 0.0, ji.MAX_LOG_SECONDS + 1, 99, 99])
    logs = FakeLogs([["a"], ["b"], ["c"]])
    out = ji.build_logs(logs, "g", "s", 0, 1, clock=lambda: next(ticks))
    assert out["lines"] == ["a", "b"] and out["truncated"] is True
    assert len(logs.calls) == 2 and "time limit" in out["note"]


def test_build_logs_empty_has_note():
    out = ji.build_logs(FakeLogs([[]]), "g", "s", 0, 1)
    assert out["lines"] == [] and out["note"] == "No log lines for this build yet."


def test_download_streams_every_line_page_by_page():
    chunks = list(ji.iter_build_log_text(FakeLogs([["a", "b"], [], ["c"]]), "g", "s", 0, 1))
    assert chunks == ["a\nb\n", "c\n"]


# --- store helpers -------------------------------------------------------------
class FakeJobs:
    def __init__(self, items):
        self.items = items

    def get_item(self, Key):
        item = self.items.get(Key["id"])
        return {"Item": dict(item)} if item else {}


@pytest.fixture
def store_env(monkeypatch):
    import store
    s3 = FakeS3()
    jobs = FakeJobs({JOB: {"id": JOB, "status": "running", "created_at": 100,
                           "started_at": 110, "theory_name": "T",
                           "builder_task_arn": "arn:aws:ecs:r:1:task/C/abc123"}})
    monkeypatch.setattr(store, "ENABLED", True)
    monkeypatch.setattr(store, "BUCKET", "bucket")
    monkeypatch.setattr(store, "_s3", s3)
    monkeypatch.setattr(store, "_jobs", jobs)
    monkeypatch.setattr(store, "_reconcile_builder_jobs", lambda items: None)
    return store, s3


def test_store_introspection_view(store_env):
    store, s3 = store_env
    view = store.job_introspection(JOB)
    assert view["available"] is False and "No progress" in view["note"]
    assert store.job_introspection("nope") is None
    r = ji.Recorder(s3, "bucket", JOB, start_thread=False)
    start_build(r)
    session(r, 1)
    r.close()
    view = store.job_introspection(JOB)
    assert view["available"] and view["job"]["id"] == JOB
    assert view["theory"] == "(t1)" and view["state"]["n_questions"] == 3
    assert view["qas"]["questions"][0] == {"question": "Q1?", "expected_answer": "A1",
                                           "correct": True}
    assert view["cost"]["credits"] == 0.5


def test_store_build_logs_needs_builder_stream(store_env, monkeypatch):
    store, _ = store_env
    monkeypatch.setattr(store, "BUILDER_LOG_GROUP", "")
    assert store.build_logs(JOB)["source"] == "unavailable"
    monkeypatch.setattr(store, "BUILDER_LOG_GROUP", "builder-group")
    monkeypatch.setattr(store, "BUILDER_LOG_STREAM_PREFIX", "pcc-builder")
    fake = FakeLogs([["hello"]])
    monkeypatch.setattr(store, "_logs", lambda: fake)
    out = store.build_logs(JOB)
    assert out["lines"] == ["hello"] and out["total_lines"] == 1
    assert fake.calls[0]["logStreamNames"] == ["pcc-builder/builder/abc123"]
    assert fake.calls[0]["startTime"] == 105_000


def test_store_build_log_download(store_env, monkeypatch):
    store, _ = store_env
    monkeypatch.setattr(store, "BUILDER_LOG_GROUP", "builder-group")
    monkeypatch.setattr(store, "_logs", lambda: FakeLogs([["x"], ["y"]]))
    name, chunks = store.build_log_download(JOB)
    assert name == "t-build.log" and "".join(chunks) == "x\ny\n"
    monkeypatch.setattr(store, "BUILDER_LOG_GROUP", "")
    assert store.build_log_download(JOB)[0] is None


def test_progress_pill_follows_the_build(monkeypatch):
    import store
    seen = []
    monkeypatch.setattr(store, "_update_job", lambda jid, **f: seen.append(f["progress"]))
    monkeypatch.setattr(store, "_jlog", lambda jid, msg: None)
    on_event = store._progress_reporter(JOB, "T")
    for event in [dict(type="start", sentences=[]),
                  dict(type="phase", phase="test_set"),
                  dict(type="test_set", qs_and_as=QAS, n_required=2),
                  dict(type="phase", phase="theory"),
                  dict(type="session_start", session=1, kind="build"),
                  dict(type="session_end", session=1, valid=True, n_correct=2),
                  dict(type="session_start", session=2, kind="stall_resume"),
                  dict(type="session_end", session=2, valid=False, n_correct=0)]:
        on_event(event)
    assert seen == ["generating test set", "test set ready (3 questions, 2 needed)",
                    "building theory", "kiro-cli session 1 (first build)",
                    "session 1 verified 2 correct",
                    "kiro-cli session 2 (resumed after a stall)",
                    "session 2 verified 0 correct (theory not valid)"]


def test_dollars_per_credit_env_override(monkeypatch):
    monkeypatch.setenv("KIRO_DOLLARS_PER_CREDIT", "0.02")
    assert ji.dollars_per_credit() == 0.02
    monkeypatch.setenv("KIRO_DOLLARS_PER_CREDIT", "nonsense")
    assert ji.dollars_per_credit() == ji.DEFAULT_DOLLARS_PER_CREDIT
    monkeypatch.delenv("KIRO_DOLLARS_PER_CREDIT")
    assert ji.dollars_per_credit() == 0.04


def test_module_doctests():
    import doctest
    result = doctest.testmod(ji)
    assert result.attempted > 0 and result.failed == 0
