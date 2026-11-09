# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Persistence + build runner for the multi-theory chatbot backend.

Data model
----------
Theories and jobs are indexed in DynamoDB; the heavy artifacts (the pasted/
uploaded source document and the computed theory YAML) live in S3.

  DynamoDB `theories` table item:
    id            (str, PK)   uuid
    name          (str)       display name
    status        (str)       building | ready | failed | cancelled
    source_s3_key (str)       documents/{id}.txt
    theory_s3_key (str|None)  theories/{id}/theory.yaml  (set when ready)
    subject_id    (str)       in-process subject id used for the run
    sentence_count(int)
    job_id        (str|None)
    created_at    (int)       epoch seconds

  DynamoDB `jobs` table item:
    id          (str, PK)   uuid
    theory_id   (str)
    theory_name (str)
    status      (str)       queued | running | finished | failed | cancelled
    progress    (str)       human-readable, e.g. "building theory"
    log_tail    (str)       last N log lines
    error       (str|None)
    created_at  (int)
    started_at  (int|None)
    finished_at (int|None)

The runner is INLINE (a background thread in the web task). It is intentionally
bounded so a run finishes in a tolerable time on the 2 vCPU web task; Phase 2
replaces `run_job` with a Fargate RunTask dispatch without changing this
module's public surface or the data model.
"""
from __future__ import annotations

import os
import re
import time
import uuid
import threading
import traceback
from typing import Optional

import json

import boto3

import build_params as _build_params
import job_introspection as _introspection

# --- Config (container env, set by the CDK stack) -------------------------
REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("THEORY_STORE_BUCKET", "")
THEORIES_TABLE = os.environ.get("THEORIES_TABLE", "")
JOBS_TABLE = os.environ.get("JOBS_TABLE", "")

# CloudWatch log group this task logs to (CDK sets SERVICE_LOG_GROUP from the
# ECS awslogs driver's log group). The inline job runner shares this task's
# process, so a job's real pipeline stdout lands in this group's stream for the
# running task -- that is what the full-log endpoint tails. Empty => the
# endpoint falls back to the job's stored log_tail status line.
LOG_GROUP = os.environ.get("SERVICE_LOG_GROUP", "")
# The builder tasks' log group and awslogs stream prefix (set on the WEB
# task). The Job Details tab reads a build's log from its builder stream.
BUILDER_LOG_GROUP = os.environ.get("BUILDER_LOG_GROUP", "")
BUILDER_LOG_STREAM_PREFIX = os.environ.get("BUILDER_LOG_STREAM_PREFIX", "builder")

# Inline-runner bounds: keep a run short enough for the web task. These are the
# knobs the Fargate builder relaxes. A pasted doc longer than this is truncated
# for an inline run (the full doc is still stored in S3). They apply ONLY on the
# legacy inline path; a build dispatched to a dedicated Fargate builder task
# (BUILDER_MODE=1) runs the FULL, uncapped pipeline -- see run_job / the ingest
# helpers, which treat MAX_SENTENCES_INLINE as unbounded under BUILDER_MODE.
MAX_SENTENCES_INLINE = int(os.environ.get("MAX_SENTENCES_INLINE", "12"))

# --- Fargate builder dispatch (Phase 2) -----------------------------------
# Every theory build is farmed out to its OWN ephemeral Fargate task running
# cdk/docker/build_task.py (the full, uncapped pipeline), instead of a daemon
# thread in this web task. The CDK stack sets these on the WEB task so it can
# launch the builder task-def into the right network place.
#   BUILDER_TASK_DEF   builder FargateTaskDefinition family[:revision] or ARN
#   BUILDER_CLUSTER    ECS cluster name/ARN to run the task in
#   BUILDER_SUBNETS    comma-separated private subnet ids (awsvpc placement)
#   BUILDER_SECURITY_GROUPS  comma-separated security group ids
#   BUILDER_CONTAINER_NAME   container name in the builder task-def to override
# BUILDER_MODE is set only on the builder task itself (by the task-def), telling
# run_job it is the dedicated builder and may run unbounded.
BUILDER_TASK_DEF = os.environ.get("BUILDER_TASK_DEF", "")
BUILDER_CLUSTER = os.environ.get("BUILDER_CLUSTER", "")
BUILDER_SUBNETS = [s for s in os.environ.get("BUILDER_SUBNETS", "").split(",")
                   if s.strip()]
BUILDER_SECURITY_GROUPS = [s for s in
                           os.environ.get("BUILDER_SECURITY_GROUPS", "").split(",")
                           if s.strip()]
BUILDER_CONTAINER_NAME = os.environ.get("BUILDER_CONTAINER_NAME", "builder")
# True inside a dedicated builder task -> run the full, uncapped pipeline.
BUILDER_MODE = os.environ.get("BUILDER_MODE", "") == "1"
# Dispatch is possible only when the web task has a builder task-def + network
# placement configured. When unset, create_theory FAILS the build (there is no
# inline fallback -- see create_theory): a build must always run on its own
# Fargate task, never inline in the web task.
DISPATCH_ENABLED = bool(BUILDER_TASK_DEF and BUILDER_CLUSTER and BUILDER_SUBNETS)

ENABLED = bool(BUCKET and THEORIES_TABLE and JOBS_TABLE)

_s3 = boto3.client("s3", region_name=REGION) if ENABLED else None
_ddb = boto3.resource("dynamodb", region_name=REGION) if ENABLED else None
_theories = _ddb.Table(THEORIES_TABLE) if ENABLED else None
_jobs = _ddb.Table(JOBS_TABLE) if ENABLED else None

# Lazily created ECS client (web task only, when dispatching/killing a build).
_ecs_client = None


def _ecs():
    global _ecs_client
    if _ecs_client is None:
        _ecs_client = boto3.client("ecs", region_name=REGION)
    return _ecs_client

# Lazily created CloudWatch Logs client (only when a log tail is requested).
_logs_client = None


def _logs():
    global _logs_client
    if _logs_client is None:
        _logs_client = boto3.client("logs", region_name=REGION)
    return _logs_client


def _now() -> int:
    return int(time.time())


# --- Job cancellation (kill a queued/running job) -------------------------
# An inline build is a daemon thread in THIS web-task process. A kill request
# arrives as an HTTP call handled in the same process, so an in-process
# registry is all we need to coordinate. (A build on a Fargate builder task is
# stopped with ecs:StopTask instead; see kill_job.)
#
# For each live job we register a threading.Event. kill_job() sets it; the
# runner polls is_job_cancelled() at every phase boundary and raises
# JobCancelled, and the kiro-cli watchdog (kiro_formalizer._run_kiro) polls the
# same flag, so a kill also ends a running kiro-cli session. A queued job
# (thread not yet past its first checkpoint) is caught at the first poll, so it
# stops before doing any real work.
_cancel_lock = threading.Lock()
_cancel_events: dict = {}  # job_id -> threading.Event


class JobCancelled(Exception):
    """Raised inside run_job when a kill has been requested for this job."""


def _register_job_cancel(job_id: str) -> "threading.Event":
    ev = threading.Event()
    with _cancel_lock:
        _cancel_events[job_id] = ev
    return ev


def _clear_job_cancel(job_id: str) -> None:
    with _cancel_lock:
        _cancel_events.pop(job_id, None)


def is_job_cancelled(job_id: str) -> bool:
    with _cancel_lock:
        ev = _cancel_events.get(job_id)
    return bool(ev and ev.is_set())


def _raise_if_cancelled(job_id: str) -> None:
    if is_job_cancelled(job_id):
        raise JobCancelled(job_id)


def kill_job(job_id: str) -> dict:
    """
    Request cancellation of a queued or running job.

    For a build dispatched to a Fargate builder task (Phase 2), stops that task
    via ECS stop_task (the recorded builder_task_arn). For a legacy inline build
    running in THIS process, sets the job's in-process cancel Event so the runner
    thread and its worker pool stop at their next checkpoint. In both cases it
    flips the DynamoDB job record to `cancelled` immediately so the UI reflects
    it without waiting for the task/thread to wind down. Returns the refreshed
    job view, or a `{status: ...}` note when
    the job is unknown or already finished.

    Idempotent: killing an already-finished/failed/cancelled job is a no-op that
    returns its current state.
    """
    job = get_job(job_id)
    if job is None:
        return {"ok": False, "note": "job not found"}
    if job["status"] in ("finished", "failed", "cancelled"):
        return {"ok": False, "note": f"job already {job['status']}", "job": job}

    # Builder tasks (Phase 2) run in their OWN Fargate task, not a thread in
    # this process. Stop that task so the build actually halts; the stopped
    # task's _run_job_safe sees the pipeline torn down and records cancellation.
    # (We also mark the record cancelled below so the UI reflects it at once.)
    # The ARN is on the raw record; the job view (get_job) leaves it out.
    raw = _jobs.get_item(Key={"id": job_id}).get("Item") or {}
    builder_arn = raw.get("builder_task_arn")
    if builder_arn:
        try:
            _ecs().stop_task(cluster=BUILDER_CLUSTER, task=builder_arn,
                             reason="killed by user via the chatbot UI")
            _jlog(job_id, f"KILL requested; stopping builder task {builder_arn}")
        except Exception as ex:  # pylint: disable=broad-except
            # The task may already be gone (finished/stopped); still mark the
            # record cancelled below so the UI is correct.
            _jlog(job_id, f"KILL requested; stop_task best-effort failed: {ex}")

    with _cancel_lock:
        ev = _cancel_events.get(job_id)
        if ev is None:
            # The job has no in-process cancel Event -- it is a dispatched
            # builder-task job (never had one in THIS process), or an inline
            # thread in a DIFFERENT task process (e.g. after a redeploy), which
            # this process cannot signal. Still mark it cancelled so the record
            # and UI are correct; a builder task was stopped above, and an
            # orphaned inline daemon thread dies with its task.
            ev = threading.Event()
            _cancel_events[job_id] = ev
        ev.set()

    if not builder_arn:
        _jlog(job_id, "KILL requested by user; cancelling")
    _update_job(job_id, status="cancelled", progress="cancelled",
                finished_at=_now(),
                log_tail=_tail(f"{job.get('theory_name') or job_id}: "
                               "cancelled by user"))
    # If the theory was still building, mark it cancelled (not failed: the
    # user stopped it) so it leaves "building" and can be rebuilt or deleted.
    tid = job.get("theory_id")
    if tid:
        item = _get_theory_item(tid)
        if item and item.get("status") == "building":
            _update_theory(tid, status="cancelled")
    return {"ok": True, "job": get_job(job_id)}


# --- S3 helpers -----------------------------------------------------------
def _put_text(key: str, text: str) -> None:
    _s3.put_object(Bucket=BUCKET, Key=key, Body=text.encode("utf-8"))


def _get_text(key: str) -> str:
    obj = _s3.get_object(Bucket=BUCKET, Key=key)
    return obj["Body"].read().decode("utf-8")


# --- Theory CRUD ----------------------------------------------------------
MAX_THEORY_NAME_LENGTH = 200


class TheoryNameTaken(ValueError):
    """The requested theory name is already used by another theory."""


def check_theory_name(name: str, exclude_id: Optional[str] = None) -> str:
    """
    Return `name` stripped, or raise when it cannot be a theory's name.

    Theory names are unique, compared case-insensitively after trimming, so
    every chat tab and theory row is unambiguous. `exclude_id` skips one theory
    (the one being renamed). Raises ValueError for an empty or overlong name
    and TheoryNameTaken (a ValueError) when another theory already has it.
    """
    clean = (name or "").strip()
    if not clean:
        raise ValueError("A theory name is required.")
    if len(clean) > MAX_THEORY_NAME_LENGTH:
        raise ValueError(
            f"A theory name can be at most {MAX_THEORY_NAME_LENGTH} characters.")
    if ENABLED:
        wanted = clean.casefold()
        for item in _theories.scan().get("Items", []):
            if item.get("id") == exclude_id:
                continue
            other = str(item.get("name") or "")
            if other.strip().casefold() == wanted:
                raise TheoryNameTaken(
                    f'A theory named "{other}" already exists. '
                    "Choose a different name.")
    return clean


def rename_theory(theory_id: str, name: str) -> Optional[dict]:
    """
    Rename a theory, and the theory name recorded on its jobs. Returns the
    theory's summary, or None when the id is unknown. Raises like
    check_theory_name for an empty, overlong or taken name.
    """
    item = _get_theory_item(theory_id)
    if not item:
        return None
    clean = check_theory_name(name, exclude_id=theory_id)
    _update_theory(theory_id, name=clean)
    for job in _jobs.scan().get("Items", []):
        if job.get("theory_id") == theory_id and job.get("theory_name") != clean:
            _jobs.update_item(
                Key={"id": job["id"]},
                UpdateExpression="SET #n = :n",
                ExpressionAttributeNames={"#n": "theory_name"},
                ExpressionAttributeValues={":n": clean},
            )
    item["name"] = clean
    return _theory_summary(item)


def list_theories() -> list[dict]:
    """All theories, newest first. Safe (returns []) when the store is off."""
    if not ENABLED:
        return []
    items = _theories.scan().get("Items", [])
    # A "building" theory whose builder task died would stay building forever;
    # reconcile its job so the Theories tab shows the real state.
    building = [i for i in items if i.get("status") == "building" and i.get("job_id")]
    if building:
        jobs = [j for j in (_jobs.get_item(Key={"id": i["job_id"]}).get("Item")
                            for i in building) if j]
        _reconcile_builder_jobs(jobs)
        failed = {j.get("theory_id") for j in jobs if j.get("status") == "failed"}
        for i in building:
            if i["id"] in failed:
                fresh = _get_theory_item(i["id"])
                if fresh:
                    i.update(fresh)
    items.sort(key=lambda i: int(i.get("created_at", 0)), reverse=True)
    return [_theory_summary(i) for i in items]


def _theory_summary(item: dict) -> dict:
    return {
        "id": item["id"],
        "name": item.get("name", item["id"]),
        "status": item.get("status", "ready"),
        "sentence_count": int(item.get("sentence_count", 0)),
        "created_at": int(item.get("created_at", 0)),
        "job_id": item.get("job_id"),
        "doc_type": item.get("doc_type", "text"),
        "origin": item.get("origin") or None,
    }


def _suggested_qas(questions: list, stored: dict, job_id: Optional[str]) -> list:
    """
    Each suggested question with its answer, for the theory's Q&A view:
    [{"question", "answer", "source"}]. A test-set question's answer is the
    expected answer the theory was verified against ("test set"); a variation's
    is the answer the chatbot proved when it was validated ("variation").
    Theories built before answers were stored fall back to the build's
    recorded test set (job introspection); a question with no known answer
    gets answer None.
    """
    expected = {qa[0]: qa[1] for qa in stored.get("verified_qs_and_as") or []
                if isinstance(qa, (list, tuple)) and len(qa) >= 2}
    if not expected and job_id and ENABLED:
        try:
            recorded = _introspection.load_qas(_s3, BUCKET, job_id) or {}
            expected = {q["question"]: q["expected_answer"]
                        for q in recorded.get("questions") or []
                        if q.get("question")}
        except Exception:  # pylint: disable=broad-except
            expected = {}
    variations = stored.get("variation_answers") or {}
    out = []
    for q in questions:
        if not isinstance(q, str):
            continue
        if q in expected:
            out.append({"question": q, "answer": expected[q], "source": "test set"})
        else:
            out.append({"question": q, "answer": variations.get(q),
                        "source": "variation"})
    return out


def get_theory(theory_id: str) -> Optional[dict]:
    """Full theory incl. source document + computed theory text (tab 2)."""
    if not ENABLED:
        return None
    item = _theories.get_item(Key={"id": theory_id}).get("Item")
    if not item:
        return None
    out = _theory_summary(item)
    out["source_document"] = (
        _get_text(item["source_s3_key"]) if item.get("source_s3_key") else ""
    )
    # The stored artifact (theories/{id}/theory.yaml) is a YAML document
    # holding the SMT-LIB `theory` string, the `suggested_questions` Q&A set,
    # and more. Expose three views of it: the raw YAML, the extracted SMT-LIB
    # theory, and the suggested questions. Parsing is best-effort -- if the YAML
    # is missing or unparseable the raw text still falls through to the SMT-LIB
    # view so nothing regresses.
    raw_yaml = (
        _get_text(item["theory_s3_key"]) if item.get("theory_s3_key") else ""
    )
    out["computed_theory_yaml"] = raw_yaml
    smt_lib = raw_yaml
    suggested: list = []
    parsed = None
    if raw_yaml:
        try:
            import yaml as _yaml  # module has no top-level yaml import
            parsed = _yaml.safe_load(raw_yaml)
            if isinstance(parsed, dict):
                smt_lib = (
                    parsed.get("theory")
                    or parsed.get("valid_theory")
                    or raw_yaml
                )
                sq = parsed.get("suggested_questions") or []
                if isinstance(sq, list):
                    suggested = sq
        except Exception:  # pylint: disable=broad-except
            parsed = None
    out["computed_theory"] = smt_lib
    out["suggested_questions"] = suggested
    out["suggested_qas"] = _suggested_qas(
        suggested, parsed if isinstance(parsed, dict) else {}, item.get("job_id"))
    out["subject_id"] = item.get("subject_id")
    # The individual sentences the document was split into (one per line in the
    # UI's sentence-list modal). FHIR theories are not sentence-split, so this
    # is only meaningful for text/wiki sources; it is [] for FHIR.
    #
    # `sentences` is the ORIGINAL (contextual) sentence text; `standalone_
    # sentences` is the DECONTEXTUALIZED version of each, index-aligned 1:1 so
    # the UI can show them side by side (original left, decontextualized
    # right). The decontextualized text resolves pronouns/references ("It is
    # ..." -> "New Zealand is ...") and is produced by the formalizer's
    # make_sentence_standalone step, persisted per subject in
    # sentences/{id}-standalone.txt.
    if item.get("doc_type") == DOC_TYPE_FHIR:
        # FHIR theories are not sentence-split from a prose source document and
        # have no persisted standalone .txt files; their sentences are the JSON
        # chunks (one FHIRsentence per non-Patient entry), captured in the
        # theory artifact. We read them back out of the stored
        # computed-theory YAML (see _fhir_sentence_pairs).
        original, standalone = _fhir_sentence_pairs(out["computed_theory"])
        out["sentences"] = original
        out["standalone_sentences"] = standalone
    else:
        original, standalone = _theory_sentence_pairs(
            item.get("subject_id"), out["source_document"])
        out["sentences"] = original
        out["standalone_sentences"] = standalone
    return out


def _theory_sentence_pairs(subject_id, source_document: str):
    """
    Return (original, standalone) sentence lists, index-aligned 1:1.

    Ground truth for a subject that ships paired sentence files is
    sentences/{id}.txt (original) + sentences/{id}-standalone.txt
    (decontextualized) -- these are written in lockstep by the formalizer, so
    line N of one corresponds to line N of the other. We use that pair when the
    files exist AND have equal line counts (the only case where index alignment
    is sound).

    Otherwise we fall back to splitting the stored source document and return
    it as BOTH lists (original == decontextualized), which is honest for
    theories whose sentences carry no distinct standalone rewrite (e.g. pasted
    text whose lines were already standalone-ish). Never raises -- any file
    error degrades to the source-split fallback.
    """
    fallback = _split_sentences(source_document or "")
    if not subject_id:
        return fallback, list(fallback)
    try:
        from pathlib import Path  # module has no top-level pathlib import
        import globals as g  # deferred: avoids a hard import at module load
        sent_dir = Path(g.SENTENCES_DIR)
    except Exception:  # pylint: disable=broad-except
        return fallback, list(fallback)

    # Tolerate hyphen/underscore skew between the stored subject id and the
    # canonical filename (e.g. "new-zealand" vs "new_zealand").
    candidates = [subject_id, subject_id.replace("-", "_"),
                  subject_id.replace("_", "-")]
    for sid in candidates:
        raw_path = sent_dir / f"{sid}.txt"
        std_path = sent_dir / f"{sid}-standalone.txt"
        if not (raw_path.is_file() and std_path.is_file()):
            continue
        try:
            original = [ln.strip() for ln in
                        raw_path.read_text(encoding="utf-8").splitlines()
                        if ln.strip()]
            standalone = [ln.strip() for ln in
                          std_path.read_text(encoding="utf-8").splitlines()
                          if ln.strip()]
        except Exception:  # pylint: disable=broad-except
            continue
        if original and len(original) == len(standalone):
            return original, standalone
    return fallback, list(fallback)


def _fhir_sentence_pairs(computed_theory_yaml: str):
    """
    Return (original, standalone) sentence lists for a FHIR theory, read from
    the stored computed-theory YAML.

    FHIR builds don't write paired sentences/{id}.txt files -- the raw Bundle
    goes to {id}.json and there is no persisted standalone (FHIR
    decontextualization is per-chunk at formalization time). The sentences the
    build actually used are captured in the theory artifact's `sentences`
    list, which run_job sanitizes to plain dicts via _to_plain() before storing.

    After that sanitization each sentence is a dict of the Sentence object's
    attributes:
      * FHIRsentence -> {"_original": <JSON chunk str>, "_patient_entries": ...}
      * TextSentence -> {"_original": <text>, "_standalone": <text>, ...}
    (a bare string is also tolerated, for older artifacts). We surface
    `_original` as the original column and `_standalone` as the decontextualized
    column, falling back to the original when no standalone was persisted (the
    FHIR case), so the UI's two-column table renders JSON chunk == both sides.

    Never raises -- any parse problem degrades to empty lists.
    """
    if not computed_theory_yaml:
        return [], []
    try:
        import yaml as _yaml  # module has no top-level yaml import
        loaded = _yaml.safe_load(computed_theory_yaml)
    except Exception:  # pylint: disable=broad-except
        return [], []
    if not isinstance(loaded, dict):
        return [], []
    raw = loaded.get("sentences") or []
    if not isinstance(raw, list):
        return [], []
    original: list = []
    standalone: list = []
    for s in raw:
        if isinstance(s, str):
            text = s.strip()
            if not text:
                continue
            original.append(text)
            standalone.append(text)
        elif isinstance(s, dict):
            orig = s.get("_original", s.get("original"))
            if orig is None:
                continue
            orig = str(orig).strip()
            if not orig:
                continue
            std = s.get("_standalone", s.get("standalone"))
            std = str(std).strip() if std is not None and str(std).strip() else orig
            original.append(orig)
            standalone.append(std)
    return original, standalone


def _get_theory_item(theory_id: str) -> Optional[dict]:
    if not ENABLED:
        return None
    return _theories.get_item(Key={"id": theory_id}).get("Item")


def theory_s3_key(theory_id: str) -> Optional[str]:
    item = _get_theory_item(theory_id)
    return item.get("theory_s3_key") if item else None


def delete_theory(theory_id: str) -> dict:
    """
    Delete a theory: remove its DynamoDB item (the user-visible removal) and
    best-effort delete its S3 artifacts (source document + computed theory
    YAML). Returns {"ok": True, "deleted": <id>} on success.

    Unknown id -> {"ok": False, "note": "theory not found"}. Idempotent-ish:
    a second delete of the same id returns "theory not found".

    S3 object deletion is best-effort: if a key is missing or the delete fails,
    the DynamoDB item is still removed so the theory disappears from the UI; any
    orphaned S3 object is harmless.
    """
    if not ENABLED:
        return {"ok": False, "note": "theory store not configured"}
    item = _get_theory_item(theory_id)
    if not item:
        return {"ok": False, "note": "theory not found"}

    # Best-effort S3 cleanup of the heavy artifacts. The DynamoDB delete below
    # is the authoritative, user-visible removal; a failed/missing S3 delete
    # only leaves a harmless orphan.
    for key in (item.get("source_s3_key"), item.get("theory_s3_key")):
        if not key:
            continue
        try:
            _s3.delete_object(Bucket=BUCKET, Key=key)
        except Exception:  # pylint: disable=broad-except
            pass

    _theories.delete_item(Key={"id": theory_id})
    return {"ok": True, "deleted": theory_id}


def rebuild_theory(theory_id: str, build_params: Optional[dict] = None) -> dict:
    """
    Re-run the build for an existing theory from its STORED source document,
    IN PLACE: the same theory record (same id and name) is reset to "building"
    and a fresh job is dispatched that writes its result back to that record.

    This goes through create_theory (the single, well-tested dispatch path, so a
    rebuild picks up every pipeline fix an original build gets) with
    rebuild_of=<id>, which tells it to reuse the existing record instead of
    creating a new row.

    Carries over the original name, subject_id, resolved doc_type, and origin.
    Reads the source from source_s3_key.

    Returns create_theory's {theory, job} on success, or {"ok": False, "note":...}
    when the store is off, the id is unknown, or the source document is missing
    (e.g. a theory that failed before its source was persisted).
    """
    if not ENABLED:
        return {"ok": False, "note": "theory store not configured"}
    item = _get_theory_item(theory_id)
    if not item:
        return {"ok": False, "note": "theory not found"}
    src_key = item.get("source_s3_key")
    if not src_key:
        return {"ok": False, "note": "theory has no stored source to rebuild"}
    try:
        source_text = _get_text(src_key)
    except Exception as ex:  # pylint: disable=broad-except
        return {"ok": False, "note": f"could not read source document: {ex}"}

    return create_theory(
        name=item.get("name") or "Rebuilt theory",
        source_text=source_text,
        # The stored doc_type is already resolved ("text"/"fhir"); pass it
        # through so create_theory doesn't re-sniff (and re-sniffing a stored
        # FHIR bundle would still land on fhir anyway).
        doc_type=item.get("doc_type") or "auto",
        origin=item.get("origin") or None,
        # Rebuild IN PLACE: reuse this same theory record (same id/name). The
        # existing row is reset to building and the result is written back.
        rebuild_of=theory_id,
        build_params=build_params,
    )


# --- Job CRUD -------------------------------------------------------------
def list_jobs() -> list[dict]:
    if not ENABLED:
        return []
    items = _jobs.scan().get("Items", [])
    _reconcile_builder_jobs(items)
    items.sort(key=lambda i: int(i.get("created_at", 0)), reverse=True)
    return [_job_view(i) for i in items]


def get_job(job_id: str) -> Optional[dict]:
    if not ENABLED:
        return None
    item = _jobs.get_item(Key={"id": job_id}).get("Item")
    if item:
        _reconcile_builder_jobs([item])
    return _job_view(item) if item else None


# Marker printed around an inline run so its CloudWatch lines can be told apart
# from other requests sharing this task's single log stream. run_job emits the
# BEGIN line at start and the END line (via _run_job_safe / run_job) at finish.
def _job_marker(job_id: str) -> str:
    return f"[job {job_id}]"


def _jlog(job_id: str, message: str) -> None:
    """
    Print a job-tagged line to the task's stdout (-> CloudWatch). Every line a
    job emits is prefixed with its `[job <id>]` marker so job_logs can pull this
    job's lines out of the web task's single shared log stream even when several
    inline jobs (daemon threads in the same process) run concurrently. The same
    text is kept as the job's log_tail status line for the no-CloudWatch path.
    """
    line = f"{_job_marker(job_id)} {message}"
    print(line, flush=True)


def job_logs(job_id: str, limit: int = 2000) -> dict:
    """
    Full log for a job, tailed from CloudWatch.

    The inline runner shares the web task's process, so a job's real pipeline
    stdout is interleaved into this task's single CloudWatch stream. We fetch
    the group's events over the job's lifetime window and keep the lines
    carrying this job's marker (`[job <id>]`), which run_job prefixes onto every
    line it prints. Returns:

      {
        "source": "cloudwatch" | "log_tail" | "unavailable",
        "lines":  [ "...", ... ],   # newest-last, already filtered to this job
        "truncated": bool,          # True if `limit` capped the result
        "note":   str | None,       # human-readable explanation when degraded
      }

    Falls back to the job's stored `log_tail` status line when the log group is
    not configured, the job never started, or CloudWatch is unreachable, so the
    endpoint always returns something useful.
    """
    job = get_job(job_id)
    if job is None:
        return {"source": "unavailable", "lines": [], "truncated": False,
                "note": "job not found"}

    def _fallback(note: str) -> dict:
        tail = (job.get("log_tail") or "").rstrip("\n")
        return {"source": "log_tail",
                "lines": tail.split("\n") if tail else [],
                "truncated": False, "note": note}

    if not LOG_GROUP:
        return _fallback("CloudWatch log group not configured; "
                         "showing the stored status line.")
    if not job.get("started_at"):
        return _fallback("Job has not started yet.")

    # Window: from the job's start (minus a small skew margin) to its finish
    # (plus margin) or now for a still-running job. CloudWatch wants epoch ms.
    start_ms = (int(job["started_at"]) - 5) * 1000
    end = job.get("finished_at")
    end_ms = ((int(end) + 10) if end else _now() + 1) * 1000
    marker = _job_marker(job_id)

    try:
        lines: list[str] = []
        truncated = False
        token = None
        # Push the job-marker filter SERVER-SIDE via filterPattern. CloudWatch
        # matches a quoted pattern as a literal substring and returns only this
        # job's lines, instead of us paging every interleaved event in the
        # window (10-way fan-out jobs emit tens of thousands) and discarding
        # most in Python -- that many sequential round-trips is what pushed the
        # endpoint past CloudFront's origin timeout (504). The `marker in msg`
        # check below stays as a belt-and-suspenders guard.
        filter_pattern = f'"{marker}"'
        while True:
            kwargs = dict(logGroupName=LOG_GROUP, startTime=start_ms,
                          endTime=end_ms, limit=1000,
                          filterPattern=filter_pattern)
            if token:
                kwargs["nextToken"] = token
            resp = _logs().filter_log_events(**kwargs)
            for ev in resp.get("events", []):
                msg = ev.get("message", "").rstrip("\n")
                if marker in msg:
                    lines.append(msg)
                    if len(lines) >= limit:
                        truncated = True
                        break
            if truncated:
                break
            token = resp.get("nextToken")
            if not token:
                break
        if not lines:
            # Nothing matched the marker yet (e.g. a job that failed before
            # printing, or logs not yet flushed). Degrade to the status line.
            return _fallback("No CloudWatch lines for this job yet; "
                             "showing the stored status line.")
        return {"source": "cloudwatch", "lines": lines,
                "truncated": truncated, "note": None}
    except Exception as ex:  # pylint: disable=broad-except
        return _fallback(f"Could not read CloudWatch logs ({ex}); "
                         "showing the stored status line.")


# --- Job introspection (the build's progress, theory and test set) --------
def job_introspection(job_id: str) -> Optional[dict]:
    """
    The progress recorded for a build (job_introspection.Recorder): its
    phases, kiro-cli sessions and cost, the latest verified theory and the
    test set. None when the job id is unknown. `available` is False for jobs
    that failed before recording started.
    """
    job = get_job(job_id)
    if job is None:
        return None
    state, theory, qas, note = None, None, None, None
    if ENABLED:
        try:
            state = _introspection.load_state(_s3, BUCKET, job_id)
            if state is not None:
                theory = _introspection.load_theory(_s3, BUCKET, job_id)
                qas = _introspection.load_qas(_s3, BUCKET, job_id)
        except Exception as ex:  # pylint: disable=broad-except
            note = f"Could not read the recorded progress ({ex})."
    if state is None and note is None:
        note = "No progress was recorded for this job (it failed before the build started)."
    rate = _introspection.dollars_per_credit()
    return {"job": job, "available": state is not None, "state": state,
            "theory": theory, "qas": qas, "note": note,
            "dollars_per_credit": rate,
            "cost": _introspection.cost_summary(state, rate)}


def _build_log_source(job_id: str):
    """(stream, start_ms, end_ms, job item) for a job's builder log, or a note."""
    item = _jobs.get_item(Key={"id": job_id}).get("Item") if ENABLED else None
    if item is None:
        return None, "job not found"
    arn = item.get("builder_task_arn")
    if not arn or not BUILDER_LOG_GROUP:
        return None, "This job has no builder-task log stream to read."
    started = int(item.get("started_at") or item.get("created_at") or 0)
    finished = item.get("finished_at")
    end = (int(finished) + 10) if finished else _now() + 1
    return (_introspection.builder_stream(arn, BUILDER_LOG_STREAM_PREFIX),
            (started - 5) * 1000, end * 1000, item), None


