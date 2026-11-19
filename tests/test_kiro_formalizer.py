"""Tests for the kiro-cli theory engine (no kiro-cli, cvc5 or Bedrock needed)."""
import json
import re
from pathlib import Path

import pytest

import globals as g
from params import Params
from sentences import TextSentence
from utils import Sat, Statistics
import auto_formalizer as af
import kiro_formalizer as kf

QAS = [("Where does photosynthesis happen?", "In the chloroplasts."),
       ("What pigment absorbs light?", "Chlorophyll."),
       ("What gas is released?", "Oxygen."),
       ("What colour is chlorophyll?", "Green.")]
SENTENCES = [TextSentence(s, standalone=s) for s in
             ("Photosynthesis takes place in the chloroplasts.",
              "Chlorophyll is the green pigment that absorbs light.",
              "Photosynthesis releases oxygen.")]


@pytest.fixture(autouse=True)
def params(monkeypatch, tmp_path):
    g.p = Params()
    monkeypatch.setenv("CHATBOT_KIRO_WORKDIR", str(tmp_path / "work"))
    monkeypatch.delenv("CHATBOT_KIRO_KEEP_WORKDIR", raising=False)
    yield
    g.p = None


# --- sandbox config --------------------------------------------------------
def checker_regex(ws: Path) -> str:
    [pattern] = kf.agent_config(ws)["toolsSettings"]["execute_bash"]["allowedCommands"]
    return pattern


@pytest.mark.parametrize("args", [
    "check theory.smt2",
    "ask theory.smt2 fragments/q1.smt2",
    "ask theory.smt2 fragments/sub-dir/q_10.smt2",
    "ask theory.smt2 fragments/q1.smt2 fragments/q2.smt2 fragments/q3.smt2",
    "ask theory.smt2 " + " ".join(f"fragments/q{i}.smt2" for i in range(50)),
])
def test_checker_allowlist_accepts_workspace_calls(tmp_path, args):
    assert re.search(checker_regex(tmp_path), f"{kf.CHECKER_CMD} {args}")


@pytest.mark.parametrize("command", [
    f"{kf.CHECKER_CMD} check theory.smt2; ls /",
    f"{kf.CHECKER_CMD} check theory.smt2 && id",
    f"{kf.CHECKER_CMD} check theory.smt2 | sh",
    f"{kf.CHECKER_CMD} check $(id).smt2",
    f"{kf.CHECKER_CMD} check `id`.smt2",
    f"{kf.CHECKER_CMD} check ../../etc/passwd.smt2",
    f"{kf.CHECKER_CMD} check /etc/hosts.smt2",
    f"{kf.CHECKER_CMD} check theory.txt",
    f"{kf.CHECKER_CMD} check theory.smt2\nls /",
    f"{kf.CHECKER_CMD} ask theory.smt2",
    f"{kf.CHECKER_CMD} ask theory.smt2 " + " ".join(f"f{i}.smt2" for i in range(51)),
    f"{kf.CHECKER_CMD} ask theory.smt2 fragments/q1.smt2 /etc/passwd.smt2",
    f"{kf.CHECKER_CMD} ask theory.smt2 fragments/q1.smt2;id",
    f"CVC5_PATH=/bin/sh {kf.CHECKER_CMD} check theory.smt2",
    "python3 /tmp/evil.py check theory.smt2",
    "ls",
])
def test_checker_allowlist_rejects_everything_else(tmp_path, command):
    assert not re.search(checker_regex(tmp_path), command)


def test_agent_config_trusts_nothing_and_scopes_paths(tmp_path):
    cfg = kf.agent_config(tmp_path)
    ws = str(tmp_path.resolve())
    assert cfg["allowedTools"] == []
    assert cfg["mcpServers"] == {} and cfg["includeMcpJson"] is False
    for tool in ("fs_read", "fs_write"):
        assert cfg["toolsSettings"][tool]["allowedPaths"] == [ws, f"{ws}/**"]
    assert kf.CHECKER_CMD in cfg["prompt"]


def test_kiro_command_never_trusts_tools():
    g.p.kiro_model = "claude-sonnet-4.5"
    cmd = kf.kiro_command("hi", resume=True)
    assert "--no-interactive" in cmd and "--resume" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4.5"
    assert not any(a.startswith("--trust") or a == "-a" for a in cmd)


