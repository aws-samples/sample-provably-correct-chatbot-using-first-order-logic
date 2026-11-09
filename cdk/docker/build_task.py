# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
One-shot Fargate builder entrypoint for a single theory build.

Phase 2 of the theory-build architecture: instead of running the WikiLogifier
pipeline inline in a daemon thread inside the 2-vCPU web task (which forced the
`MAX_SENTENCES_INLINE` cap and the "skip decontextualization" shortcut), every
build is dispatched to its OWN ephemeral ECS Fargate task that runs this file
as its command. That task has the whole container to itself, so it runs the
full, uncapped pipeline -- including real per-sentence decontextualization.

Contract (unchanged data model):
  * The web task (store.create_theory) has ALREADY created the `theories` and
    `jobs` DynamoDB records (status building/queued) and uploaded the source
    document to S3 before launching this task.
  * This task reads its job parameters from the environment (set as a container
    override by store._dispatch_build), records its own ECS task ARN into the
    job record so a kill can `stop_task` it, loads the source document from S3,
    then calls the EXISTING store.run_job -- the same code the inline runner
    called. run_job flips the records to ready/finished (or failed) exactly as
    before.

Environment set by the dispatcher (in addition to the task-def's own env):
  BUILD_JOB_ID        job uuid
  BUILD_THEORY_ID     theory uuid
  BUILD_SUBJECT_ID    in-process subject id
  BUILD_NAME          theory display name
  BUILD_DOC_TYPE      "text" | "fhir"
  BUILD_SOURCE_S3_KEY S3 key of the uploaded source document
  BUILD_PARAMS        JSON of the validated build parameters (test set,
                      kiro-cli budget, ...; build_params.py). Optional: absent
                      or "{}" means every default.

Exit code is 0 on a build that finished OR was cancelled OR failed-with-a-
recorded-job (the failure is captured in the job record, which is the real
channel); it is non-zero only when the task could not even start the build
(missing params, unreadable source), so ECS surfaces an infra fault distinct
from an ordinary build failure.
"""
from __future__ import annotations

import os
import sys


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"build_task: missing required env {name}")
    return val


def main() -> int:
    # Deferred import so an import failure is reported with context rather than
    # crashing at module load (and so this file is cheap to import for tests).
    import store

    if not store.ENABLED:
        # No bucket/tables configured -- nothing this task can do. Treat as an
        # infra fault (non-zero) so the dispatcher's run_task is not silently a
        # no-op.
        print("build_task: store disabled (bucket/tables unset); aborting",
              file=sys.stderr)
        return 2

    job_id = _require("BUILD_JOB_ID")
    theory_id = _require("BUILD_THEORY_ID")
    subject_id = _require("BUILD_SUBJECT_ID")
    name = os.environ.get("BUILD_NAME", theory_id)
    doc_type = os.environ.get("BUILD_DOC_TYPE", store.DOC_TYPE_TEXT)
    source_key = _require("BUILD_SOURCE_S3_KEY")
    try:
        import json
        build_params = json.loads(os.environ.get("BUILD_PARAMS") or "{}")
    except ValueError as ex:
        print(f"build_task: BUILD_PARAMS is not JSON ({ex}); using defaults",
              file=sys.stderr)
        build_params = {}

    # Record this task's ARN into the job record so kill_job can stop_task it.
    # The ECS agent exposes the task ARN via the metadata endpoint; fall back to
    # the env var some task configs set. Best-effort: a build still runs without
    # it (kill then only marks the record, same as a cross-process inline kill).
    task_arn = _discover_task_arn()
    if task_arn:
        try:
            store._update_job(job_id, builder_task_arn=task_arn)
        except Exception as ex:  # pylint: disable=broad-except
            print(f"build_task: could not record task ARN: {ex}",
                  file=sys.stderr)

    # Pull the source document the web task uploaded to S3.
    try:
        source_text = store._get_text(source_key)
    except Exception as ex:  # pylint: disable=broad-except
        # Can't even read the input -> record the job failed, then exit non-zero
        # (infra fault: the input promised by the dispatcher was not there).
        try:
            store._update_job(job_id, status="failed",
                              error=f"source document unreadable: {ex}",
                              finished_at=store._now())
            store._update_theory(theory_id, status="failed")
        except Exception:  # pylint: disable=broad-except
            pass
        print(f"build_task: cannot read source {source_key}: {ex}",
              file=sys.stderr)
        return 3

    print(f"build_task: starting build job={job_id} theory={theory_id} "
          f"subject={subject_id} doc_type={doc_type} "
          f"source={source_key} ({len(source_text)} chars)")

    # Run the full, UNCAPPED pipeline. _run_job_safe records cancellation and
    # failure into the job record itself (its whole purpose), so an ordinary
    # build failure here is a recorded outcome, not an infra fault -> exit 0.
    store._run_job_safe(job_id, theory_id, subject_id, name, source_text,
                        doc_type, build_params=build_params)
    print(f"build_task: run_job returned for job={job_id}")
    return 0


def _discover_task_arn() -> str:
    """
    Resolve this Fargate task's ARN from the ECS task-metadata endpoint
    (ECS_CONTAINER_METADATA_URI_V4), falling back to an explicit env var. Best
    effort: returns "" if neither is available (local runs, older platforms).
    """
    uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4") \
        or os.environ.get("ECS_CONTAINER_METADATA_URI")
    # The ECS agent sets an http:// link-local URL; refuse anything else
    # (file://, ftp://, ...) before opening it.
    if uri and not uri.lower().startswith(("http://", "https://")):
        print(f"build_task: ignoring non-http task metadata URI {uri!r}",
              file=sys.stderr)
        uri = ""
    if uri:
        try:
            import json
            import urllib.request
            with urllib.request.urlopen(f"{uri}/task", timeout=2) as resp:  # nosec B310 (scheme checked above)
                meta = json.loads(resp.read().decode("utf-8"))
            arn = meta.get("TaskARN") or ""
            if arn:
                return arn
        except Exception:  # pylint: disable=broad-except
            pass
    return os.environ.get("BUILD_TASK_ARN", "")


if __name__ == "__main__":
    sys.exit(main())
