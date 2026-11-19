// API client for the chatbot backend.
// Base URL comes from REACT_APP_API_BASE_URL, defaulting to localhost (empty in
// the deployed image, so calls are same-origin).

import {
  TheorySummary,
  TheoryDetail,
  Job,
  JobLogs,
  DocType,
  SamplePatient,
  JobIntrospection,
  BuildParams,
  BuildParamSpec,
} from './types';

export const API_BASE_URL =
  process.env.REACT_APP_API_BASE_URL ?? 'http://localhost:8000';

export interface SubjectResponse {
  title: string;
}

export interface AskResponse {
  answer: string;
}

export interface LuckyResponse {
  question: string;
}

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    credentials: 'include', // send the app session cookie
    ...init,
  });
  if (res.status === 401) {
    // Session missing or expired: bounce to the Cognito Hosted UI login.
    window.location.href = `${API_BASE_URL}/auth/login`;
    // Never resolves; the navigation above replaces the page.
    return new Promise<T>(() => {});
  }
  if (!res.ok) {
    // Surface FastAPI's {"detail": "..."} (e.g. an out-of-range build
    // parameter) so callers can show why a request was refused.
    let detail = '';
    try {
      const body = await res.json();
      if (body && typeof body.detail === 'string') detail = body.detail;
    } catch {
      // not JSON; fall back to the status line
    }
    throw new Error(detail || `Request to ${path} failed with status ${res.status}`);
  }
  return (await res.json()) as T;
}

export function fetchSubject(theoryId?: string): Promise<SubjectResponse> {
  const q = theoryId ? `?theory_id=${encodeURIComponent(theoryId)}` : '';
  return getJson<SubjectResponse>(`/subject${q}`);
}

export function askQuestion(text: string, theoryId?: string): Promise<AskResponse> {
  return getJson<AskResponse>('/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, theory_id: theoryId ?? '' }),
  });
}

export function fetchLuckyQuestion(theoryId?: string): Promise<LuckyResponse> {
  const q = theoryId ? `?theory_id=${encodeURIComponent(theoryId)}` : '';
  return getJson<LuckyResponse>(`/lucky${q}`);
}

export interface LuckyQuestionsResponse {
  questions: string[];
}

// Theory-specific, pre-validated questions for the "I'm Feeling Lucky" picker.
// Unlike /lucky (which generates one fresh question via Bedrock), this returns
// the loaded theory's stored suggested questions cheaply.
export function fetchLuckyQuestions(theoryId?: string): Promise<LuckyQuestionsResponse> {
  const q = theoryId ? `?theory_id=${encodeURIComponent(theoryId)}` : '';
  return getJson<LuckyQuestionsResponse>(`/lucky-questions${q}`);
}

// --- Theories + jobs (tabs 2/3/4) ---------------------------------------

export function fetchTheories(): Promise<{ theories: TheorySummary[] }> {
  return getJson('/api/theories');
}

export function fetchTheory(id: string): Promise<TheoryDetail> {
  return getJson(`/api/theories/${encodeURIComponent(id)}`);
}

