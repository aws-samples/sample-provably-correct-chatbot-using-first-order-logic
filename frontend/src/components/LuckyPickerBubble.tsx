// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useRef, useState } from 'react';

interface LuckyPickerBubbleProps {
  // Loads the theory-specific suggested questions when the bubble opens.
  fetchChoices: () => Promise<string[]>;
  // Called with the question the user picks; the parent asks it.
  onPick: (question: string) => void;
  onClose: () => void;
}

/**
 * "I'm Feeling Lucky" picker rendered as a speech bubble that slides up from
 * the lucky button. The parent (Composer) positions it directly above the
 * button via a `.lucky-bubble-anchor` wrapper; this component handles the
 * slide-up animation, loading the theory's suggested questions, click-outside
 * and Escape to close.
 *
 * Replaces the old modal picker: no backdrop, anchored to the button, with a
 * downward-pointing tail so it reads as coming from the button.
 */
export function LuckyPickerBubble({ fetchChoices, onPick, onClose }: LuckyPickerBubbleProps) {
  const [questions, setQuestions] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Drives the slide-up: starts false (translated down + transparent), flips
  // to true on mount so the CSS transition animates it into place.
  const [shown, setShown] = useState(false);
  const bubbleRef = useRef<HTMLDivElement>(null);

  // Trigger the slide-up on mount (next frame so the transition runs).
  useEffect(() => {
    const id = requestAnimationFrame(() => setShown(true));
    return () => cancelAnimationFrame(id);
  }, []);

  // Close on Escape or a click outside the bubble.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    const onDown = (e: MouseEvent) => {
      // Ignore clicks on the bubble itself; a click anywhere else closes it.
      // The lucky button's own handler toggles separately, so guard against
      // the bubble node only.
      if (bubbleRef.current && !bubbleRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    // Defer binding the outside-click one tick so the opening click that
    // mounted this bubble doesn't immediately close it.
    const t = window.setTimeout(
      () => document.addEventListener('mousedown', onDown),
      0,
    );
    return () => {
      window.removeEventListener('keydown', onKey);
      window.clearTimeout(t);
      document.removeEventListener('mousedown', onDown);
    };
  }, [onClose]);

  // Load the picker list once on open.
  useEffect(() => {
    let cancelled = false;
    fetchChoices()
      .then((qs) => {
        if (!cancelled) setQuestions(qs);
      })
      .catch(() => {
        if (!cancelled) setError('Could not load questions. Please try again.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fetchChoices]);

  return (
    <div
      ref={bubbleRef}
      className={`lucky-bubble${shown ? ' lucky-bubble--shown' : ''}`}
      role="dialog"
      aria-modal="false"
      aria-label="Pick a question"
    >
      <div className="lucky-bubble-head">
        <span className="lucky-bubble-title">Pick a question</span>
        {loading && <span className="muted small">loading…</span>}
        <button
          className="lucky-bubble-close"
          onClick={onClose}
          aria-label="Close"
        >
          ×
        </button>
      </div>

      {error && <p className="error-text">{error}</p>}

      {!loading && !error && questions.length === 0 && (
        <p className="muted small lucky-empty">
          This theory has no suggested questions.
        </p>
      )}

      {questions.length > 0 && (
        <ul className="lucky-list">
          {questions.map((q, i) => (
            <li key={i}>
              <button
                type="button"
                className="lucky-item"
                onClick={() => onPick(q)}
              >
                {q}
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Downward tail so the bubble reads as rising from the button. */}
      <span className="lucky-bubble-tail" aria-hidden="true" />
    </div>
  );
}
