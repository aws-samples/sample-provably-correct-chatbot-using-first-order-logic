// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Message model for the chat transcript.

export type MessageKind = 'user' | 'bot' | 'pending' | 'error';

export interface Message {
  id: number;
  kind: MessageKind;
  text: string;
  // Set on 'bot' messages: whether the answer is theorem-prover verified
  // (i.e. not the "I don't know" fallback). Undefined for other kinds.
  verified?: boolean;
}

// --- Theories + jobs (tabs 2/3/4) ---------------------------------------

export type TheoryStatus = 'building' | 'ready' | 'failed' | 'cancelled';

// The source document's type. 'auto' is a UI-only request value; the backend
// resolves it to 'text' or 'fhir' and stores the concrete type.
export type DocType = 'text' | 'fhir' | 'auto';

// A baked-in Synthea sample patient offered by the "Add theory" form as a FHIR
// source. `id` is the Bundle's filename stem; `name` is "First Last".
export interface SamplePatient {
  id: string;
  name: string;
}

export interface TheorySummary {
  id: string;
  name: string;
  status: TheoryStatus;
  sentence_count: number;
  created_at: number; // epoch seconds
  job_id: string | null;
  doc_type: 'text' | 'fhir';
  // Source provenance shown in the type column INSTEAD of doc_type when set
  // (e.g. a Wikipedia page builds as text but is surfaced as "wiki").
  origin?: string | null;
}

export interface TheoryDetail extends TheorySummary {
  source_document: string;
  // The SMT-LIB theory statements (what cvc5 consumes), extracted from the
  // stored theory YAML by the backend.
  computed_theory: string;
  // The raw theory YAML (the build's result), available for anyone who wants it.
  computed_theory_yaml?: string;
  // The "suggested questions" (the test-set questions the build
  // verified), surfaced as a [Q&As] artifact. Empty until the build is ready.
  suggested_questions?: string[];
  // The same questions with their answers: a test-set question's expected
  // answer (what the theory was verified against), or the answer the chatbot
  // proved for a generated variation. answer is null when none is known.
  suggested_qas?: { question: string; answer: string | null; source: 'test set' | 'variation' }[];
  subject_id: string | null;
  // The individual sentences the source document was split into (one per line
  // in the sentence-list modal). Empty for FHIR theories.
  sentences?: string[];
  // The decontextualized ("standalone") version of each sentence, index-
  // aligned 1:1 with `sentences`: pronouns/references resolved (e.g.
  // "It is ..." -> "New Zealand is ..."). Shown beside the original in the
  // sentences table. Falls back to equalling `sentences` when a theory has no
  // distinct standalone rewrite. Empty for FHIR theories.
  standalone_sentences?: string[];
}

export type JobStatus = 'queued' | 'running' | 'finished' | 'failed' | 'cancelled';

export interface Job {
  id: string;
  theory_id: string | null;
  theory_name: string | null;
  status: JobStatus;
  progress: string;
  log_tail: string;
  error: string | null;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  // The parameters the build ran with (null for jobs started before the
  // Build parameters dialog existed).
  build_params?: BuildParams | null;
}

// --- Build parameters dialog ---------------------------------------------

export type BuildParamValue = number | string;
export type BuildParams = Record<string, BuildParamValue>;

// One field of the Build parameters dialog (GET /api/build-params).
export interface BuildParamSpec {
  key: string;
  label: string;
  group: string;
  type: 'int' | 'float' | 'choice' | 'text';
  default: BuildParamValue;
  help: string;
  min?: number;
  max?: number;
  step?: number;
  choices?: string[];
}

// Response of GET /api/jobs/{id}/logs. `source` says where the lines came from:
// 'cloudwatch' = the real tailed pipeline output; 'log_tail' = the stored
// status line (CloudWatch unconfigured/unreachable or no lines yet);
// 'unavailable' = the job id is unknown. `note` explains any degraded source.
export type JobLogSource = 'cloudwatch' | 'log_tail' | 'unavailable';