def test_kiro_env_strips_aws_credentials_and_isolates_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/x")
    monkeypatch.setenv("AWS_PROFILE", "someone")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("KIRO_API_KEY", "ksk_test")
    env = kf._kiro_env(tmp_path)
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_PROFILE"):
        assert var not in env
    assert env["AWS_REGION"] == "us-east-1"
    assert env["KIRO_API_KEY"] == "ksk_test"
    assert env["HOME"] == str(tmp_path / ".home")


def test_kiro_env_keeps_home_without_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY_SECRET_ID", raising=False)
    monkeypatch.setenv("HOME", "/home/dev")
    assert kf._kiro_env(tmp_path)["HOME"] == "/home/dev"


# --- workspace -------------------------------------------------------------
def test_workspace_layout(tmp_path):
    ws = kf._make_workspace(SENTENCES, QAS, 3)
    assert (ws / "sentences.txt").read_text().splitlines() == [s.standalone for s in SENTENCES]
    test_set = json.loads((ws / "test_set.json").read_text())
    assert [t["id"] for t in test_set] == ["q1", "q2", "q3", "q4"]
    assert test_set[2] == dict(id="q3", question=QAS[2][0], expected_answer=QAS[2][1])
    task = (ws / "TASK.md").read_text()
    assert "ALL 3 sentences" in task
    assert "at least 3 of the 4 questions (75%)" in task
    assert sorted(p.name for p in ws.iterdir()) == [
        ".kiro", "TASK.md", "fragments", "sentences.txt", "test_set.json"]
    assert (ws / ".kiro/agents/theory-formalizer.json").is_file()


# --- independent verification ----------------------------------------------
class FakeHarness:
    """Stands in for run_cvc5 / get_model_output_2_nat_lang / llm_as_judge."""

    def __init__(self, monkeypatch, theory_validity=Sat.SAT, fragment_validity=None,
                 model_answers=None, agree=None):
        self.theory_validity = theory_validity
        self.fragment_validity = fragment_validity or {}
        self.model_answers = model_answers or {}
        self.agree = agree if agree is not None else set(self.model_answers.values())
        self.judged = []
        monkeypatch.setattr(af, "run_cvc5", self.run_cvc5)
        monkeypatch.setattr(af, "get_model_output_2_nat_lang", self.to_nat_lang)
        monkeypatch.setattr(af, "llm_as_judge", self.judge)

    def run_cvc5(self, theory, question, stats, print_out_theories=True, timeout=None):
        assert timeout is not None, "verification must bound cvc5"
        if question is None:
            return dict(validity=self.theory_validity, get_model_output=None, stderr="")
        validity = self.fragment_validity.get(question.strip(), Sat.SAT)
        model = f"model-of {question.strip()}" if validity == Sat.SAT else None
        return dict(validity=validity, get_model_output=model, stderr="")

    def to_nat_lang(self, valid_theory, question, q_logical_form, get_model_output, stats):
        return dict(answer=self.model_answers.get(q_logical_form.strip(), "I don't know"))

    def judge(self, q, expected, answer, stats):
        self.judged.append(q)
        return dict(are_same=answer in self.agree)


def write_result(ws, answers, theory="(declare-const x Int)"):
    (ws / "theory.smt2").write_text(theory)
    entries = []
    for qid, frag, agent_answer, agent_correct in answers:
        (ws / "fragments" / f"{qid}.smt2").write_text(frag)
        entries.append(dict(id=qid, fragment=f"fragments/{qid}.smt2",
                            answer=agent_answer, correct=agent_correct))
    (ws / "answers.json").write_text(json.dumps(entries))


def test_verify_counts_only_model_grounded_judged_answers(monkeypatch):
    ws = kf._make_workspace(SENTENCES, QAS, 4)
    h = FakeHarness(monkeypatch,
                    fragment_validity={"f3": Sat.UNSAT},
                    model_answers={"f1": "In the chloroplasts", "f2": "Carotene", "f4": "Green"},
                    agree={"In the chloroplasts", "Green"})
    # The agent claims every answer is right; only q1 and q4 survive re-checking.
    write_result(ws, [("q1", "f1", "In the chloroplasts.", True),
                      ("q2", "f2", "Chlorophyll.", True),
                      ("q3", "f3", "Oxygen.", True),
                      ("q4", "f4", "Green.", True)])
    v = kf._verify(ws, QAS, 4, Statistics())
    assert v["valid"] is True
    assert v["correct"] == [QAS[0][0], QAS[3][0]]
    assert "q2: reading the model" in v["report"] and "'Carotene'" in v["report"]
    assert "q3: theory + fragments/q3.smt2 is Sat.UNSAT" in v["report"]
    assert "2 of the 4 questions verified correct; 4 are needed" in v["report"]
    assert sorted(h.judged) == sorted([QAS[0][0], QAS[1][0], QAS[3][0]])  # none for UNSAT