def build_logs(job_id: str, head: int = 500, tail: int = 1500) -> dict:
    """The build's log (first `head` + newest `tail` lines) from its builder stream."""
    source, note = _build_log_source(job_id)
    if source is None:
        return {"source": "unavailable", "lines": [], "truncated": False,
                "note": note, "total_lines": 0, "omitted": 0}
    stream, start_ms, end_ms, _item = source
    try:
        return _introspection.build_logs(
            _logs(), BUILDER_LOG_GROUP, stream, start_ms, end_ms,
            head=head, tail=tail)
    except Exception as ex:  # pylint: disable=broad-except
        return {"source": "unavailable", "lines": [], "truncated": False,
                "note": f"Could not read CloudWatch logs ({ex}).",
                "total_lines": 0, "omitted": 0}


def kiro_log_tail(job_id: str, cursor: Optional[str] = None,
                  limit: int = _introspection.MAX_KIRO_LINES) -> dict:
    """The build's kiro-cli lines newer than `cursor` (the live kiro-cli view)."""
    source, note = _build_log_source(job_id)
    if source is None:
        return {"lines": [], "cursor": cursor, "omitted": 0,
                "incomplete": False, "note": note}
    stream, start_ms, end_ms, _item = source
    try:
        return _introspection.kiro_log_tail(
            _logs(), BUILDER_LOG_GROUP, stream, start_ms, end_ms,
            cursor=cursor, limit=limit)
    except Exception as ex:  # pylint: disable=broad-except
        # Keep the caller's cursor so the next refresh retries from the same place.
        return {"lines": [], "cursor": cursor, "omitted": 0, "incomplete": False,
                "note": f"Could not read CloudWatch logs ({ex})."}


