"""
FastAPI entrypoint for the chatbot container.

Runs from the repo root (/app in the image) and imports the repo modules
directly. It serves the chatbot endpoints (/subject, /ask, /lucky,
/lucky-questions) against theories in the store, the theory and job API the
React frontend uses (/api/...), and the built SPA for everything else.
"""
import os
import html
import json
import threading

import yaml
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               StreamingResponse)
from starlette.concurrency import iterate_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth               # Cognito app-level auth (beside this file)
import store             # S3+DynamoDB theory/job store + inline runner (beside this file)
import build_params      # Build parameters dialog: specs + validation (beside this file)
import chatbot_backend  # repo-root module
import globals as g      # repo-root module (shared mutable config)
import subjects          # repo-root module
from params import Params  # repo-root module
from sentences import TextSentence  # repo-root module

app = FastAPI(title="Provably-correct chatbot")

# Serialize per-request theory switching: /ask mutates chatbot_backend module
# globals (THEORY/SENTENCES/SUBJECT_IDS) and then forks prover workers that read
# them, so two concurrent /ask calls against different theories must not race.
_theory_lock = threading.Lock()
# The theory currently loaded into chatbot_backend globals (by id), so we can
# skip reloading when the same theory is asked twice in a row.
_loaded_theory_id: str = ""
# Theory-specific suggested questions for the currently loaded theory, kept in
# sync by _apply_theory. Served by GET /lucky-questions for the picker.
_suggested_questions: list = []

# CloudFront serves the SPA same-origin, so CORS is not strictly needed in
# production. Kept permissive so the API also works from a local `npm start`.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Question(BaseModel):
    text: str
    theory_id: str = ""  # empty => use the currently loaded theory


def require_auth(request: Request) -> dict:
    """
    FastAPI dependency: 401 if the request has no valid session. The frontend
    turns a 401 on an API call into a redirect to /auth/login. (When Cognito
    is not configured -- local dev -- auth.current_user returns a stub, so
    this is a no-op.)
    """
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


# --- Health + auth routes (all UNauthenticated) --------------------------
@app.get("/healthz")
async def healthz() -> dict:
    # Dedicated unauthenticated liveness probe for the ALB target group, so
    # health checks never depend on a Cognito session.
    return {"status": "ok"}


@app.get("/auth/login")
async def auth_login(request: Request):
    return auth.login(request)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    return auth.callback(request)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    return auth.logout(request)


def _parse_theory_result(text: str, source: str = "<theory>") -> dict:
    """
    Parse a stored theory YAML (string) into the result dict. The store keeps
    either the build's run file ([params, result]) or the post-processed
    result as one flat document ([result]); both are accepted, as is a result
    nested under a 'result' key. The theory lives under 'theory' (or
    'valid_theory').
    """
    blobs = list(yaml.safe_load_all(text))

    def _theory_of(d: dict):
        return d.get("theory") or d.get("valid_theory")

    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        candidate = blob.get("result", blob)
        if isinstance(candidate, dict) and _theory_of(candidate):
            return candidate
    raise RuntimeError(
        f"No theory found in {source} (parsed {len(blobs)} YAML doc(s))"
    )


def _extract_suggested_questions(result: dict) -> list:
    """
    Pull the theory-specific, pre-validated question strings out of a
    theory's result dict for the "I'm Feeling Lucky" picker.

    PREFERS the persisted, expanded `suggested_questions` list (originals +
    validated variations, baked in at build time by
    store.generate_suggested_questions). Falls back to deriving from the build
    questions for theories built before that key existed.

    Tolerates the shapes the pipeline emits:
      - suggested_questions: list of plain strings (the baked expanded set)
      - correctly_answered_questions: list of plain strings OR of
        {"q": ..., "answer": ...} dicts
      - all_qs_and_as: list of (question, answer) tuples
    De-duplicates while preserving order and drops blanks.
    """
    raw = (result.get("suggested_questions")
           or result.get("correctly_answered_questions")
           or result.get("all_qs_and_as") or [])
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