def test_verify_stops_once_enough_are_correct(monkeypatch):
    g.p.verify_pool_size = 1  # serial, so the cut-off point is deterministic
    ws = kf._make_workspace(SENTENCES, QAS, 2)
    h = FakeHarness(monkeypatch, model_answers={f"f{i}": "ok" for i in range(1, 5)})
    write_result(ws, [(f"q{i}", f"f{i}", "ok", True) for i in range(1, 5)])
    v = kf._verify(ws, QAS, 2, Statistics())
    assert len(v["correct"]) == 2 and len(h.judged) == 2


def test_verify_trusts_agent_flag_when_reverify_off(monkeypatch):
    g.p.kiro_reverify_answers = False
    ws = kf._make_workspace(SENTENCES, QAS, 4)
    h = FakeHarness(monkeypatch, fragment_validity={"f4": Sat.UNKNOWN})
    write_result(ws, [("q1", "f1", "a", True), ("q2", "f2", "b", False),
                      ("q3", "f3", "c", True), ("q4", "f4", "d", True)])
    v = kf._verify(ws, QAS, 4, Statistics())
    # cvc5 is still re-checked: q4's fragment is not SAT, so its claim is dropped.
    assert v["correct"] == [QAS[0][0], QAS[2][0]]
    assert h.judged == []


def test_verify_checks_questions_in_parallel(monkeypatch):
    import threading
    g.p.verify_pool_size = 4
    barrier = threading.Barrier(4, timeout=5)
    h = FakeHarness(monkeypatch, model_answers={f"f{i}": "ok" for i in range(1, 5)})
    judge = h.judge

    def judge_together(q, expected, answer, stats):
        barrier.wait()  # only passes if all four questions are in flight at once
        return judge(q, expected, answer, stats)

    monkeypatch.setattr(af, "llm_as_judge", judge_together)
    ws = kf._make_workspace(SENTENCES, QAS, 4)
    write_result(ws, [(f"q{i}", f"f{i}", "ok", True) for i in range(1, 5)])
    v = kf._verify(ws, QAS, 4, Statistics())
    assert v["correct"] == [q for q, _ in QAS]  # in test-set order


def test_verify_rejects_invalid_theory(monkeypatch):
    ws = kf._make_workspace(SENTENCES, QAS, 2)
    FakeHarness(monkeypatch, theory_validity=Sat.UNSAT)
    write_result(ws, [("q1", "f1", "a", True)])
    v = kf._verify(ws, QAS, 2, Statistics())
    assert v["valid"] is False and "not SAT" in v["report"]


def test_verify_reports_missing_outputs(monkeypatch):
    ws = kf._make_workspace(SENTENCES, QAS, 2)
    FakeHarness(monkeypatch)
    assert "theory.smt2 was not written" in kf._verify(ws, QAS, 2, Statistics())["report"]
    (ws / "theory.smt2").write_text("(declare-const x Int)")
    assert "answers.json was not written" in kf._verify(ws, QAS, 2, Statistics())["report"]
    (ws / "answers.json").write_text("{not json")
    assert "not a JSON list" in kf._verify(ws, QAS, 2, Statistics())["report"]


def test_verify_refuses_fragments_outside_workspace(monkeypatch, tmp_path):
    ws = kf._make_workspace(SENTENCES, QAS, 1)
    FakeHarness(monkeypatch, model_answers={"secret": "x"})
    (tmp_path / "outside.smt2").write_text("secret")
    (ws / "theory.smt2").write_text("(declare-const x Int)")
    (ws / "answers.json").write_text(json.dumps(
        [dict(id="q1", fragment=str(tmp_path / "outside.smt2"), answer="x", correct=True)]))
    v = kf._verify(ws, QAS, 1, Statistics())
    assert v["correct"] == [] and "outside the workspace" in v["report"]