def build_log_download(job_id: str):
    """
    (filename, text-chunk iterator) with EVERY line of the build's log, for
    the "Download all lines" button; (None, note) when there is no stream.
    """
    source, note = _build_log_source(job_id)
    if source is None:
        return None, note
    stream, start_ms, end_ms, item = source
    base = _slug(item.get("theory_name") or "job") or "job"
    return f"{base}-build.log", _introspection.iter_build_log_text(
        _logs(), BUILDER_LOG_GROUP, stream, start_ms, end_ms)


def _job_view(item: dict) -> dict:
    return {
        "id": item["id"],
        "theory_id": item.get("theory_id"),
        "theory_name": item.get("theory_name"),
        "status": item.get("status", "queued"),
        "progress": item.get("progress", ""),
        "log_tail": item.get("log_tail", ""),
        "error": item.get("error"),
        "created_at": int(item.get("created_at", 0)),
        "started_at": int(item["started_at"]) if item.get("started_at") else None,
        "finished_at": int(item["finished_at"]) if item.get("finished_at") else None,
        "build_params": _parse_build_params(item.get("build_params_json")),
    }


def _parse_build_params(raw: Optional[str]) -> Optional[dict]:
    """The job's build parameters, or None for jobs started before they existed."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _update_job(job_id: str, **fields) -> None:
    if not ENABLED or not fields:
        return
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    _jobs.update_item(
        Key={"id": job_id},
        UpdateExpression=expr,
        ExpressionAttributeNames={f"#{k}": k for k in fields},
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
    )


# --- Stale builder-job reconciliation -------------------------------------
# A Fargate builder normally writes its own terminal status (finished/failed)
# into the job record. If the task dies without doing so (OOM-kill, stop from
# the console, host failure, a hang that someone stops by hand) the record sits
# at "running" forever and the theory at "building". On read we ask ECS about
# any non-terminal job that has a builder task ARN, and fail the job when ECS
# reports the task STOPPED or no longer knows it (MISSING: stopped tasks age out
# of DescribeTasks after ~1h).
#
# Cost: one batched DescribeTasks call (up to 100 ARNs) per read, and only when
# there are active builder jobs whose last check is older than the interval, so
# the common "nothing building" page load makes no ECS call at all.
_TERMINAL_JOB_STATES = ("finished", "failed", "cancelled")
_RECONCILE_INTERVAL_SECS = 30
_reconcile_lock = threading.Lock()
_last_reconcile: dict = {}  # job_id -> epoch secs of the last ECS check


def _task_id(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


def _stopped_reason(task: dict) -> str:
    parts = [task.get("stoppedReason") or "builder task stopped"]
    if task.get("stopCode"):
        parts.append(f"stopCode={task['stopCode']}")
    for c in task.get("containers", []):
        if c.get("exitCode") is not None:
            parts.append(f"exitCode={c['exitCode']}")
        if c.get("reason"):
            parts.append(c["reason"])
    return "; ".join(parts)


def _fail_orphaned_job(item: dict, reason: str) -> None:
    """Mark a job (and its still-building theory) failed; update `item` in place."""
    jid = item["id"]
    msg = (f"{item.get('theory_name') or jid}: builder task ended without "
           f"reporting a result ({reason})")
    now = _now()
    try:
        _jobs.update_item(
            Key={"id": jid},
            UpdateExpression=("SET #status = :failed, #progress = :progress, "
                              "#finished_at = :now, #error = :err, "
                              "#log_tail = :tail"),
            # Only flip a still-active record: if the builder wrote its own
            # terminal status in the meantime, that one wins.
            ConditionExpression="NOT (#status IN (:fin, :failed, :cancelled))",
            ExpressionAttributeNames={
                "#status": "status", "#progress": "progress",
                "#finished_at": "finished_at", "#error": "error",
                "#log_tail": "log_tail",
            },
            ExpressionAttributeValues={
                ":failed": "failed", ":fin": "finished",
                ":cancelled": "cancelled", ":progress": "builder task stopped",
                ":now": now, ":err": msg, ":tail": _tail(msg),
            },
        )
    except _ddb.meta.client.exceptions.ConditionalCheckFailedException:
        fresh = _jobs.get_item(Key={"id": jid}).get("Item")
        if fresh:
            item.clear()
            item.update(fresh)
        return

    _jlog(jid, f"reconcile: {msg}")
    item.update(status="failed", progress="builder task stopped",
                finished_at=now, error=msg, log_tail=_tail(msg))
    tid = item.get("theory_id")
    if tid:
        theory = _get_theory_item(tid)
        # Don't touch a theory that a newer rebuild has since taken over.
        if (theory and theory.get("status") == "building"
                and theory.get("job_id") in (None, jid)):
            _update_theory(tid, status="failed")


def _reconcile_builder_jobs(items: list) -> None:
    """Fail active jobs whose builder task ECS reports gone. Mutates `items`."""
    if not ENABLED or not BUILDER_CLUSTER:
        return
    now = _now()
    due: dict = {}  # task id -> job item
    with _reconcile_lock:
        for item in items:
            arn = item.get("builder_task_arn")
            if not arn or item.get("status", "queued") in _TERMINAL_JOB_STATES:
                continue
            if now - _last_reconcile.get(item["id"], 0) < _RECONCILE_INTERVAL_SECS:
                continue
            _last_reconcile[item["id"]] = now
            due[_task_id(arn)] = item
    if not due:
        return

    arns = [i["builder_task_arn"] for i in due.values()]
    for start in range(0, len(arns), 100):
        try:
            resp = _ecs().describe_tasks(cluster=BUILDER_CLUSTER,
                                         tasks=arns[start:start + 100])
        except Exception as ex:  # pylint: disable=broad-except
            # Never break a page load over this; the next read retries.
            print(f"reconcile: describe_tasks failed: {ex}", flush=True)
            continue
        gone: dict = {}
        for task in resp.get("tasks", []):
            if task.get("lastStatus") == "STOPPED":
                gone[_task_id(task["taskArn"])] = _stopped_reason(task)
        for failure in resp.get("failures", []):
            if failure.get("reason") == "MISSING":
                gone[_task_id(failure.get("arn", ""))] = (
                    "task no longer known to ECS; it stopped earlier")
        for tid, reason in gone.items():
            item = due.get(tid)
            if item is None:
                continue
            try:
                _fail_orphaned_job(item, reason)
            except Exception as ex:  # pylint: disable=broad-except
                print(f"reconcile: could not fail job {item['id']}: {ex}",
                      flush=True)


def _update_theory(theory_id: str, **fields) -> None:
    if not ENABLED or not fields:
        return
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    _theories.update_item(
        Key={"id": theory_id},
        UpdateExpression=expr,
        ExpressionAttributeNames={f"#{k}": k for k in fields},
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
    )


# --- Document-type detection (text / FHIR) --------------------------------
# A theory's source document is one of:
#   "text" -- prose (Wikipedia-style); split into sentences, TextDocument path.
#   "fhir" -- a JSON HL7 FHIR Bundle for one patient; FHIRdocument path, which
#             reads the raw file as a single JSON object (NOT sentence-split).
# The UI chooser sends "text", "fhir", or "auto"; "auto" resolves here by
# sniffing the content. subjects.subject_type() keys off a Subject having a
# synthea_path (-> "fhir") vs not (-> "default"/text), so the runner registers
# the subject accordingly.
DOC_TYPE_TEXT = "text"
DOC_TYPE_FHIR = "fhir"


# --- Baked-in Synthea sample patients -------------------------------------
# The image ships a set of Synthea-generated HL7 FHIR Bundles (one JSON file
# per synthetic patient) under sentences/synthea_sample_data_fhir_dstu2_latest/.
# The "Add theory" UI can offer these as pick-from-a-list sources instead of a
# pasted/uploaded document: the chosen Bundle's raw JSON is read here and fed
# into the normal create_theory(..., doc_type="fhir") path.
SYNTHEA_DIR_NAME = "synthea_sample_data_fhir_dstu2_latest"


def _synthea_dir() -> "Optional[Path]":
    """
    Resolve the baked-in Synthea sample directory, or None if it is absent
    (e.g. a build that excluded the sample data). Mirrors how run_job resolves
    the sentences dir: prefer Path.cwd()/"sentences", fall back to RUNS_DIR's
    sibling.
    """
    from pathlib import Path
    candidates = [Path.cwd() / "sentences" / SYNTHEA_DIR_NAME]
    try:
        import globals as g  # repo-root module
        candidates.append(Path(g.RUNS_DIR).parent / "sentences" / SYNTHEA_DIR_NAME)
    except Exception:  # pylint: disable=broad-except
        pass
    for d in candidates:
        if d.is_dir():
            return d
    return None


def _synthea_display_name(stem: str) -> str:
    """
    Turn a Synthea filename stem (First##_Last##_<uuid> or First##_Mid##_Last##
    _<uuid>) into a human display name ("First Last"). Reuses subjects.py's
    parser, which strips the trailing digits off the first two name parts.
    """
    try:
        import subjects  # repo-root module
        first, last = subjects.parse_synthea_file_name(stem)
        return f"{first} {last}"
    except Exception:  # pylint: disable=broad-except
        return stem


def list_sample_patients() -> list:
    """
    Return the baked-in Synthea sample patients as
    [{"id": <filename stem>, "name": "First Last"}], sorted by display name.
    `id` is the file stem (no .json), which api_create_theory maps back to the
    file via load_sample_patient_text. Empty list if the sample data is absent.
    """
    d = _synthea_dir()
    if d is None:
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        out.append({"id": p.stem, "name": _synthea_display_name(p.stem)})
    out.sort(key=lambda r: r["name"].lower())
    return out


def load_sample_patient_text(sample_id: str) -> str:
    """
    Read the raw FHIR Bundle JSON for a baked-in sample patient, identified by
    its file stem (as returned by list_sample_patients). Raises ValueError if
    the sample data is absent or the id does not match a known file (the id is
    validated against the directory listing, so an arbitrary path cannot be
    read through this).
    """
    d = _synthea_dir()
    if d is None:
        raise ValueError("sample patient data is not available in this build")
    valid = {p.stem for p in d.glob("*.json")}
    if sample_id not in valid:
        raise ValueError(f"unknown sample patient: {sample_id}")
    return (d / f"{sample_id}.json").read_text(encoding="utf-8")


# --- Web page (Wikipedia) source ------------------------------------------
# The "Add theory" UI can offer a fourth source: a web page URL. The page is
# downloaded here at submit time and its prose extracted into one sentence per
# line via parse_wikipedia_page (the repo-root HTML->sentences parser), then
# fed into the normal create_theory(..., doc_type="text") path. The parser is
# tuned for Wikipedia's article markup (it pulls <p> text and strips [1]-style
# citations); any URL is accepted, but a non-Wikipedia page may extract poorly
# -- the UI warns about that, and this surfaces an empty extraction as a 400.
def is_wikipedia_url(url: str) -> bool:
    """
    True if `url` is an http(s) URL whose host is wikipedia.org or a subdomain
    (e.g. en.wikipedia.org). Used by the API to decide whether to log a
    best-effort warning; the UI shows the user-facing red warning. Host match
    is exact-or-dot-suffix so a look-alike like 'wikipedia.org.evil.com' is
    NOT treated as Wikipedia.
    """
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, TypeError):
        return False
    return host == "wikipedia.org" or host.endswith(".wikipedia.org")


# MediaWiki "chrome": UI/navigation/footer boilerplate that parse_wikipedia_page
# pulls out of <p> tags along with the real prose. These are not article
# content and must never become sentences/facts in a theory (the reviewed-
# revision banner "This is an accepted version of this page" is what failed the
# Diabetes build). Matched case-insensitively.
_WIKI_CHROME_EXACT = frozenset(s.lower() for s in (
    "This is an accepted version of this page",
    "From Wikipedia, the free encyclopedia",
    "Jump to navigation",
    "Jump to search",
    "Jump to content",
    "This article is about the disease.",
    "(Redirected from Diabetes)",
))
# Substrings whose presence marks a whole sentence as chrome, wherever they
# appear (navigation prose, footers, edit-links, retrieval notices).
_WIKI_CHROME_SUBSTR = tuple(s.lower() for s in (
    "the free encyclopedia",
    "[edit]",
    "retrieved from \"",
    "retrieved from “",
    "this page was last edited",
    "text is available under the creative commons",
    "wikipedia\u00ae is a registered trademark",
    "privacy policy",
    "terms of use",
    "disambiguation)",
    "for other uses, see",
    "not to be confused with",
    "citation needed",
    "page semi-protected",
    "learn how and when to remove this",
))
# The reviewed-revision banner as a leading PREFIX on an otherwise-real first
# sentence (the parser sometimes glues it to the opening line).
_WIKI_BANNER_PREFIX = "this is an accepted version of this page"


def _strip_wikipedia_chrome(sentences: list) -> list:
    """
    Drop MediaWiki chrome lines from an extracted sentence list and clean a
    chrome banner glued onto the front of the first real sentence. Operates on
    the WHOLE list (not just the first line) because the reviewed-revision
    banner and navigation boilerplate can land as their own standalone
    sentences anywhere near the top. Returns a new, de-chromed list; order and
    the real prose are preserved.
    """
    cleaned: list = []
    for raw in sentences:
        s = (raw or "").strip()
        if not s:
            continue
        low = s.lower()
        # Peel a banner prefix off a sentence that is otherwise real content.
        if low.startswith(_WIKI_BANNER_PREFIX):
            s = s[len(_WIKI_BANNER_PREFIX):].lstrip(" .\u2014-:\t")
            low = s.lower().strip()
            if not s:
                continue
        if low in _WIKI_CHROME_EXACT:
            continue
        if any(frag in low for frag in _WIKI_CHROME_SUBSTR):
            continue
        cleaned.append(s)
    return cleaned


def fetch_web_page_text(url: str) -> str:
    """
    Download the page at `url` and extract its prose as newline-joined
    sentences, suitable as a doc_type="text" source document. Delegates to the
    repo-root parse_wikipedia_page (HTML -> <p> text -> NLTK sentences, minus
    citation brackets), then strips MediaWiki chrome via _strip_wikipedia_chrome.

    Raises ValueError (-> HTTP 400 via api_create_theory) when the URL is
    malformed, the page cannot be fetched, or no sentences can be extracted
    (e.g. a non-article page). The parser works best on Wikipedia articles; a
    non-Wikipedia page is still attempted but is more likely to extract nothing.
    """
    from urllib.parse import urlparse

    u = (url or "").strip()
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Please provide a valid http(s) URL.")

    try:
        import parse_wikipedia_page  # repo-root module (image: /app)
        sentences = parse_wikipedia_page.parse_wikipedia_page(u)
    except ValueError:
        raise
    except Exception as ex:  # pylint: disable=broad-except
        raise ValueError(f"Could not download or parse that page: {ex}") from ex

    sentences = [s.strip() for s in (sentences or []) if s and s.strip()]
    # Wikipedia articles carry UI/navigation/footer boilerplate (the reviewed-
    # revision banner "This is an accepted version of this page", "From
    # Wikipedia, the free encyclopedia", edit-links, retrieval notices, ...)
    # that parse_wikipedia_page extracts from <p> tags alongside the real prose.
    # Strip it so none of it becomes a bogus sentence/fact in the theory. This
    # banner in particular is what failed the Diabetes build.
    sentences = _strip_wikipedia_chrome(sentences)
    if not sentences:
        raise ValueError(
            "No text could be extracted from that page. The parser is built "
            "for Wikipedia articles; this page may not be one, or may have no "
            "readable paragraphs."
        )
    return "\n".join(sentences)


def _looks_like_fhir(source_text: str) -> bool:
    """
    True when the document parses as a JSON object that looks like an HL7 FHIR
    resource: a Bundle (has an `entry` list whose items carry
    `resource.resourceType`), or a single resource with a `resourceType`.
    """
    import json as _json
    s = source_text.strip()
    if not s or s[0] not in "{[":
        return False
    try:
        obj = _json.loads(s)
    except (ValueError, TypeError):
        return False
    if isinstance(obj, dict):
        if obj.get("resourceType") == "Bundle" and isinstance(obj.get("entry"), list):
            return True
        entries = obj.get("entry")
        if isinstance(entries, list) and entries:
            first = entries[0]
            if isinstance(first, dict) and isinstance(first.get("resource"), dict) \
                    and first["resource"].get("resourceType"):
                return True
        # A bare single resource (e.g. a Patient) is also FHIR-shaped.
        if obj.get("resourceType"):
            return True
    return False


def resolve_doc_type(source_text: str, requested: str) -> str:
    """
    Map the UI's requested type ("text" | "fhir" | "auto") to a concrete
    "text" | "fhir". "auto" sniffs the content; anything unrecognized is text.
    """
    requested = (requested or "auto").strip().lower()
    if requested == DOC_TYPE_FHIR:
        return DOC_TYPE_FHIR
    if requested == DOC_TYPE_TEXT:
        return DOC_TYPE_TEXT
    # auto (or unknown): guess from content.
    return DOC_TYPE_FHIR if _looks_like_fhir(source_text) else DOC_TYPE_TEXT


def _fhir_entry_count(source_text: str) -> int:
    """Number of non-Patient FHIR entries (the units FHIRdocument makes
    sentences from); 0 if it can't be parsed."""
    import json as _json
    try:
        obj = _json.loads(source_text)
        entries = obj.get("entry", []) if isinstance(obj, dict) else []
        return sum(
            1 for e in entries
            if isinstance(e, dict)
            and isinstance(e.get("resource"), dict)
            and e["resource"].get("resourceType") != "Patient"
        )
    except (ValueError, TypeError, AttributeError):
        return 0