// Delete a theory. Removes its backend record (and best-effort its S3
// artifacts) so it disappears from the Theories tab. Returns
// {ok, deleted?} / {note?} describing the outcome.
export function deleteTheory(
  id: string
): Promise<{ ok?: boolean; deleted?: string; note?: string }> {
  return getJson(`/api/theories/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

// Rebuild an existing theory from its stored source document. Creates a FRESH
// theory + job (visible in the Jobs tab) via the normal build path. Returns the
// new {theory, job} on success.
export function rebuildTheory(
  id: string,
  buildParams?: BuildParams
): Promise<{ theory: TheorySummary; job: Job }> {
  return getJson(`/api/theories/${encodeURIComponent(id)}/rebuild`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ build_params: buildParams ?? {} }),
  });
}

// The parameters the Build parameters dialog offers, with their defaults.
export function fetchBuildParams(): Promise<{ params: BuildParamSpec[] }> {
  return getJson('/api/build-params');
}

export function createTheoryFromText(
  name: string,
  document: string,
  docType: DocType = 'auto',
  buildParams?: BuildParams
): Promise<{ theory: TheorySummary; job: Job }> {
  return getJson('/api/theories', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, document, doc_type: docType, build_params: buildParams ?? {} }),
  });
}

export function createTheoryFromFile(
  name: string,
  file: File,
  docType: DocType = 'auto',
  buildParams?: BuildParams
): Promise<{ theory: TheorySummary; job: Job }> {
  const form = new FormData();
  form.append('name', name);
  form.append('file', file);
  form.append('doc_type', docType);
  form.append('build_params', JSON.stringify(buildParams ?? {}));
  // No Content-Type header: the browser sets the multipart boundary.
  return getJson('/api/theories', { method: 'POST', body: form });
}

// The baked-in Synthea sample patients the "Add theory" form can offer as a
// FHIR source instead of a pasted/uploaded document.
export function fetchSamplePatients(): Promise<{ sample_patients: SamplePatient[] }> {
  return getJson('/api/sample-patients');
}

// Build a theory from one of the baked-in sample patients. The backend loads
// that patient's FHIR Bundle server-side and builds it as doc_type "fhir";
// `name` is optional and defaults to the patient's name.
export function createTheoryFromSample(
  samplePatientId: string,
  name?: string,
  buildParams?: BuildParams
): Promise<{ theory: TheorySummary; job: Job }> {
  return getJson('/api/theories', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      sample_patient_id: samplePatientId,
      name: name ?? '',
      build_params: buildParams ?? {},
    }),
  });
}

// Build a theory from a web page URL. The backend downloads the page and
// extracts its prose into sentences server-side (via the Wikipedia-focused
// HTML parser), then builds it as doc_type "text". The parser is tuned for
// Wikipedia articles; a non-Wikipedia URL is accepted but may extract poorly.
// `name` is optional and defaults to the URL server-side.
export function createTheoryFromWikipedia(
  wikipediaUrl: string,
  name?: string,
  buildParams?: BuildParams
): Promise<{ theory: TheorySummary; job: Job }> {
  return getJson('/api/theories', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      wikipedia_url: wikipediaUrl,
      name: name ?? '',
      build_params: buildParams ?? {},
    }),
  });
}

// True if `url` is an http(s) URL whose host is wikipedia.org or a subdomain
// (e.g. en.wikipedia.org). The "Add theory" form uses this to decide whether
// to show the red "likely won't parse" warning. Mirrors store.is_wikipedia_url
// on the backend. Returns false for anything that is not a parseable http(s)
// URL (so a half-typed value does not flash a warning prematurely).
export function isWikipediaUrl(url: string): boolean {
  let host: string;
  try {
    const u = new URL(url.trim());
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
    host = u.hostname.toLowerCase();
  } catch {
    return false;
  }
  return host === 'wikipedia.org' || host.endsWith('.wikipedia.org');
}

export function fetchJobs(): Promise<{ jobs: Job[] }> {
  return getJson('/api/jobs');
}

export function fetchJob(id: string): Promise<Job> {
  return getJson(`/api/jobs/${encodeURIComponent(id)}`);
}

// Full job log, tailed from CloudWatch (falls back to the stored status line
// server-side; see JobLogs.source).
export function fetchJobLogs(id: string, limit = 2000): Promise<JobLogs> {
  return getJson(`/api/jobs/${encodeURIComponent(id)}/logs?limit=${limit}`);
}

// The progress recorded for a build (Job Introspection tab): phases,
// sessions, cost, the latest verified theory and the test set.
export function fetchJobIntrospection(id: string): Promise<JobIntrospection> {
  return getJson(`/api/jobs/${encodeURIComponent(id)}/introspection`);
}

// The build's log from the builder's CloudWatch stream: its first `head`
// lines and newest `tail` lines, with an omitted-lines marker.
export function fetchBuildLogs(id: string, head = 500, tail = 1500): Promise<JobLogs> {
  return getJson(
    `/api/jobs/${encodeURIComponent(id)}/introspection/logs?head=${head}&tail=${tail}`
  );
}

// Download URL for EVERY line of the build's log, as a .log text file.
export function buildLogsDownloadUrl(id: string): string {
  return `${API_BASE_URL}/api/jobs/${encodeURIComponent(id)}/introspection/logs.txt`;
}

// Kill a queued or running build job. The backend stops the builder task (or,
// for an inline build, sets the cooperative-cancel flag, which also ends a
// running kiro-cli session) and flips the job to `cancelled`. Returns the
// refreshed job.
export function killJob(id: string): Promise<{ ok?: boolean; note?: string; job?: Job }> {
  return getJson(`/api/jobs/${encodeURIComponent(id)}/kill`, { method: 'POST' });
}

// The backend returns this literal string when it cannot prove an answer.
export const UNKNOWN_ANSWER = "I don't know";

export function isVerifiedAnswer(answer: string): boolean {
  return answer.trim() !== UNKNOWN_ANSWER;
}