# --- attempt loop ------------------------------------------------------------
def test_resumes_with_feedback_until_verified(monkeypatch):
    calls = []

    def fake_run(ws, message, resume, timeout, **_kw):
        calls.append((message, resume))
        if len(calls) == 1:   # first session forgets answers.json
            (ws / "theory.smt2").write_text("(declare-const x Int)")
        else:
            write_result(ws, [(f"q{i}", f"f{i}", "ok", True) for i in range(1, 5)])
        return 0, (0.5 if len(calls) == 1 else None)

    FakeHarness(monkeypatch, model_answers={f"f{i}": "ok" for i in range(1, 5)})
    monkeypatch.setattr(kf, "_run_kiro", fake_run)
    r = kf.kiro_create_valid_useful_theory(SENTENCES, QAS, 2, Statistics())
    assert r["theory"] == "(declare-const x Int)"
    assert len(r["correctly_answered_questions"]) >= 2  # parallel checks may verify more
    assert calls[0][1] is False and calls[1][1] is True
    assert "answers.json was not written" in calls[1][0]


def test_gives_up_after_max_attempts_and_cleans_up(monkeypatch, tmp_path):
    seen = []

    def fake_run(ws, message, resume, timeout, **_kw):
        seen.append(ws)
        return 1, 0.25

    FakeHarness(monkeypatch)
    monkeypatch.setattr(kf, "_run_kiro", fake_run)
    g.p.kiro_max_attempts = 2
    r = kf.kiro_create_valid_useful_theory(SENTENCES, QAS, 2, Statistics())
    assert r == dict(theory=None, n_correct_answers_reqd=2,
                     correctly_answered_questions=[])
    assert len(seen) == 2 and not seen[0].exists()


def test_clamps_required_count_like_ensure_usefulness(monkeypatch):
    FakeHarness(monkeypatch)
    monkeypatch.setattr(kf, "_run_kiro", lambda *a, **k: (1, None))
    g.p.kiro_max_attempts = 1
    r = kf.kiro_create_valid_useful_theory(SENTENCES, QAS, 10, Statistics())
    assert r["n_correct_answers_reqd"] == 2  # len(QAS) - 2


def test_session_events_follow_each_verification(monkeypatch):
    runs = iter([kf.STALLED_RC, 1, 0])

    def fake_run(ws, message, resume, timeout, **_kw):
        rc = next(runs)
        if rc == 0:
            write_result(ws, [(f"q{i}", f"f{i}", "ok", True) for i in range(1, 5)])
        return rc, 0.5

    FakeHarness(monkeypatch, model_answers={f"f{i}": "ok" for i in range(1, 5)})
    monkeypatch.setattr(kf, "_run_kiro", fake_run)
    events = []
    r = kf.kiro_create_valid_useful_theory(SENTENCES, QAS, 2, Statistics(),
                                           on_event=events.append)
    assert r["theory"] == "(declare-const x Int)"
    starts = [e for e in events if e["type"] == "session_start"]
    ends = [e for e in events if e["type"] == "session_end"]
    assert [(e["session"], e["kind"]) for e in starts] == [
        (1, "build"), (2, "stall_resume"), (3, "feedback")]
    assert [e["type"] for e in events] == ["session_start", "session_end"] * 3
    assert [e["stalled"] for e in ends] == [True, False, False]
    assert [e["valid"] for e in ends] == [False, False, True]
    assert ends[2]["n_correct"] >= 2 and ends[2]["theory"] == "(declare-const x Int)"
    assert all(e["credits"] == 0.5 and e["duration"] >= 0 for e in ends)


def test_model_defaults_from_environment(monkeypatch):
    monkeypatch.setenv("CHATBOT_KIRO_MODEL", "m1")
    assert Params().kiro_model == "m1"
    monkeypatch.delenv("CHATBOT_KIRO_MODEL")
    assert Params().kiro_model is None
    # Round-trips through the run file, and ignores keys from other versions.
    assert Params.from_dict(dict(Params(kiro_model="m2").to_dict(),
                                 theory_engine="agentic")).kiro_model == "m2"


# --- kiro-cli usage (stream-json) ---------------------------------------------
def test_unit_records_every_sessions_credits(monkeypatch):
    runs = iter([(1, 0.75), (1, None), (1, 0.5)])
    FakeHarness(monkeypatch)
    monkeypatch.setattr(kf, "_run_kiro", lambda *a, **k: next(runs))
    g.p.kiro_max_attempts = 3
    stats = Statistics()
    kf.kiro_create_valid_useful_theory(SENTENCES, QAS, 2, stats)
    assert (stats.kiro_credits, stats.n_kiro_sessions,
            stats.n_kiro_unmetered_sessions) == (1.25, 3, 1)
    merged = Statistics.combine([stats, stats])
    assert (merged.kiro_credits, merged.n_kiro_sessions) == (2.5, 6)