def _apply_theory(result: dict, subject_ids: list) -> None:
    """
    Point the chatbot_backend module globals at `result` so /ask and /lucky
    answer against this theory. Caller must hold _theory_lock.
    """
    def _theory_of(d: dict):
        return d.get("theory") or d.get("valid_theory")

    # Theory-specific, pre-validated questions used by the "I'm Feeling Lucky"
    # picker (GET /lucky-questions). Stash the extracted strings for whichever
    # theory is now loaded so the endpoint can serve them without a Bedrock
    # call (unlike /lucky, which generates a fresh question each time).
    global _suggested_questions
    _suggested_questions = _extract_suggested_questions(result)

    chatbot_backend.SUBJECT_IDS = subject_ids
    chatbot_backend.THEORY = _theory_of(result)
    # The YAML stores sentences as plain strings. generate_q_and_a() (/lucky)
    # asserts each element is a Sentence and later calls doc_class.
    # make_chunk_standalone_, so wrap each string as a TextSentence (which sets
    # doc_class=TextDocument and carries its own standalone text). The stored
    # sentences are already standalone, so original == standalone.
    raw_sentences = result.get("sentences", [])
    chatbot_backend.SENTENCES = [
        TextSentence(s, standalone=s) if isinstance(s, str) else s
        for s in raw_sentences
    ]

    # The serving path needs globals.p: starmap_with_dupes reads
    # g.p.do_profile when forking prover workers (/ask), and generate_q_and_a
    # reads g.p.subject_ids (/lucky). A default Params does for both.
    g.p = Params(subject_ids=subject_ids)
    print(
        f"server: applied theory for {subject_ids} with "
        f"{len(chatbot_backend.SENTENCES)} sentences, "
        f"{len(_suggested_questions)} suggested questions"
    )


def _stored_theory_yaml(detail: dict) -> str:
    """
    The theory's stored YAML document. store.get_theory returns
    it as `computed_theory_yaml` and puts the extracted SMT-LIB theory in
    `computed_theory`; parsing that SMT-LIB as YAML fails on any line that
    looks like a mapping (a comment like `; (f x): ...`), which broke the
    chatbot for such theories. `computed_theory` is only a fallback for a
    store that does not provide the YAML view.
    """
    return detail.get("computed_theory_yaml") or detail.get("computed_theory") or ""


def _load_selected_theory(theory_id: str) -> None:
    """
    Load a store-backed theory (by id) into chatbot_backend if it isn't already
    loaded. Serialized via _theory_lock by the caller. Raises on a missing or
    not-ready theory.
    """
    global _loaded_theory_id
    if theory_id == _loaded_theory_id:
        return
    detail = store.get_theory(theory_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="theory not found")
    if detail.get("status") != "ready":
        raise HTTPException(status_code=409,
                            detail=f"theory is {detail.get('status')}, not ready")
    text = _stored_theory_yaml(detail)
    if not text:
        raise HTTPException(status_code=409, detail="theory has no computed artifact")
    result = _parse_theory_result(text, source=f"theory:{theory_id}")
    subject_ids = [detail.get("subject_id") or theory_id]
    _apply_theory(result, subject_ids)
    _loaded_theory_id = theory_id


@app.on_event("startup")
async def startup_event() -> None:
    # Warm the Cognito client secret + JWKS (no-op when auth is unconfigured).
    # No theory is loaded until a request names one (theory_id): every theory
    # lives in the store and is built from the Theories tab.
    auth.init()


def _require_theory(theory_id: str) -> None:
    """Load the requested theory, or fail when none is selected or loaded."""
    if theory_id:
        _load_selected_theory(theory_id)
    elif not _loaded_theory_id:
        raise HTTPException(status_code=409,
                            detail="no theory selected; build one in the Theories tab")


def _resolve_subject(subject_id: str):
    """
    Look up a Subject by id, tolerating hyphen/underscore skew between the
    caller (which may use 'new-zealand') and the canonical subject ids in
    subjects.py (which use 'new_zealand').
    """
    for candidate in (subject_id, subject_id.replace("-", "_"),
                      subject_id.replace("_", "-")):
        try:
            return subjects.get_subject_by_id(candidate)
        except ValueError:
            continue
    raise ValueError(f"Subject with id {subject_id!r} not found.")