def _validate_fhir_json(source_text: str, requested: str) -> None:
    """
    Fail fast on a FHIR document that will not parse, so the POST returns a 400
    at submit time instead of silently misrouting it (auto -> text) or deferring
    the failure to the background job (explicit fhir -> job marked failed).

    Raises ValueError (which api_create_theory maps to HTTP 400) when:
      * requested == "fhir" but the body is not valid JSON; or
      * requested == "auto", the body clearly LOOKS like JSON (starts with
        '{' or '[') -- i.e. the user plainly meant a FHIR/JSON doc -- but it
        does not parse. (A genuine prose document never trips this: it does not
        start with a JSON delimiter, so auto-mode still treats it as text.)

    A well-formed JSON document that simply is not FHIR-shaped is NOT rejected
    here: in auto mode it falls through to the text path as before.
    """
    import json as _json

    requested = (requested or "auto").strip().lower()
    s = source_text.strip()
    looks_like_json = bool(s) and s[0] in "{["

    if requested == DOC_TYPE_FHIR:
        try:
            _json.loads(s)
        except (ValueError, TypeError) as ex:
            raise ValueError(f"FHIR document is not valid JSON: {ex}") from ex
    elif requested != DOC_TYPE_TEXT and looks_like_json:
        # auto (or anything not explicitly "text") + JSON-shaped-but-broken.
        try:
            _json.loads(s)
        except (ValueError, TypeError) as ex:
            raise ValueError(
                "document looks like JSON (FHIR) but is not valid JSON: "
                f"{ex}. If you meant to submit plain text, set doc_type=text."
            ) from ex


