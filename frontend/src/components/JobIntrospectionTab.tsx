import React, { useEffect, useState } from 'react';
import {
  BuildPhase,
  IntrospectionState,
  Job,
  JobIntrospection,
  JobLogs,
  SessionKind,
  SessionRecord,
  TestSet,
} from '../types';
import { buildLogsDownloadUrl, fetchBuildLogs, fetchJobIntrospection } from '../api';
import { formatCost, formatDuration, formatSessionCost } from '../kiroCost';

interface JobIntrospectionTabProps {
  jobs: Job[];
  // The job this tab introspects. Each job gets its own "Job Introspection
  // (name)" tab, opened by clicking the job's name in the Jobs tab.
  jobId: string;
}

export type BuildView = 'sentences' | 'qas' | 'theory' | 'logs';

const VIEWS: { key: BuildView; label: string }[] = [
  { key: 'sentences', label: 'Sentences' },
  { key: 'qas', label: 'Test set' },
  { key: 'theory', label: 'Theory' },
  { key: 'logs', label: 'Logs' },
];

const PHASES: { key: BuildPhase; label: string }[] = [
  { key: 'sentences', label: 'Load sentences' },
  { key: 'test_set', label: 'Generate test set' },
  { key: 'theory', label: 'Build theory' },
  { key: 'done', label: 'Done' },
];

const SESSION_KINDS: Record<SessionKind, string> = {
  build: 'first build',
  feedback: 'fix after verification',
  stall_resume: 'resume after a stall',
};

const POLL_MS = 5000;
// The Logs view shows the build's first LOG_HEAD lines and its newest
// LOG_TAIL lines; Download all lines gets everything.
const LOG_HEAD = 500;
const LOG_TAIL = 1500;

function isLive(job: Job | undefined): boolean {
  return job?.status === 'queued' || job?.status === 'running';
}

function jobLabel(j: Job): string {
  const when = new Date((j.started_at ?? j.created_at) * 1000).toLocaleString();
  return `${j.theory_name ?? j.theory_id ?? j.id} · ${j.status} · ${when}`;
}

/**
 * The status shown for a build: the recorded one, except that a build whose
 * job ended while it was still running (killed, or the builder died) is
 * "stopped".
 */
export function buildStatus(state: IntrospectionState, job: Job): string {
  if (state.status === 'running' && !isLive(job)) return 'stopped';
  return state.status;
}

/** e.g. "41/50 verified (40 needed)" once a session has been verified. */
export function verifiedLine(state: IntrospectionState): string | null {
  if (state.n_questions == null) return null;
  const needed = state.n_required != null ? ` (${state.n_required} needed)` : '';
  if (state.n_correct == null) return `${state.n_questions} test questions${needed}`;
  return `${state.n_correct}/${state.n_questions} verified${needed}`;
}

/**
 * A per-job tab: the progress of one build. A build formalizes the whole
 * document in one unit of work, so the tab shows its phases, each kiro-cli
 * session (with its cost and how many test questions the harness verified
 * after it), and four views: the sentences, the test set, the latest
 * verified theory and the build log. A running build refreshes every few
 * seconds.
 */