@app.get("/subject")
async def get_subject(theory_id: str = "",
                      user: dict = Depends(require_auth)) -> dict:
    # The subtitle must reflect the *selected* theory, not whichever theory
    # happens to be loaded in the process globals. When the client passes a
    # theory_id (the dropdown selection), resolve the title from that theory's
    # subject; otherwise fall back to the currently-loaded subject ids.
    #
    # chatbot_backend.current_subject_id() returns a human-readable *label*
    # (join_with_and over SUBJECT_IDS), not a lookup key, so resolve the real
    # Subject objects straight from the ids instead.
    subject_ids = None
    if theory_id:
        detail = store.get_theory(theory_id)
        if detail is not None:
            sid = detail.get("subject_id") or theory_id
            # Prefer the theory's stored name (e.g. "Diabetes") so a built
            # theory shows its own name even when its subject isn't registered
            # in subjects.py; fall back to the subject's canonical title.
            name = detail.get("name")
            if name:
                return {
                    "title": subjects.Subject(name, sid).title()
                }
            subject_ids = [sid]
    if subject_ids is None:
        subject_ids = chatbot_backend.SUBJECT_IDS or []
    titles = []
    for sid in subject_ids:
        try:
            titles.append(_resolve_subject(sid).title())
        except ValueError:
            titles.append(subjects.Subject(sid, sid).title())
    return {"title": " ".join(titles)}


@app.post("/ask")
async def ask_question(question: Question,
                       user: dict = Depends(require_auth)) -> dict:
    # Switch to the requested theory (if any) under the lock, then answer.
    # answer_q_from_chatbot forks prover workers that read chatbot_backend
    # globals, so the whole switch+answer is serialized.
    with _theory_lock:
        _require_theory(question.theory_id)
        answer = chatbot_backend.answer_q_from_chatbot(question.text)
    return {"answer": answer}


@app.get("/lucky")
async def feeling_lucky(theory_id: str = "",
                        user: dict = Depends(require_auth)) -> dict:
    with _theory_lock:
        _require_theory(theory_id)
        question = chatbot_backend.im_feeling_lucky()
    return {"question": question}


@app.get("/lucky-questions")
async def lucky_questions(theory_id: str = "",
                          user: dict = Depends(require_auth)) -> dict:
    """
    Theory-specific suggested questions for the "I'm Feeling Lucky" picker.
    Returns the loaded theory's pre-validated questions (no Bedrock call,
    unlike /lucky). Loads the requested theory first so the list matches the
    selected theory even if a different one was loaded last.
    """
    with _theory_lock:
        _require_theory(theory_id)
        questions = list(_suggested_questions)
    return {"questions": questions}


# --- Theory + job endpoints (tabs 2/3/4) ---------------------------------
class CreateTheory(BaseModel):
    name: str
    document: str  # pasted source text (upload path uses the Form endpoint)
    doc_type: str = "auto"  # "text" | "fhir" | "auto" (sniff the content)


@app.get("/api/theories")
async def api_list_theories(user: dict = Depends(require_auth)) -> dict:
    return {"theories": store.list_theories()}


@app.get("/api/theories/{theory_id}")
async def api_get_theory(theory_id: str,
                         user: dict = Depends(require_auth)) -> dict:
    detail = store.get_theory(theory_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="theory not found")
    # Tab 2 wants the actual theory -- the SMT-LIB statements cvc5 consumes --
    # not the YAML envelope. Pull it out from under the 'theory'/'valid_theory'
    # key. Keep the raw YAML available under 'computed_theory_yaml' for anyone
    # who wants it, and surface the SMT-LIB as 'computed_theory'.
    raw_yaml = _stored_theory_yaml(detail)
    smt = _extract_smt_theory(raw_yaml)
    detail["computed_theory_yaml"] = raw_yaml
    detail["computed_theory"] = smt if smt else raw_yaml
    return detail


@app.delete("/api/theories/{theory_id}")
async def api_delete_theory(theory_id: str,
                            user: dict = Depends(require_auth)) -> dict:
    """
    Delete a theory: removes its DynamoDB record (the user-visible removal) and
    best-effort deletes its S3 artifacts. 404 when the id is unknown. Returns
    {"ok": True, "deleted": <id>} on success.
    """
    result = store.delete_theory(theory_id)
    if result.get("ok"):
        return result
    note = result.get("note", "")
    if note == "theory not found":
        raise HTTPException(status_code=404, detail="theory not found")
    raise HTTPException(status_code=400, detail=note or "could not delete theory")


@app.get("/api/build-params")
async def api_build_params(user: dict = Depends(require_auth)) -> dict:
    """
    The parameters the "Build parameters" dialog offers (test set, kiro-cli
    budget, ...), each with its type, bounds, help text and current default.
    """
    return {"params": build_params.specs()}


def _build_params_from(raw) -> dict:
    """Validate submitted build parameters; 400 on a bad value."""
    if raw in (None, ""):
        raw = {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as ex:
            raise HTTPException(status_code=400,
                                detail="build_params is not valid JSON") from ex
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="build_params must be an object")
    try:
        return build_params.validate(raw)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