# --- Create a theory (tab 3 "Add theory") + kick off a job ----------------
def create_theory(name: str, source_text: str, doc_type: str = "auto",
                   origin: Optional[str] = None,
                   rebuild_of: Optional[str] = None,
                   build_params: Optional[dict] = None) -> dict:
    """
    Persist the source document, create a building theory + queued job, and
    start the inline runner in a background thread. Returns {theory, job}.

    `doc_type` is "text", "fhir", or "auto" (sniff the content). For FHIR the
    document is a JSON Bundle stored verbatim; for text it is prose that gets
    sentence-split.

    `origin` is an optional source-provenance label (e.g. "wiki") shown in the
    UI's type column INSTEAD of doc_type. It never affects the build pipeline:
    a Wikipedia page builds as doc_type="text" but is surfaced as "wiki" so the
    panel distinguishes it from pasted/uploaded prose.

    `build_params` are the "Build parameters" dialog's values (test set,
    kiro-cli budget, ...; see build_params.py). Missing keys take their
    defaults. They are stored on the job and handed to the builder.

    Raises ValueError (HTTP 400 via api_create_theory) for a FHIR document that
    will not parse or an out-of-range build parameter, and TheoryNameTaken
    (HTTP 409) when another theory has the name, before any theory/job record
    is created -- see _validate_fhir_json / build_params.validate /
    check_theory_name.
    """
    if not ENABLED:
        raise RuntimeError("theory store is not configured")

    # Fail fast on invalid input before creating any records.
    params_values = _build_params.validate(build_params)
    params_json = json.dumps(params_values, sort_keys=True)
    _validate_fhir_json(source_text, doc_type)

    resolved = resolve_doc_type(source_text, doc_type)
    jid = str(uuid.uuid4())

    if rebuild_of:
        # In-place rebuild: reuse the EXISTING theory record (same id, name,
        # subject_id, and stored source) rather than creating a new theory.
        # The existing row is flipped back to "building" and a fresh job is
        # pointed at the same theory_id; the builder writes its result back to
        # that same record. Its name is kept, so no uniqueness check is needed.
        existing = _get_theory_item(rebuild_of)
        if not existing:
            raise RuntimeError(f"cannot rebuild unknown theory {rebuild_of!r}")
        tid = rebuild_of
        name = existing.get("name") or name
        subject_id = existing.get("subject_id") or (_slug(name) + "_" + tid[:8])
        # Reuse the stored source document that was persisted on the first build
        # (rebuild_theory passes the same source_text; re-persisting under the
        # existing key keeps the record's source_s3_key valid).
        suffix = "json" if resolved == DOC_TYPE_FHIR else "txt"
        src_key = existing.get("source_s3_key") or f"documents/{tid}.{suffix}"
        _put_text(src_key, source_text)
        if resolved == DOC_TYPE_FHIR:
            unit_count = _fhir_entry_count(source_text)
        else:
            unit_count = len(_split_sentences(source_text))
        # Reset the existing record to building; clear the prior result/key.
        _update_theory(tid, status="building", theory_s3_key=None,
                       sentence_count=unit_count, job_id=jid)
        _jobs.put_item(Item={
            "id": jid, "theory_id": tid, "theory_name": name,
            "status": "queued", "progress": "queued", "log_tail": "",
            "error": None, "created_at": _now(),
            "started_at": None, "finished_at": None,
            # JSON string: DynamoDB's resource API rejects Python floats.
            "build_params_json": params_json,
        })
    else:
        # Theory names are unique: refuse a name another theory already has
        # (TheoryNameTaken, HTTP 409) so the user picks a different one.
        name = check_theory_name(name)

        tid = str(uuid.uuid4())
        # In-process subject id derived from the name (sanitized, unique-suffixed).
        subject_id = _slug(name) + "_" + tid[:8]
        # FHIR docs are stored as .json (FHIRdocument reads the whole file as
        # JSON); text docs as .txt.
        suffix = "json" if resolved == DOC_TYPE_FHIR else "txt"
        src_key = f"documents/{tid}.{suffix}"
        _put_text(src_key, source_text)

        if resolved == DOC_TYPE_FHIR:
            unit_count = _fhir_entry_count(source_text)
        else:
            unit_count = len(_split_sentences(source_text))

        _theories.put_item(Item={
            "id": tid, "name": name, "status": "building",
            "source_s3_key": src_key, "theory_s3_key": None,
            "subject_id": subject_id, "sentence_count": unit_count,
            "doc_type": resolved,
            "origin": (origin or None),
            "created_at": _now(), "job_id": jid,
        })
        _jobs.put_item(Item={
            "id": jid, "theory_id": tid, "theory_name": name,
            "status": "queued", "progress": "queued", "log_tail": "",
            "error": None, "created_at": _now(),
            "started_at": None, "finished_at": None,
            "build_params_json": params_json,
        })

    # Dispatch the build to its own ephemeral Fargate task (full, uncapped
    # pipeline). Theory builds ALWAYS farm out -- there is deliberately no
    # inline fallback: a build running in this web task gets orphaned whenever a
    # deploy drains the task, and a silent fallback once hid a missing
    # ecs:TagResource grant for a full day (every "Fargate" build was really
    # running inline). If dispatch is unconfigured or the launch is denied, the
    # job FAILS loudly so the problem is visible instead of masquerading as a
    # slow inline build.
    if not DISPATCH_ENABLED:
        msg = (f"{name}: Fargate dispatch not configured "
               "(BUILDER_TASK_DEF/BUILDER_CLUSTER/BUILDER_SUBNETS missing); "
               "build FAILED (no inline fallback)")
        _jlog(jid, msg)
        _update_job(jid, status="failed", progress="dispatch not configured",
                    finished_at=_now(), error=msg, log_tail=_tail(msg))
        _update_theory(tid, status="failed")
        return {"theory": _theory_summary(_get_theory_item(tid)),
                "job": get_job(jid)}

    try:
        arn = _dispatch_build(jid, tid, subject_id, name, resolved, src_key,
                              params_json)
    except Exception as ex:  # pylint: disable=broad-except
        msg = (f"{name}: Fargate dispatch failed ({ex}); "
               "build FAILED (no inline fallback)")
        _jlog(jid, msg)
        _update_job(jid, status="failed", progress="dispatch failed",
                    finished_at=_now(), error=str(ex), log_tail=_tail(msg))
        _update_theory(tid, status="failed")
        return {"theory": _theory_summary(_get_theory_item(tid)),
                "job": get_job(jid)}

    _update_job(jid, builder_task_arn=arn, progress="dispatched",
                log_tail=_tail(f"{name}: dispatched to builder task"))
    _jlog(jid, f"{name}: dispatched to Fargate builder ({arn})")

    return {"theory": _theory_summary(_get_theory_item(tid)),
            "job": get_job(jid)}


