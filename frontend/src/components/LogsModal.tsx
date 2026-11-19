import React, { useEffect, useState } from 'react';
import { Job, JobStatus } from '../types';
import { fetchJob, fetchJobLogs } from '../api';

interface LogsModalProps {
  job: Job;
  onClose: () => void;
}

/**
 * Modal dialog showing a job's full log.
 *
 * The log text comes from GET /api/jobs/{id}/logs, which tails this job's
 * lines out of the web task's CloudWatch log stream (the inline runner shares
 * the task process). That endpoint degrades to the job's stored status line
 * when CloudWatch is unconfigured/unreachable; `source` tells us which, and we
 * surface a small note when the content is the degraded fallback.
 *
 * The job's status/error are refreshed from GET /api/jobs/{id} in the same
 * tick. While the job is queued/running the dialog keeps polling every few
 * seconds so the log grows live.
 */
export function LogsModal({ job, onClose }: LogsModalProps) {
  const [lines, setLines] = useState<string[]>(
    job.log_tail ? job.log_tail.split('\n') : []
  );
  const [source, setSource] = useState<string>('log_tail');
  const [note, setNote] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [status, setStatus] = useState<JobStatus>(job.status);
  const [error, setError] = useState<string | null>(job.error);
  const [loading, setLoading] = useState(true);

  // Close on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Fetch the full log + fresh job status on open, then poll while in flight.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const load = async () => {
      try {
        const [logs, fresh] = await Promise.all([
          fetchJobLogs(job.id),
          fetchJob(job.id),
        ]);
        if (cancelled) return;
        setLines(logs.lines);
        setSource(logs.source);
        setNote(logs.note);
        setTruncated(logs.truncated);
        setStatus(fresh.status);
        setError(fresh.error);
        if (fresh.status === 'queued' || fresh.status === 'running') {
          timer = setTimeout(load, 4000);
        }
      } catch {
        if (!cancelled) setNote((n) => n ?? 'Could not refresh logs.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [job.id]);

  const title = job.theory_name ?? job.theory_id ?? job.id;
  const body = lines.length ? lines.join('\n') : '(no log)';
  const degraded = source === 'log_tail';

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Logs for ${title}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div className="modal-title">
            <span className={`status-pill status-${status}`}>{status}</span>
            <span className="job-name">{title}</span>
            {source === 'cloudwatch' && (
              <span className="muted small">CloudWatch</span>
            )}
            {loading && <span className="muted small">refreshing…</span>}
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        {error && <p className="error-text">Error: {error}</p>}
        {degraded && note && <p className="muted small">{note}</p>}
        <pre className="modal-log">{body}</pre>
        {truncated && (
          <p className="muted small">
            Log truncated to the most recent lines.
          </p>
        )}
      </div>
    </div>
  );
}