@app.post("/api/theories/{theory_id}/rebuild")
async def api_rebuild_theory(theory_id: str, request: Request,
                             user: dict = Depends(require_auth)) -> dict:
    """
    Re-run the build for an existing theory from its stored source document,
    creating a FRESH theory + job (tracked in the Jobs tab) through the normal
    build path. 404 when the id is unknown; 400 when the theory has no stored
    source to rebuild from or a build parameter is invalid. An optional JSON
    body {build_params: {...}} sets the build's parameters. Returns
    create_theory's {theory, job} on success.
    """
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            body = {}
    values = _build_params_from((body or {}).get("build_params"))
    result = store.rebuild_theory(theory_id, build_params=values)
    # create_theory returns {theory, job} on success; rebuild_theory returns
    # {"ok": False, "note": ...} only for the error cases.
    if result.get("ok") is False:
        note = result.get("note", "")
        if note == "theory not found":
            raise HTTPException(status_code=404, detail="theory not found")
        raise HTTPException(status_code=400,
                            detail=note or "could not rebuild theory")
    return result


def _extract_smt_theory(text: str) -> str:
    """
    Extract the SMT-LIB theory block from a stored theory YAML string.
    Returns "" if no theory can be parsed (caller falls back to the raw text).
    """
    if not text.strip():
        return ""
    try:
        result = _parse_theory_result(text)
    except Exception:  # pylint: disable=broad-except
        return ""
    theory = result.get("theory") or result.get("valid_theory")
    if theory is None:
        return ""
    # The theory is typically a single SMT-LIB string, but tolerate a list of
    # statements by joining them.
    if isinstance(theory, (list, tuple)):
        return "\n".join(str(s) for s in theory)
    return str(theory)


@app.get("/api/sample-patients")
async def api_list_sample_patients(user: dict = Depends(require_auth)) -> dict:
    """
    List the baked-in Synthea sample patients the "Add theory" form can offer
    as a FHIR source instead of a pasted/uploaded document. Each entry is
    {"id": <stem>, "name": "First Last"}; the id is passed back as
    `sample_patient_id` to POST /api/theories.
    """
    return {"sample_patients": store.list_sample_patients()}


@app.post("/api/theories")
async def api_create_theory(request: Request,
                            user: dict = Depends(require_auth)) -> dict:
    """
    Create a theory from one of four sources, then kick off a build job:
      * a pasted document        (JSON {name, document, doc_type})
      * an uploaded file         (multipart: name + file + doc_type)
      * a baked-in sample patient(JSON {name?, sample_patient_id}) -- loads that
        Synthea FHIR Bundle server-side and builds it as doc_type="fhir".
      * a web page URL           (JSON {name?, wikipedia_url}) -- downloads the
        page server-side, extracts its prose to sentences (Wikipedia-focused
        parser), and builds it as doc_type="text".
    """
    if not store.ENABLED:
        raise HTTPException(status_code=503, detail="theory store not configured")
    content_type = request.headers.get("content-type", "")
    origin: str | None = None  # source provenance for the UI (e.g. "wiki")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        name = str(form.get("name") or "").strip()
        doc_type = str(form.get("doc_type") or "auto").strip().lower()
        upload = form.get("file")
        if upload is None:
            raise HTTPException(status_code=400, detail="no file uploaded")
        raw = await upload.read()
        document = raw.decode("utf-8", errors="replace")
        if not name:
            name = getattr(upload, "filename", "Uploaded document")
        values = _build_params_from(form.get("build_params"))
    else:
        body = await request.json()
        values = _build_params_from(body.get("build_params"))
        name = str(body.get("name") or "").strip()
        document = str(body.get("document") or "")
        doc_type = str(body.get("doc_type") or "auto").strip().lower()
        # Source #3: a baked-in Synthea sample patient. Load its Bundle here and
        # force the FHIR path; default the theory name to the patient's name.
        sample_id = str(body.get("sample_patient_id") or "").strip()
        if sample_id:
            try:
                document = store.load_sample_patient_text(sample_id)
            except ValueError as ex:
                raise HTTPException(status_code=400, detail=str(ex)) from ex
            doc_type = "fhir"
            if not name:
                known = {p["id"]: p["name"]
                         for p in store.list_sample_patients()}
                name = known.get(sample_id, sample_id)
        # Source #4: a web page URL (Wikipedia-focused). Download and extract
        # its prose to sentences HERE (at submit time), then force the text
        # path. The parser targets Wikipedia article markup; a non-Wikipedia
        # URL is accepted but may extract poorly (the UI warns the user).
        wikipedia_url = str(body.get("wikipedia_url") or "").strip()
        if wikipedia_url:
            try:
                document = store.fetch_web_page_text(wikipedia_url)
            except ValueError as ex:
                raise HTTPException(status_code=400, detail=str(ex)) from ex
            doc_type = "text"
            origin = "wiki"
            if not store.is_wikipedia_url(wikipedia_url):
                print(f"server: building theory from NON-Wikipedia URL "
                      f"{wikipedia_url!r}; extraction may be poor")
            if not name:
                name = wikipedia_url
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not document.strip():
        raise HTTPException(status_code=400, detail="document is empty")
    try:
        return store.create_theory(name, document, doc_type=doc_type,
                                   origin=origin, build_params=values)
    except ValueError as ex:
        # e.g. invalid FHIR JSON caught by store._validate_fhir_json: fail fast
        # with a 400 at submit time rather than a 500 or a deferred job failure.
        raise HTTPException(status_code=400, detail=str(ex)) from ex