def _dispatch_build(job_id: str, theory_id: str, subject_id: str, name: str,
                    doc_type: str, source_key: str,
                    params_json: Optional[str] = None) -> str:
    """
    Launch a one-shot Fargate task that runs cdk/docker/build_task.py for this
    job. Returns the launched task's ARN. Raises on launch failure (the caller
    falls back to the inline runner). `params_json` (validated build
    parameters) travels as BUILD_PARAMS.

    The builder uses the SAME image as the web task, with a command override
    (`python build_task.py`) and the per-job parameters passed as env overrides.
    """
    overrides = {
        "containerOverrides": [{
            "name": BUILDER_CONTAINER_NAME,
            "command": ["python", "build_task.py"],
            "environment": [
                {"name": "BUILD_JOB_ID", "value": job_id},
                {"name": "BUILD_THEORY_ID", "value": theory_id},
                {"name": "BUILD_SUBJECT_ID", "value": subject_id},
                {"name": "BUILD_NAME", "value": name},
                {"name": "BUILD_DOC_TYPE", "value": doc_type},
                {"name": "BUILD_SOURCE_S3_KEY", "value": source_key},
                {"name": "BUILD_PARAMS", "value": params_json or "{}"},
            ],
        }],
    }
    resp = _ecs().run_task(
        cluster=BUILDER_CLUSTER,
        taskDefinition=BUILDER_TASK_DEF,
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": BUILDER_SUBNETS,
                "securityGroups": BUILDER_SECURITY_GROUPS,
                # Private subnets with a NAT -> no public IP needed.
                "assignPublicIp": "DISABLED",
            }
        },
        overrides=overrides,
        # Tag so builder tasks are identifiable in the console/cost reports.
        tags=[
            {"key": "chatbot:job", "value": job_id},
            {"key": "chatbot:theory", "value": theory_id},
        ],
    )
    failures = resp.get("failures") or []
    tasks = resp.get("tasks") or []
    if failures or not tasks:
        raise RuntimeError(f"run_task failed: failures={failures}")
    return tasks[0].get("taskArn", "")


def _job_was_cancelled(job_id: str) -> bool:
    """True when the job record says the user killed it (kill_job)."""
    try:
        job = _jobs.get_item(Key={"id": job_id}).get("Item") if ENABLED else None
    except Exception:  # pylint: disable=broad-except
        return False
    return bool(job) and job.get("status") == "cancelled"


def _run_job_safe(job_id, theory_id, subject_id, name, source_text, doc_type,
                  build_params: Optional[dict] = None) -> None:
    try:
        run_job(job_id, theory_id, subject_id, name, source_text, doc_type,
                build_params=build_params)
    except JobCancelled:
        # User-requested kill. kill_job() already set the job DynamoDB record to
        # `cancelled`; mark the theory `cancelled` too (NOT `failed`) so the UI
        # shows a user cancellation as "Cancelled", distinct from a real build
        # failure. Like `failed`, it leaves the "building" state so the theory
        # can be deleted or rebuilt.
        _jlog(job_id, "cancelled: build stopped by user")
        _update_theory(theory_id, status="cancelled")
    except Exception as ex:  # pylint: disable=broad-except
        if _job_was_cancelled(job_id):
            # Killed from the UI: kill_job already marked the job cancelled and
            # stopped this builder task, and the error is the build being torn
            # down. Keep it cancelled rather than overwriting it with failed.
            _jlog(job_id, f"cancelled: build stopped by user ({ex})")
            _update_theory(theory_id, status="cancelled")
            return
        tb = traceback.format_exc()
        # Tag every traceback line with the job marker so job_logs captures the
        # whole traceback from the shared stream, not just its first line.
        _jlog(job_id, f"FAILED: {ex}")
        for tb_line in tb.rstrip("\n").split("\n"):
            _jlog(job_id, tb_line)
        _update_job(job_id, status="failed", error=str(ex),
                    finished_at=_now(),
                    log_tail=_tail(f"{name}: build failed\n{tb}"))
        _update_theory(theory_id, status="failed")
    finally:
        _clear_job_cancel(job_id)
        try:
            import utils as _utils
            _utils.set_cancel_check(None)
        except Exception:  # pylint: disable=broad-except
            pass


