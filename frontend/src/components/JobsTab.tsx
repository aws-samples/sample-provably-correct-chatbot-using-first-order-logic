import React, { useState } from 'react';
import { Job } from '../types';
import { API_BASE_URL, killJob } from '../api';
import { LogsModal } from './LogsModal';

interface JobsTabProps {
  jobs: Job[];
  // Open (or switch to) the Job Introspection tab for this job.
  onIntrospect?: (jobId: string) => void;
}

/**
 * Tab 4: current and finished jobs. A "job" is one WikiLogifier run triggered
 * by adding a theory in tab 3. The parent polls while any job is in flight, so
 * this list updates on its own.
 *
 * Each row carries a "View logs" link that opens the full log (tailed from
 * CloudWatch) in a modal dialog, plus an "Open in new window" link that loads
 * the log as a formatted HTML page (GET /api/jobs/{id}/logs.html, a <pre>
 * block so newlines render) in a separate browser tab.
 */
export function JobsTab({ jobs, onIntrospect }: JobsTabProps) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [logsJob, setLogsJob] = useState<Job | null>(null);
  // Jobs for which a kill request is in flight, so the button disables and
  // shows progress until the parent's poll reflects the `cancelled` status.
  const [killing, setKilling] = useState<Set<string>>(new Set());

  const onKill = async (j: Job, e: React.MouseEvent) => {
    e.stopPropagation();
    if (killing.has(j.id)) return;
    const label = j.theory_name ?? j.theory_id ?? j.id;
    if (!window.confirm(`Kill the build job for "${label}"? This stops the build; the theory will be marked failed.`)) {
      return;
    }
    setKilling((s) => new Set(s).add(j.id));
    try {
      await killJob(j.id);
      // The parent polls jobs while any is in flight and will pick up the
      // `cancelled` status; nothing else to do here.
    } catch {
      // Surface a minimal error; the row stays as-is and the user can retry.
      window.alert('Could not kill the job. It may have already finished.');
    } finally {
      setKilling((s) => {
        const next = new Set(s);
        next.delete(j.id);
        return next;
      });
    }
  };

  return (
    <div className="jobs-tab">
      <h3>Jobs</h3>
      {jobs.length === 0 && (
        <p className="muted">No jobs yet. Adding a theory starts one.</p>
      )}
      {/* One grid for the header and every row (rows join it via subgrid),
          so each column is as wide as its widest cell and they line up. */}
      <div className="jobs-grid">
      {jobs.length > 0 && (
        <div className="jobs-header">
          <span className="col-status">Status</span>
          <span className="col-name">Theory</span>
          <span className="col-progress">Progress</span>
          <span className="col-actions">Actions</span>
          <span className="col-duration">Duration</span>
        </div>
      )}
      <ul className="jobs-list">
        {jobs.map((j) => (
          <li key={j.id} className="job-row">
            <div
              className="job-head"
              onClick={() => setExpanded((e) => (e === j.id ? null : j.id))}
            >
              <span className={`status-pill status-${j.status}`}>{j.status}</span>
              <span className="job-name">
                {onIntrospect ? (
                  <button
                    type="button"
                    className="link-button job-introspect-link"
                    title="Open Job Introspection for this job"
                    onClick={(e) => {
                      e.stopPropagation();
                      onIntrospect(j.id);
                    }}
                  >
                    {j.theory_name ?? j.theory_id}
                  </button>
                ) : (
                  j.theory_name ?? j.theory_id
                )}
              </span>
              <span className="job-progress">{j.progress}</span>
              <span className="job-actions">
                <button
                  type="button"
                  className="link-button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setLogsJob(j);
                  }}
                >
                  View logs
                </button>
                <a
                  className="link-button"
                  href={`${API_BASE_URL}/api/jobs/${encodeURIComponent(j.id)}/logs.html`}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={(e) => e.stopPropagation()}
                >
                  Open in new window
                </a>
                {(j.status === 'queued' || j.status === 'running') && (
                  <button
                    type="button"
                    className="link-button kill-button"
                    disabled={killing.has(j.id)}
                    onClick={(e) => onKill(j, e)}
                  >
                    {killing.has(j.id) ? 'Killing…' : 'Kill'}
                  </button>
                )}
              </span>
              <span className="job-time">{duration(j)}</span>
            </div>
            {expanded === j.id && (
              <div className="job-detail">
                {j.error && <p className="error-text">Error: {j.error}</p>}
                {j.build_params && (
                  <p className="muted small job-params">
                    Build parameters:{' '}
                    {Object.entries(j.build_params)
                      .map(([k, v]) => `${k}=${v === '' ? '(default)' : v}`)
                      .join(' · ')}
                  </p>
                )}
                <pre className="job-log">{j.log_tail || '(no log)'}</pre>
                <p className="muted small">
                  Created {formatTime(j.created_at)}
                  {j.started_at ? ` · started ${formatTime(j.started_at)}` : ''}
                  {j.finished_at ? ` · finished ${formatTime(j.finished_at)}` : ''}
                </p>
              </div>
            )}
          </li>
        ))}
      </ul>
      </div>

      {logsJob && (
        <LogsModal job={logsJob} onClose={() => setLogsJob(null)} />
      )}
    </div>
  );
}

function formatTime(epochSeconds: number | null): string {
  if (!epochSeconds) return '—';
  return new Date(epochSeconds * 1000).toLocaleString();
}

function duration(j: Job): string {
  const start = j.started_at ?? j.created_at;
  const end = j.finished_at ?? Math.floor(Date.now() / 1000);
  if (!start) return '';
  const secs = Math.max(0, end - start);
  if (secs < 60) return `${secs}s`;
  if (secs >= 3600) {
    const hrs = Math.floor(secs / 3600);
    const mins = Math.floor((secs % 3600) / 60);
    return `${hrs}h ${mins}m`;
  }
  const mins = Math.floor(secs / 60);
  return `${mins}m ${secs % 60}s`;
}