@app.get("/api/jobs")
async def api_list_jobs(user: dict = Depends(require_auth)) -> dict:
    return {"jobs": store.list_jobs()}


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str,
                      user: dict = Depends(require_auth)) -> dict:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/api/jobs/{job_id}/logs")
async def api_get_job_logs(job_id: str,
                           limit: int = 2000,
                           user: dict = Depends(require_auth)) -> dict:
    """
    Full log for a job, tailed from CloudWatch.

    The inline runner shares the web task's process, so a job's pipeline stdout
    is interleaved into this task's single CloudWatch stream; store.job_logs
    pulls out just this job's lines (by its `[job <id>]` marker) over the job's
    lifetime window. Degrades to the job's stored status line when the log group
    is unconfigured or CloudWatch is unreachable (the `source`/`note` fields say
    which). 404 only when the job id itself is unknown.
    """
    if store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    # Clamp limit to a sane range so a bad query can't ask for everything.
    limit = max(1, min(int(limit), 10000))
    return store.job_logs(job_id, limit=limit)


@app.get("/api/jobs/{job_id}/introspection")
async def api_job_introspection(job_id: str,
                                user: dict = Depends(require_auth)) -> dict:
    """
    The build's progress for the Job Introspection tab, recorded by the
    builder (job_introspection.Recorder): its phases, sentences, test set,
    kiro-cli sessions and cost, and the latest verified theory. `available`
    is False for jobs with no recorded progress.
    """
    view = store.job_introspection(job_id)
    if view is None:
        raise HTTPException(status_code=404, detail="job not found")
    return view


@app.get("/api/jobs/{job_id}/introspection/logs")
async def api_job_build_logs(job_id: str, head: int = 500, tail: int = 1500,
                             user: dict = Depends(require_auth)) -> dict:
    """
    The build's log from its builder task's stream, for display: its first
    `head` lines and newest `tail` lines, with an omitted-lines marker (and
    the kiro-cli milestones) between them.
    """
    if store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    head = max(0, min(int(head), 5000))
    tail = max(1, min(int(tail), 10000))
    return store.build_logs(job_id, head=head, tail=tail)


@app.get("/api/jobs/{job_id}/introspection/kiro")
async def api_job_kiro_log(job_id: str, cursor: str = "", limit: int = 2000,
                           user: dict = Depends(require_auth)) -> dict:
    """
    The live kiro-cli view: the build's kiro-cli transcript lines (and the
    harness's verdicts between sessions) newer than `cursor`. The first call
    (no cursor) returns the newest `limit` lines; each response carries the
    cursor for the next call, so a refresh reads only what is new.
    """
    if store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    limit = max(1, min(int(limit), 5000))
    return store.kiro_log_tail(job_id, cursor=cursor or None, limit=limit)