export interface JobLogs {
  source: JobLogSource;
  lines: string[];
  truncated: boolean;
  note: string | null;
  // Build logs only: lines in the whole log, and how many the head-and-tail
  // view skipped (marked by an "… N lines omitted …" line).
  total_lines?: number;
  omitted?: number;
}

// --- Job introspection (the progress of one build) -------------------------

// Recorded by the builder (cdk/docker/job_introspection.py). A build is one
// unit of work: load the sentences, generate the test set, then one or more
// kiro-cli sessions until the theory verifies or the budget runs out.
export type BuildStatus = 'running' | 'succeeded' | 'failed';
export type BuildPhase = 'sentences' | 'test_set' | 'theory' | 'suggested_questions' | 'done';
// build = the first session; feedback = resumed with the harness's
// verification report; stall_resume = resumed after the stall watchdog.
export type SessionKind = 'build' | 'feedback' | 'stall_resume';

export interface PhaseTime {
  started_at: number;
  ended_at: number | null;
  // What a finished phase spent: Bedrock (estimated from token counts) and
  // kiro-cli credits. Absent while it runs and for older builds.
  bedrock_dollars?: number;
  kiro_credits?: number;
}

export interface SessionRecord {
  session: number;
  kind: SessionKind;
  status: 'running' | 'done';
  started_at: number | null;
  finished_at: number | null;
  duration: number | null;
  rc: number | null;
  stalled: boolean | null;
  // null when the session reported no usage (killed or crashed).
  credits: number | null;
  // The harness's re-verification after the session.
  valid: boolean | null;
  n_correct: number | null;
}

export interface IntrospectionState {
  version: number;
  job_id: string;
  updated_at: number;
  status: BuildStatus;
  phase: BuildPhase;
  started_at: number | null;
  finished_at: number | null;
  duration: number | null;
  // When each phase started and ended (ended_at null while it runs). Absent
  // for builds recorded before phase times were kept.
  phase_times?: Partial<Record<Exclude<BuildPhase, 'done'>, PhaseTime>>;
  n_sentences: number | null;
  // What the agent is given (for text, the de-contextualized sentences).
  sentences: string[];
  // Each sentence as written, and its de-contextualized rewrite (null where
  // there is none: FHIR entries get their context attached structurally).
  // Absent for jobs recorded before they were kept.
  raw_sentences?: string[];
  standalone_sentences?: (string | null)[];
  n_questions: number | null;
  n_required: number | null;
  // Questions the latest valid theory answered correctly (as re-verified).
  n_correct: number | null;
  theory_chars: number | null;
  sessions: SessionRecord[];
}

export interface KiroCostTotals {
  credits: number;
  dollars: number;
  sessions: number;
  unmetered_sessions: number;
}

export interface KiroCost extends KiroCostTotals {
  dollars_per_credit: number;
  per_session: { session: number; credits: number | null; dollars: number | null }[];
}

export interface TestQuestion {
  question: string;
  expected_answer: string;
  // Whether the latest verification credited it. Verification stops once
  // enough questions pass, so false can also mean "not checked".
  correct: boolean;
}

export interface TestSet {
  n_required: number | null;
  questions: TestQuestion[];
}

export interface JobIntrospection {
  job: Job;
  available: boolean;
  state: IntrospectionState | null;
  // The latest re-verified theory (the final one once the build ends).
  theory: string | null;
  qas: TestSet | null;
  note: string | null;
  // kiro-cli spend; null before any session finished.
  cost: KiroCost | null;
  // $ per kiro-cli credit, for pricing phase spend.
  dollars_per_credit?: number;
}

// Response of GET /api/jobs/{id}/introspection/kiro: the build's kiro-cli
// transcript lines ("kc: ...") and the harness's verdicts between sessions
// ("kcvut: ...") newer than the cursor sent.
export interface KiroLog {
  lines: string[];
  // Pass back on the next call to get only newer lines (null until any line).
  cursor: string | null;
  // Older lines not returned on a first load (beyond its limit).
  omitted: number;
  // The read hit its page/time cap; the next call carries on from the cursor.
  incomplete: boolean;
  note: string | null;
}
