import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { fetchKiroLog } from '../api';

// How often a running build's kiro-cli output is refreshed. Each refresh
// reads only lines newer than the last one shown.
export const KIRO_POLL_MS = 2500;
// Lines kept in the view; older ones drop off the top.
export const MAX_KIRO_VIEW_LINES = 5000;
// Within this many pixels of the bottom counts as "following" the output.
const FOLLOW_SLACK_PX = 40;

export type KiroLineKind = 'session' | 'verdict' | 'line';

/**
 * How a kiro-cli view line is shown. "kc: $ kiro-cli ..." starts a session;
 * "kcvut: ..." is the harness (its verdicts between sessions); anything else
 * is the session's own output. The "kc: " prefix is dropped for display.
 */
export function classifyKiroLine(line: string): { kind: KiroLineKind; text: string } {
  if (line.startsWith('kc: $ ')) return { kind: 'session', text: line.slice(4) };
  if (line.startsWith('kcvut: ')) return { kind: 'verdict', text: line.slice(7) };
  return { kind: 'line', text: line.startsWith('kc: ') ? line.slice(4) : line };
}

/**
 * Live tail of the build's kiro-cli sessions. The first load shows the
 * newest lines; while the build runs, each refresh appends only what was
 * logged since (the server returns a cursor). The view follows the newest
 * line until the user scrolls up, then offers "Jump to latest".
 */
export function KiroLiveView({ jobId, live }: { jobId: string; live: boolean }) {
  const [lines, setLines] = useState<string[]>([]);
  const [dropped, setDropped] = useState(0);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [following, setFollowing] = useState(true);
  const boxRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cursor: string | null = null;
    setLines([]);
    setDropped(0);
    setLoaded(false);
    const load = async () => {
      try {
        const r = await fetchKiroLog(jobId, cursor);
        if (cancelled) return;
        cursor = r.cursor ?? cursor;
        if (r.lines.length > 0 || r.omitted > 0) {
          setLines((prev) => {
            const next = prev.concat(r.lines);
            const over = next.length - MAX_KIRO_VIEW_LINES;
            if (over > 0) setDropped((d) => d + over);
            return over > 0 ? next.slice(over) : next;
          });
          if (r.omitted > 0) setDropped((d) => d + r.omitted);
        }
        setNote(r.note);
        setError(null);
        setLoaded(true);
      } catch {
        if (!cancelled) setError('Could not load the kiro-cli output.');
      } finally {
        if (!cancelled && live) timer = setTimeout(load, KIRO_POLL_MS);
      }
    };
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId, live]);

  // Stay pinned to the newest line while following.
  useLayoutEffect(() => {
    const box = boxRef.current;
    if (box && followingRef.current) box.scrollTop = box.scrollHeight;
  }, [lines]);

  const onScroll = () => {
    const box = boxRef.current;
    if (!box) return;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight <= FOLLOW_SLACK_PX;
    followingRef.current = atBottom;
    setFollowing(atBottom);
  };

  const jumpToLatest = () => {
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
    followingRef.current = true;
    setFollowing(true);
  };

  let sessions = 0;
  return (
    <>
      <div className="unit-log-bar">
        <span className="muted small" role="status">
          {live ? 'Live · refreshes every few seconds' : 'Build ended'}
          {loaded ? ` · ${lines.length.toLocaleString()} lines` : ''}
          {dropped > 0 ? ` (${dropped.toLocaleString()} older not shown; see Logs)` : ''}
        </span>
        {!following && (
          <button type="button" className="link-button" onClick={jumpToLatest}>
            Jump to latest
          </button>
        )}
      </div>
      {error && <p className="error-text">{error}</p>}
      {note && <p className="muted small unit-note">{note}</p>}
      <div
        ref={boxRef}
        className="modal-log artifact-body kiro-live"
        onScroll={onScroll}
        role="log"
        aria-label="kiro-cli output"
        aria-live="off"
        tabIndex={0}
      >
        {lines.map((line, i) => {
          const { kind, text } = classifyKiroLine(line);
          if (kind === 'session') {
            sessions += 1;
            return (
              <div key={i} className="kiro-session-start">
                <span className="kiro-session-label">
                  {dropped === 0 ? `Session ${sessions} started` : 'Session started'}
                </span>
                <span className="kiro-session-cmd">{text}</span>
              </div>
            );
          }
          return (
            <div key={i} className={kind === 'verdict' ? 'kiro-line kiro-verdict' : 'kiro-line'}>
              {text}
            </div>
          );
        })}
      </div>
    </>
  );
}