@app.get("/api/jobs/{job_id}/introspection/logs.txt")
async def api_job_build_logs_download(job_id: str,
                                      user: dict = Depends(require_auth)):
    """
    Every line of the build's log as a downloadable text file ("Download all
    lines"). Streamed page by page from CloudWatch, so a long log never waits
    out the CloudFront timeout.
    """
    if store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    filename, chunks = store.build_log_download(job_id)
    if filename is None:
        raise HTTPException(status_code=404, detail=chunks)
    return StreamingResponse(
        iterate_in_threadpool(chunks), media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/jobs/{job_id}/logs.html", response_class=HTMLResponse)
async def api_get_job_logs_html(job_id: str, request: Request, limit: int = 2000):
    """
    Human-friendly HTML view of a job's log, for the "Open in new window" link.

    The JSON endpoint above is for the frontend; opening it directly in a
    browser tab renders the raw JSON (an escaped `lines` array with literal
    `\\n`), which is unreadable. This route returns the same log text wrapped in
    a monospace `<pre>` block so newlines render correctly.

    This is a top-level browser navigation, not an XHR, so we authenticate from
    the session cookie and REDIRECT an unauthenticated viewer to the Hosted UI
    login (returning here) instead of surfacing a bare 401 page.
    """
    if auth.current_user(request) is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    limit = max(1, min(int(limit), 10000))
    result = store.job_logs(job_id, limit=limit)

    title = job.get("theory_name") or job.get("theory_id") or job_id
    status = job.get("status", "")
    body = "\n".join(result.get("lines") or []) or "(no log)"
    note = result.get("note")
    truncated = result.get("truncated")

    meta_bits = [f"status: {status}", f"source: {result.get('source', '')}"]
    if truncated:
        meta_bits.append("truncated to most recent lines")
    meta = " · ".join(meta_bits)

    page = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f"<title>Logs — {html.escape(str(title))}</title>\n"
        "<style>\n"
        "  :root { color-scheme: light dark; }\n"
        "  body { margin: 0; font-family: ui-sans-serif, system-ui, sans-serif;\n"
        "         background: #0b0f14; color: #e6edf3; }\n"
        "  header { padding: 12px 16px; border-bottom: 1px solid #30363d;\n"
        "           background: #11161d; position: sticky; top: 0; }\n"
        "  header h1 { font-size: 14px; margin: 0 0 2px; font-weight: 600; }\n"
        "  header .meta { font-size: 12px; color: #8b949e; }\n"
        "  header .note { font-size: 12px; color: #d29922; margin-top: 4px; }\n"
        "  pre { margin: 0; padding: 16px; white-space: pre-wrap;\n"
        "        word-break: break-word; font-family: ui-monospace, SFMono-Regular,\n"
        "        Menlo, Consolas, monospace; font-size: 12.5px; line-height: 1.5; }\n"
        "</style></head><body>\n"
        f"<header><h1>{html.escape(str(title))}</h1>\n"
        f'<div class="meta">{html.escape(meta)}</div>\n'
        + (f'<div class="note">{html.escape(str(note))}</div>\n' if note else "")
        + "</header>\n"
        f"<pre>{html.escape(body)}</pre>\n"
        "</body></html>\n"
    )
    return HTMLResponse(content=page)


@app.post("/api/jobs/{job_id}/kill")
async def api_kill_job(job_id: str,
                       user: dict = Depends(require_auth)) -> dict:
    """
    Cancel a queued or running build job.

    Sets the job's cooperative-cancel flag (the inline runner stops at its next
    checkpoint and tears its worker pool down) and flips the job record to
    `cancelled` immediately. 404 when the job id is unknown. Killing an
    already-finished/failed/cancelled job is a no-op that returns its state.
    """
    result = store.kill_job(job_id)
    if not result.get("ok") and result.get("note") == "job not found":
        raise HTTPException(status_code=404, detail="job not found")
    return result


# --- Static SPA ----------------------------------------------------------
# Serve the production React build. Any non-API path falls back to index.html
# so client-side routing works.
_SPA_DIR = os.path.join(os.path.dirname(__file__), "frontend", "build")

if os.path.isdir(_SPA_DIR):
    app.mount(
        "/static",
        StaticFiles(directory=os.path.join(_SPA_DIR, "static")),
        name="static",
    )

    @app.get("/")
    async def spa_root(request: Request):
        # The HTML shell is private: bounce unauthenticated viewers straight
        # to the Cognito Hosted UI so they never see the app chrome.
        if auth.current_user(request) is None:
            return RedirectResponse("/auth/login")
        return FileResponse(os.path.join(_SPA_DIR, "index.html"))

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str, request: Request):
        # Real build files (JS/CSS/favicon/manifest) are served as-is so the
        # authenticated page can load its assets. Anything else resolves to
        # the SPA shell, which is auth-gated like '/'.
        candidate = os.path.join(_SPA_DIR, full_path)
        if os.path.isfile(candidate):
            return FileResponse(candidate)
        if auth.current_user(request) is None:
            return RedirectResponse("/auth/login")
        return FileResponse(os.path.join(_SPA_DIR, "index.html"))