# --- Suggested-question generation (build-time, baked into the theory) ------
# How many variation questions to generate per original build question before
# answerability filtering. We generate a few extra candidates per slot because
# some variations won't prove within the theory and get dropped; we stop once
# we have `len(originals)` validated variations (an equal-sized second set).
_VARIATION_OVERSHOOT = 2  # candidates generated per wanted variation
_VARIATION_MAX_CANDIDATES = 24  # hard cap on generate calls, keeps builds bounded


def _original_build_questions(result: dict) -> list:
    """
    The questions the pipeline used/validated while building the query program:
    correctly_answered_questions[].q (preferred) or all_qs_and_as[0]. Returns
    de-duplicated, order-preserving question strings.

    Final fallback: `suggested_questions`. auto_formalizer.load_run_result
    loads the build's run file and already extracts its verified test-set
    questions (correctly_answered_questions) into a `suggested_questions` key
    -- the other two keys are NOT carried through on that path. Without this
    fallback the base question set is empty, and generate_suggested_questions
    would replace the real test-set questions with [].
    """
    raw = (result.get("correctly_answered_questions")
           or result.get("all_qs_and_as")
           or result.get("suggested_questions") or [])
    out: list = []
    seen = set()
    for item in raw:
        if isinstance(item, dict):
            q = item.get("q") or item.get("question") or ""
        elif isinstance(item, (list, tuple)) and item:
            q = item[0]
        elif isinstance(item, str):
            q = item
        else:
            q = ""
        q = (q or "").strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def generate_suggested_questions(result: dict, subject_ids: list,
                                 job_id: str = "", name: str = "",
                                 answers_out: Optional[dict] = None) -> list:
    """
    Build the persisted "I'm Feeling Lucky" question set for a theory:

      originals  +  an equal number of VALIDATED variations

    `originals` are the questions used to build the query program
    (correctly_answered_questions). The variations are freshly generated by the
    LLM (generate_q_and_a, excluding the originals and earlier variations so it
    produces genuinely new phrasings), each from one window of the document
    (qa_window_sentences sentences, cycling through the windows, as the test
    set is generated) so the prompt stays within the model's input limit even
    for a large document. Each is then VALIDATED by running it
    through the real answer pipeline (answer_q_from_chatbot) and kept only if
    the theory can actually prove an answer -- i.e. it stays "answerable within
    the bounds of the theory". We stop once we have len(originals) validated
    variations, or hit the candidate cap.

    Runs at build time with chatbot_backend already pointed at this theory
    (THEORY/SENTENCES/SUBJECT_IDS set by the caller). Best-effort: any failure
    degrades to returning just the originals, so a flaky LLM/prover never fails
    a theory build.

    `answers_out`, when given, receives each accepted variation's answer (the
    one the validation run proved), keyed by the question.
    """
    import chatbot_backend as cb
    import globals as g
    from auto_formalizer import generate_q_and_a, get_global_stats, sentence_windows
    from sentences import Document

    originals = _original_build_questions(result)
    want = len(originals)
    if want == 0:
        return []

    def _log(msg):
        if job_id:
            _jlog(job_id, f"{name}: suggested-questions: {msg}")

    # Windows never straddle two documents (a window is made standalone as
    # one chunk). Prompting with the whole document overflowed the model's
    # input for a large FHIR bundle (Ali Krajcik, 2026-11-09).
    window = int(getattr(getattr(g, "p", None), "qa_window_sentences", 0) or 10)
    windows = [w for run in Document.document_runs(cb.SENTENCES)
               for w in sentence_windows(run, window)]
    if not windows:
        return originals

    _log(f"{want} originals; generating up to {want} validated variations "
         f"from {len(windows)} windows of up to {window} sentences")

    variations: list = []
    seen = {q.lower() for q in originals}
    attempts = 0
    max_candidates = min(_VARIATION_MAX_CANDIDATES, want * _VARIATION_OVERSHOOT)

    while len(variations) < want and attempts < max_candidates:
        attempts += 1
        try:
            # Exclude originals + accepted variations so the LLM varies phrasing
            # rather than repeating. SENTENCES was set by the caller.
            exclude = originals + variations
            q_result = generate_q_and_a(
                windows[(attempts - 1) % len(windows)], exclude, temperature=None,
                stats=get_global_stats())
            candidate = (q_result or {}).get("question", "").strip()
        except Exception as ex:  # pylint: disable=broad-except
            _log(f"generate attempt {attempts} failed: {ex}")
            continue

        if not candidate or candidate.lower() in seen:
            continue
        seen.add(candidate.lower())

        # Validate: keep only if the theory can actually answer it.
        try:
            answer = cb.answer_q_from_chatbot(candidate)
        except Exception as ex:  # pylint: disable=broad-except
            _log(f"validate of {candidate!r} failed: {ex}")
            continue

        if answer and answer.strip().lower() != "i don't know":
            variations.append(candidate)
            if answers_out is not None:
                answers_out[candidate] = answer
            _log(f"accepted variation {len(variations)}/{want}: {candidate!r}")
        else:
            _log(f"dropped unanswerable variation: {candidate!r}")

    out = originals + variations
    _log(f"final set: {len(originals)} originals + {len(variations)} variations")
    return out


def run_job(job_id, theory_id, subject_id, name, source_text, doc_type,
            build_params: Optional[dict] = None) -> None:
    """
    Run one build: formalize the source document into ONE theory
    (auto_formalizer.formalize_document), write it to S3, flip the theory to
    ready. Runs on a dedicated Fargate builder task (build_task.py), or inline
    (bounded) when called in the web task.

    Two ingestion paths, keyed on `doc_type`:

    * "text": prose. The pipeline reads sentences for a subject from
      sentences/{subject_id}.txt (one per line) and precomputed standalones from
      sentences/{subject_id}-standalone.txt (see sentences.TextDocument). The
      pasted document's sentences are already standalone-ish, so we write both
      files from the same lines, avoiding N extra LLM "make standalone" calls.
      The subject is registered WITHOUT a synthea_path, so subject_type ==
      "default" and Document.load_for_subject picks TextDocument.

    * "fhir": a JSON HL7 FHIR Bundle for one patient. FHIRdocument reads the
      raw file as a SINGLE JSON object (json.loads(...)["entry"]), NOT
      sentence-split, and derives one FHIRsentence per non-Patient entry. The
      subject is registered WITH a synthea_path pointing at the written .json
      file, so subjects.subject_type -> "fhir" and load_for_subject picks
      FHIRdocument. We write the raw JSON verbatim (no standalone precompute;
      FHIR standalones are computed per-chunk, re-attaching patient entries).

    Phase 2 swaps this body for a Fargate RunTask dispatch (same job record
    contract), leaving the API and data model untouched.
    """
    from pathlib import Path
    import yaml as _yaml

    # Deferred imports: a failure here becomes a failed job, not a server crash.
    import globals as g
    import subjects

    _update_job(job_id, status="running", progress="starting",
                started_at=_now(), log_tail=_tail(f"{name}: starting build"))
    _raise_if_cancelled(job_id)  # a queued job killed before it ran stops here

    # Install the cooperative-cancel predicate so a running kiro-cli session
    # stops when the user kills this job -- not only at the phase boundaries
    # below.
    import utils as _utils
    _utils.set_cancel_check(lambda: is_job_cancelled(job_id))

    # Resolve the sentences dir TextDocument/FHIRdocument expect. Both build
    # paths from Path.cwd()/"sentences"; we also mirror g.RUNS_DIR's base so the
    # dir exists and is writable in the image.
    sentences_dir = Path(g.RUNS_DIR).parent / "sentences"
    sentences_dir.mkdir(parents=True, exist_ok=True)
    cwd_dir = Path.cwd() / "sentences"
    cwd_dir.mkdir(parents=True, exist_ok=True)

    if doc_type == DOC_TYPE_FHIR:
        unit_count = _run_fhir_ingest(
            job_id, subject_id, name, source_text, subjects, cwd_dir, sentences_dir)
    else:
        unit_count = _run_text_ingest(
            job_id, subject_id, name, source_text, subjects, cwd_dir, sentences_dir)

    # 3. Params and run. The build formalizes the whole document in one unit;
    #    the Build parameters dialog (build_params.py) sets the test-set and
    #    kiro-cli knobs. An inline run (no dedicated builder) is capped at
    #    MAX_SENTENCES_INLINE sentences to keep the web task responsive.
    values = _build_params.validate(build_params)
    g.p = _build_params.apply(
        values, subject_id,
        max_sentences=None if BUILDER_MODE else MAX_SENTENCES_INLINE)
    _jlog(job_id, f"{name}: build parameters "
                  f"{json.dumps(values, sort_keys=True)}")
    _update_job(job_id, progress="loading sentences",
                log_tail=_tail(f"{name}: {unit_count} units ({doc_type})"))
    _jlog(job_id, f"{name}: building one theory over {unit_count} units "
                  f"({doc_type})")

    from auto_formalizer import formalize_document, load_run_result, \
        make_abs_run_file_path
    import auto_formalizer as _af

    # The formalizer reports its progress (phases and kiro-cli sessions) as
    # events: they drive the job's progress pill here and the Job
    # Details tab through the recorder (job_introspection.py). Both
    # are best-effort; report_progress swallows their errors.
    recorder = None
    if ENABLED:
        try:
            recorder = _introspection.Recorder(_s3, BUCKET, job_id)
        except Exception as ex:  # pylint: disable=broad-except
            _jlog(job_id, f"introspection disabled: {ex}")
    on_progress = _progress_reporter(job_id, name)

    def _on_event(event: dict) -> None:
        on_progress(event)
        if recorder is not None:
            recorder.handle(event)
    _af.set_progress_cb(_on_event)

    _raise_if_cancelled(job_id)  # don't start the build if already killed
    try:
        result = formalize_document()
    finally:
        _af.set_progress_cb(None)  # never leak the hook across builds
        if recorder is not None:
            recorder.close()
    _raise_if_cancelled(job_id)
    run_file = Path(result["runs_path"])
    if not result["ok"]:
        raise RuntimeError(
            f"no verified theory: the best kiro-cli session did not reach "
            f"{result['n_required']} correct answers out of {result['num_qs']} "
            "test questions (see Job Details)")

    # The theory is built; what remains (suggested questions, storing the
    # theory) can take a while, so say so.
    _update_job(job_id, progress="post-processing",
                log_tail=_tail(f"{name}: post-processing"))
    _jlog(job_id, f"{name}: post-processing")

    # 4. Read the build's result, bake in the suggested-question set, and
    #    store it in S3.
    loaded = None
    try:
        loaded = load_run_result(run_file)
    except Exception:  # pylint: disable=broad-except
        loaded = None

    if loaded is not None:
        # Generate + validate the "I'm Feeling Lucky" question set and persist
        # it under `suggested_questions` so the picker serves it directly (and
        # chatbot_backend.setup() can read it). Point chatbot_backend at this
        # theory first so generation/validation run against it. Best-effort:
        # a failure here must not fail the build, so we guard it.
        try:
            import chatbot_backend as cb
            from sentences import TextSentence

            theory_str = loaded.get("theory") or loaded.get("valid_theory")
            raw_sentences = loaded.get("sentences", [])
            cb.THEORY = theory_str
            cb.SUBJECT_IDS = [subject_id]
            cb.SENTENCES = [
                TextSentence(s, standalone=s) if isinstance(s, str) else s
                for s in raw_sentences
            ]
            _jlog(job_id, f"{name}: generating suggested questions")
            variation_answers: dict = {}
            loaded["suggested_questions"] = generate_suggested_questions(
                loaded, [subject_id], job_id=job_id, name=name,
                answers_out=variation_answers)
            loaded["variation_answers"] = variation_answers
        except Exception as ex:  # pylint: disable=broad-except
            _jlog(job_id, f"{name}: suggested-questions generation skipped: {ex}")
            loaded.setdefault("suggested_questions",
                              _original_build_questions(loaded))
        # load_run_result() returns a structure containing arbitrary
        # Python OBJECTS -- loaded["p"] is a reconstructed Params, and
        # loaded["sentences"] is a list of TextSentence objects -- while
        # safe_dump can only emit plain types (dict/list/scalars). ANY such
        # object anywhere in the tree raises yaml.representer.RepresenterError
        # and fails the whole build at the final save step. Sanitize the whole
        # structure to plain types before dumping, rather than popping known
        # offenders one at a time (we were previously bitten by Params, then by
        # TextSentence). The stored theory YAML is metadata the chatbot reads
        # back loosely (theory/sentences/suggested_questions as strings), so a
        # lossy object->dict/str conversion is fine.
        loaded = _to_plain(loaded)
        theory_text = _yaml.safe_dump(loaded, sort_keys=False)
    else:
        with open(make_abs_run_file_path(run_file), encoding="utf8") as f:
            theory_text = f.read()

    thy_key = f"theories/{theory_id}/theory.yaml"
    _put_text(thy_key, theory_text)

    summary = (f"{result['n_correct']}/{result['num_qs']} test questions "
               f"verified, {result['num_sentences']} sentences")
    _update_theory(theory_id, status="ready", theory_s3_key=thy_key)
    _update_job(job_id, status="finished", progress=f"finished ({summary})",
                finished_at=_now(),
                log_tail=_tail(f"{name}: build finished ({summary})"))
    _jlog(job_id, f"{name}: build finished ({summary})")