# Trimmed from a real kiro-cli 2.31.1 `--output-format stream-json` session.
STREAM = [
    {"type": "runStarted", "data": {"payloadSchema": "acp"}},
    {"type": "metadata", "data": {"sessionId": "s", "contextUsagePercentage": 4.0}},
    {"type": "sessionUpdate", "data": {"update": {
        "sessionUpdate": "tool_call", "title": "Reading note.txt:1", "kind": "read",
        "rawInput": {"operations": [{"mode": "Line", "path": "note.txt"}]}}}},
    {"type": "sessionUpdate", "data": {"update": {
        "sessionUpdate": "tool_call_update", "status": "completed",
        "rawOutput": {"items": [{"Text": "the secret word\nis banana"}]}}}},
    {"type": "sessionUpdate", "data": {"update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ban"}}}},
    {"type": "sessionUpdate", "data": {"update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ana\nok"}}}},
    {"type": "metadata", "data": {"turnDurationMs": 5319, "meteringUsage": [
        {"value": 0.04, "unit": "credit"}, {"value": 0.0387, "unit": "credit"},
        {"value": 0.032, "unit": "credit"}]}},
    {"type": "runFinished", "data": {"status": "success", "stopReason": "end_turn"}},
]


def test_transcript_renders_stream_json_and_sums_credits():
    t = kf._Transcript()
    out = []
    for event in STREAM:
        out += t.feed(json.dumps(event) + "\n")
    out += t.feed("Error: something kiro-cli printed\n")
    out += t.close()
    assert t.credits == pytest.approx(0.1107)
    assert out[0].startswith("● Reading note.txt:1 ")
    assert out[1:4] == ["  ↳ completed", "    the secret word", "    is banana"]
    assert out[4:6] == ["banana", "ok"]      # chunks joined, flushed before metadata
    assert out[6].startswith("[turn used 0.1107 credits over 3 request(s)")
    assert out[7] == "[run finished: success, end_turn]"
    assert out[8] == "Error: something kiro-cli printed"


def test_transcript_without_metering_reports_none():
    t = kf._Transcript()
    t.feed(json.dumps(STREAM[4]))
    assert t.credits is None and t.close() == ["ban"]


# --- stall watchdog -----------------------------------------------------------
def test_stalled_sessions_are_resumed_without_using_an_attempt(monkeypatch):
    calls = []

    def fake_run(ws, message, resume, timeout, idle_timeout=None):
        calls.append((message, resume, idle_timeout))
        if len(calls) <= 2:
            return kf.STALLED_RC, None          # two stalls in a row
        write_result(ws, [(f"q{i}", f"f{i}", "ok", True) for i in range(1, 5)])
        (ws / "theory.smt2").write_text("(declare-const x Int)")
        return 0, 0.5

    FakeHarness(monkeypatch, model_answers={f"f{i}": "ok" for i in range(1, 5)})
    monkeypatch.setattr(kf, "_run_kiro", fake_run)
    g.p.kiro_max_attempts = 1                    # stalls must not use it up
    g.p.kiro_idle_timeout_seconds = 300
    stats = Statistics()
    r = kf.kiro_create_valid_useful_theory(SENTENCES, QAS, 2, stats)
    assert r["theory"] == "(declare-const x Int)"
    assert [c[1] for c in calls] == [False, True, True]
    assert all(c[2] == 300 for c in calls)
    assert "produced no output for 5 minutes" in calls[1][0]
    assert stats.n_kiro_sessions == 3 and stats.n_kiro_unmetered_sessions == 2


def test_stall_resumes_are_capped(monkeypatch):
    calls = []

    def fake_run(*a, **k):
        calls.append(1)
        return kf.STALLED_RC, None

    FakeHarness(monkeypatch)
    monkeypatch.setattr(kf, "_run_kiro", fake_run)
    g.p.kiro_max_attempts = 2
    g.p.kiro_max_stall_resumes = 1
    r = kf.kiro_create_valid_useful_theory(SENTENCES, QAS, 2, Statistics())
    assert r["theory"] is None
    # attempt 1 stalls, resumes once, stalls again (counts), attempt 2 stalls.
    assert len(calls) == 3
