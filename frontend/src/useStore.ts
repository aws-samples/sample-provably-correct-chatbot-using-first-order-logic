import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchJobs, fetchTheories } from './api';
import { Job, TheorySummary } from './types';

export interface UseStore {
  theories: TheorySummary[];
  jobs: Job[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

const POLL_MS = 4000;

/**
 * Loads theories + jobs and polls while any job is still queued/running (so
 * tab 3's "building" theories flip to "ready" and tab 4's list stays live).
 * Polling stops once everything is terminal, and restarts on refresh().
 */
export function useStore(): UseStore {
  const [theories, setTheories] = useState<TheorySummary[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const [t, j] = await Promise.all([fetchTheories(), fetchJobs()]);
      setTheories(t.theories);
      setJobs(j.jobs);
      setError(null);
    } catch (e) {
      setError('Could not load theories/jobs.');
    } finally {
      setLoading(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    await load();
  }, [load]);

  useEffect(() => {
    let cancelled = false;

    const tick = async () => {
      if (cancelled) return;
      await load();
      if (cancelled) return;
      // Keep polling only while something is still in flight.
      const active = (prev: Job[]) =>
        prev.some((x) => x.status === 'queued' || x.status === 'running');
      setJobs((prev) => {
        if (active(prev)) {
          timer.current = setTimeout(tick, POLL_MS);
        }
        return prev;
      });
    };

    tick();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load]);

  return { theories, jobs, loading, error, refresh };
}