# Progress-pill text for each build phase.
_PHASE_PROGRESS = {"test_set": "generating test set", "theory": "building theory"}
_SESSION_KINDS = {"build": "first build", "feedback": "fixing after verification",
                  "stall_resume": "resumed after a stall"}


def _progress_reporter(job_id: str, name: str):
    """A progress callback that keeps the job's progress pill current."""
    def on_event(event: dict) -> None:
        kind = event.get("type")
        if kind == "phase" and event.get("phase") in _PHASE_PROGRESS:
            text = _PHASE_PROGRESS[event["phase"]]
        elif kind == "test_set":
            text = (f"test set ready ({len(event.get('qs_and_as') or [])} "
                    f"questions, {event.get('n_required')} needed)")
        elif kind == "session_start":
            text = (f"kiro-cli session {event.get('session')} "
                    f"({_SESSION_KINDS.get(event.get('kind'), event.get('kind'))})")
        elif kind == "session_end":
            text = (f"session {event.get('session')} verified "
                    f"{event.get('n_correct')} correct"
                    + ("" if event.get("valid") else " (theory not valid)"))
        else:
            return
        _update_job(job_id, progress=text, log_tail=_tail(f"{name}: {text}"))
        _jlog(job_id, f"{name}: {text}")
    return on_event


def _run_text_ingest(job_id, subject_id, name, source_text, subjects,
                     cwd_dir, sentences_dir) -> int:
    """
    Prepare a TEXT subject: register a plain Subject (no synthea_path ->
    subject_type "default" -> TextDocument), and write the raw sentence file
    TextDocument reads. Returns the sentence count.

    Decontextualization (the per-sentence standalone rewrite) is an LLM call per
    sentence. Two modes:

    * BUILDER_MODE (dedicated Fargate builder, the normal path now): use the
      FULL document (no MAX_SENTENCES_INLINE cap) and do NOT pre-write
      `{id}-standalone.txt`. TextDocument then runs the real
      `compute_standalones()` (LLM per sentence) and writes genuine
      decontextualized sentences -- so the view-sentences table shows a true
      original-vs-decontextualized pair.

    * Legacy inline mode (web task, fallback only): truncate to
      MAX_SENTENCES_INLINE and pre-write the standalone file identical to the
      raw file, so no extra LLM calls run and the small web task stays
      responsive (columns match until the theory is rebuilt by a builder task).
    """
    sentences = _split_sentences(source_text)
    inline = sentences if BUILDER_MODE else sentences[:MAX_SENTENCES_INLINE]
    _jlog(job_id, f"{name}: starting TEXT build "
                  f"(subject {subject_id}, {len(inline)} sentences, "
                  f"{'full/builder' if BUILDER_MODE else 'inline'})")

    subjects.ID_TO_SUBJECT[subject_id] = subjects.Subject(name, subject_id)

    for d in {sentences_dir, cwd_dir}:
        (d / f"{subject_id}.txt").write_text("\n".join(inline), encoding="utf-8")
        if not BUILDER_MODE:
            # Inline shortcut: standalone == original (skip the N LLM calls).
            # Under BUILDER_MODE we deliberately leave the standalone file
            # absent so TextDocument.compute_standalones() generates the real
            # decontextualized versions.
            (d / f"{subject_id}-standalone.txt").write_text(
                "\n".join(inline), encoding="utf-8")
    return len(inline)


def _run_fhir_ingest(job_id, subject_id, name, source_text, subjects,
                     cwd_dir, sentences_dir) -> int:
    """
    Prepare a FHIR subject: write the raw JSON Bundle verbatim where
    FHIRdocument reads it, and register a Subject WITH a synthea_path pointing
    at it, so subjects.subject_type -> "fhir" and Document.load_for_subject
    picks FHIRdocument. FHIRdocument reads the file as one JSON object and
    derives one FHIRsentence per non-Patient entry; no sentence-splitting and
    no standalone precompute (FHIR standalones are per-chunk). Returns the
    non-Patient entry count (bounded).

    NOTE: FHIRdocument builds its raw path as sentences/{synthea_path} with NO
    suffix (is_synthea_based -> suffix ""), so synthea_path must be the exact
    filename under sentences/. We give it "{subject_id}.json" and write that.
    """
    import json as _json

    fname = f"{subject_id}.json"
    _jlog(job_id, f"{name}: starting FHIR build (subject {subject_id})")

    # Optionally bound the entry list so the inline run stays small, mirroring
    # MAX_SENTENCES_INLINE for text. We keep all Patient entries (context) and
    # cap the non-Patient entries.
    try:
        obj = _json.loads(source_text)
    except (ValueError, TypeError) as ex:
        raise ValueError(f"FHIR document is not valid JSON: {ex}") from ex
    entries = obj.get("entry", []) if isinstance(obj, dict) else []
    patient = [e for e in entries if isinstance(e, dict)
               and isinstance(e.get("resource"), dict)
               and e["resource"].get("resourceType") == "Patient"]
    others = [e for e in entries if e not in patient]
    bounded_others = others if BUILDER_MODE else others[:MAX_SENTENCES_INLINE]
    if isinstance(obj, dict):
        obj = dict(obj)
        obj["entry"] = patient + bounded_others
    bounded_text = _json.dumps(obj)

    # FHIRdocument reads Path.cwd()/"sentences"/{synthea_path}; mirror into both
    # candidate dirs so whichever cwd the pipeline resolves finds the file.
    for d in {sentences_dir, cwd_dir}:
        (d / fname).write_text(bounded_text, encoding="utf-8")

    subjects.ID_TO_SUBJECT[subject_id] = subjects.Subject(
        name, subject_id, synthea_path=fname)
    _jlog(job_id, f"{name}: FHIR subject registered with "
                  f"{len(patient)} patient + {len(bounded_others)} other entries")
    return len(bounded_others)


# --- Text utils -----------------------------------------------------------
def _split_sentences(text: str) -> list[str]:
    """
    Split a document into sentences. Prefer NLTK (used elsewhere in the repo);
    fall back to a simple regex when the punkt data is unavailable.
    """
    text = text.strip()
    if not text:
        return []
    try:
        import nltk
        try:
            nltk.data.find("tokenizers/punkt")
        except LookupError:
            nltk.download("punkt", quiet=True)
        return [s.strip() for s in nltk.sent_tokenize(text) if s.strip()]
    except Exception:  # pylint: disable=broad-except
        import re
        parts = re.split(r"(?<=[.!?])\s+", text)
        return [p.strip() for p in parts if p.strip()]


def _slug(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "theory"


def _to_plain(obj):
    """
    Recursively convert an arbitrary structure into YAML-safe plain types
    (dict / list / scalars) so yaml.safe_dump never hits a RepresenterError.

    The result loaded by load_run_result() carries live
    objects (a Params, a list of TextSentence, possibly other dataclasses).
    safe_dump refuses any such object, failing the whole build at the save
    step. We walk the tree once and coerce every non-plain value:
      * dict/list/tuple -> recurse (tuples become lists)
      * scalars (str/int/float/bool/None) -> kept as-is
      * objects with to_dict()        -> that dict (recursed)
      * other objects with __dict__   -> their attribute dict (recursed)
      * anything else                 -> str(obj)
    The stored YAML is metadata the chatbot reads back loosely, so a lossy
    object->dict/str conversion is acceptable.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return _to_plain(to_dict())
        except Exception:  # pylint: disable=broad-except
            pass
    attrs = getattr(obj, "__dict__", None)
    if isinstance(attrs, dict) and attrs:
        return _to_plain(dict(attrs))
    return str(obj)


def _tail(text: str, n: int = 4000) -> str:
    return text[-n:]