export function JobIntrospectionTab({ jobs, jobId }: JobIntrospectionTabProps) {
  const [data, setData] = useState<JobIntrospection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [view, setView] = useState<BuildView>('sentences');

  const selected = jobs.find((j) => j.id === jobId) ?? data?.job;
  const live = isLive(selected);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    setData(null);
    const load = async () => {
      setLoading(true);
      try {
        const d = await fetchJobIntrospection(jobId);
        if (cancelled) return;
        setData(d);
        setError(null);
        if (isLive(d.job)) timer = setTimeout(load, POLL_MS);
      } catch {
        if (!cancelled) setError('Could not load this job’s progress.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId]);

  const state = data?.state ?? null;
  const cost = data?.cost ?? null;
  // An unfinished build's duration runs to now while the job is live (the tab
  // re-renders on every poll), else to the last recorded update.
  const nowSecs =
    data && !isLive(data.job) ? state?.updated_at ?? Date.now() / 1000 : Date.now() / 1000;

  return (
    <div className="introspection-tab">
      <div className="introspection-head">
        <h3>Job Introspection</h3>
        <span className="introspection-job-label">
          {selected ? jobLabel(selected) : jobId}
        </span>
        {loading && <span className="muted small">refreshing…</span>}
        {live && !loading && <span className="muted small">live · updates every 5s</span>}
      </div>

      {error && <p className="error-text">{error}</p>}
      {data && !data.available && <p className="muted">{data.note}</p>}

      {data && state && (
        <>
          <BuildSummary state={state} job={data.job} costText={cost && formatCost(cost)}
            nowSecs={nowSecs} />
          <SessionsTable
            sessions={state.sessions}
            nRequired={state.n_required}
            dollarsPerCredit={cost?.dollars_per_credit ?? null}
            nowSecs={nowSecs}
          />
          <div className="unit-view-tabs" role="tablist" aria-label="Build views">
            {VIEWS.map((v) => (
              <button
                key={v.key}
                type="button"
                role="tab"
                id={`build-view-${v.key}`}
                aria-selected={view === v.key}
                aria-controls="build-view-panel"
                className={view === v.key ? 'unit-view-tab active' : 'unit-view-tab'}
                onClick={() => setView(v.key)}
              >
                {v.label}
              </button>
            ))}
          </div>
          <div
            id="build-view-panel"
            role="tabpanel"
            aria-labelledby={`build-view-${view}`}
            className="build-view-panel"
          >
            {view === 'sentences' && <SentencesView sentences={state.sentences} />}
            {view === 'qas' && <TestSetView qas={data.qas} />}
            {view === 'theory' && <TheoryView theory={data.theory} state={state} />}
            {view === 'logs' && <LogsView jobId={data.job.id} live={live} />}
          </div>
        </>
      )}
    </div>
  );
}

function BuildSummary({
  state,
  job,
  costText,
  nowSecs,
}: {
  state: IntrospectionState;
  job: Job;
  costText: string | null;
  nowSecs: number;
}) {
  const status = buildStatus(state, job);
  const current = PHASES.findIndex((p) => p.key === state.phase);
  const elapsed =
    state.started_at != null
      ? formatDuration((state.finished_at ?? nowSecs) - state.started_at)
      : null;
  const facts = [
    state.n_sentences != null ? `${state.n_sentences} sentences` : null,
    verifiedLine(state),
    elapsed ? `duration: ${elapsed}` : null,
    costText ? `kiro spend: ${costText}` : null,
  ].filter(Boolean);
  return (
    <section className="build-summary" aria-label="Build summary">
      <div className="build-summary-row">
        <span className={`status-pill unit-status-${status}`}>{status}</span>
        <ol className="build-phases" aria-label="Build phases">
          {PHASES.map((p, i) => {
            const done = i < current || state.phase === 'done';
            const here = i === current && state.phase !== 'done';
            return (
              <li
                key={p.key}
                className={done ? 'build-phase done' : here ? 'build-phase current' : 'build-phase'}
                aria-current={here ? 'step' : undefined}
              >
                {p.label}
              </li>
            );
          })}
        </ol>
      </div>
      <p className="muted small build-facts">{facts.join(' · ')}</p>
    </section>
  );
}

function SessionsTable({
  sessions,
  nRequired,
  dollarsPerCredit,
  nowSecs,
}: {
  sessions: SessionRecord[];
  nRequired: number | null;
  dollarsPerCredit: number | null;
  nowSecs: number;
}) {
  if (sessions.length === 0) {
    return <p className="muted small">No kiro-cli session has started yet.</p>;
  }
  return (
    <table className="unit-qas-table build-sessions">
      <caption className="visually-hidden">kiro-cli sessions</caption>
      <thead>
        <tr>
          <th scope="col">Session</th>
          <th scope="col">Kind</th>
          <th scope="col">Duration</th>
          <th scope="col">Cost</th>
          <th scope="col">Verified after it</th>
        </tr>
      </thead>
      <tbody>
        {sessions.map((s) => {
          const running = s.status === 'running';
          const secs =
            s.duration ?? (s.started_at != null ? (s.finished_at ?? nowSecs) - s.started_at : 0);
          let verified: string;
          if (running) verified = 'running…';
          else if (s.valid === false) verified = 'theory not valid (not SAT)';
          else verified = `${s.n_correct ?? 0}${nRequired != null ? `/${nRequired} needed` : ''}`;
          if (!running && s.stalled) verified += ' · stalled';
          return (
            <tr key={s.session}>
              <td>{s.session}</td>
              <td>{SESSION_KINDS[s.kind] ?? s.kind}</td>
              <td>{formatDuration(secs)}</td>
              <td>{running ? '—' : formatSessionCost(s.credits, dollarsPerCredit)}</td>
              <td>{verified}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function SentencesView({ sentences }: { sentences: string[] }) {
  if (sentences.length === 0) return <p className="muted">No sentences recorded yet.</p>;
  return (
    <pre className="modal-log artifact-body">
      {sentences.map((t, i) => `${i + 1}. ${t}`).join('\n')}
    </pre>
  );
}

function TestSetView({ qas }: { qas: TestSet | null }) {
  if (!qas || qas.questions.length === 0) {
    return <p className="muted">No test set yet: it is generated before the first kiro-cli session.</p>;
  }
  const passed = qas.questions.filter((q) => q.correct).length;
  return (
    <>
      <p className="muted small">
        {passed} of {qas.questions.length} credited correct by the latest verification
        {qas.n_required != null ? `, ${qas.n_required} needed` : ''}. Checking stops once
        enough pass, so an unmarked question may not have been checked.
      </p>
      <table className="unit-qas-table">
        <caption className="visually-hidden">Test questions and expected answers</caption>
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">Question</th>
            <th scope="col">Expected answer</th>
            <th scope="col">Credited</th>
          </tr>
        </thead>
        <tbody>
          {qas.questions.map((q, i) => (
            <tr key={i}>
              <td>{i + 1}</td>
              <td>{q.question}</td>
              <td>{q.expected_answer}</td>
              <td>
                {q.correct ? (
                  <span className="qa-correct">✓ correct</span>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function TheoryView({ theory, state }: { theory: string | null; state: IntrospectionState }) {
  if (!theory) {
    return (
      <p className="muted">
        No verified theory yet: one appears here once a kiro-cli session leaves a valid
        (satisfiable) theory.
      </p>
    );
  }
  const final = state.status === 'succeeded';
  return (
    <>
      <p className="muted small">
        {final
          ? 'The final theory.'
          : 'The latest valid theory the harness re-verified; it may still change.'}
      </p>
      <pre className="modal-log artifact-body">{theory}</pre>
    </>
  );
}

function LogsView({ jobId, live }: { jobId: string; live: boolean }) {
  const [logs, setLogs] = useState<JobLogs | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const load = async () => {
      try {
        const l = await fetchBuildLogs(jobId, LOG_HEAD, LOG_TAIL);
        if (!cancelled) {
          setLogs(l);
          setError(null);
        }
      } catch {
        if (!cancelled) setError('Could not load the build log.');
      } finally {
        if (!cancelled && live) timer = setTimeout(load, POLL_MS);
      }
    };
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId, live]);

  const total = logs?.total_lines ?? 0;
  const omitted = logs?.omitted ?? 0;
  let countNote = '';
  if (logs && total > 0) {
    if (omitted > 0) {
      const shown = total - omitted;
      const head = Math.min(LOG_HEAD, shown);
      countNote =
        `Showing the first ${head.toLocaleString()} and newest ` +
        `${(shown - head).toLocaleString()} of ${total.toLocaleString()} lines.`;
    } else {
      countNote = `${total.toLocaleString()} lines.`;
    }
  }
  return (
    <>
      <div className="unit-log-bar">
        <span className="muted small">{countNote}</span>
        <a className="link-button" href={buildLogsDownloadUrl(jobId)} download>
          Download all lines
        </a>
      </div>
      {error && <p className="error-text">{error}</p>}
      {logs?.note && <p className="muted small unit-note">{logs.note}</p>}
      <pre className="modal-log artifact-body">
        {logs ? (logs.lines.length ? logs.lines.join('\n') : '(no log lines)') : ''}
      </pre>
    </>
  );
}
