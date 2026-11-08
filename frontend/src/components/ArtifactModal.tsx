// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useState } from 'react';
import { TheorySummary, TheoryDetail } from '../types';
import { fetchTheory } from '../api';

// Which artifact of a theory the modal shows.
export type ArtifactKind = 'source' | 'smtlib' | 'qas' | 'yaml';

interface ArtifactModalProps {
  theory: TheorySummary;
  kind: ArtifactKind;
  onClose: () => void;
}

const TITLES: Record<ArtifactKind, string> = {
  source: 'Source document',
  smtlib: 'SMT-LIB theory',
  qas: 'Verified test-set questions',
  yaml: 'Theory YAML',
};

/**
 * Modal dialog showing ONE artifact of a theory -- its source document or its
 * computed SMT-LIB theory -- in a large (80% of the viewport) scrollable pane.
 * This replaces the old "Current theory" tab: the same two artifacts
 * (TheoryDetail.source_document / .computed_theory from GET /api/theories/{id})
 * are now reachable per-row from the Theories tab's Artifacts column.
 */
export function ArtifactModal({ theory, kind, onClose }: ArtifactModalProps) {
  const [detail, setDetail] = useState<TheoryDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Close on Escape (matches the SentencesModal behavior).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Fetch the full theory detail (which carries both artifacts) on open.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchTheory(theory.id)
      .then((d: TheoryDetail) => {
        if (!cancelled) setDetail(d);
      })
      .catch(() => {
        if (!cancelled) setError('Could not load this artifact.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [theory.id]);

  // The text for the requested artifact, with the same empty/not-ready fallback
  // messages the old Current-theory tab used.
  const body = (() => {
    if (!detail) return '';
    const notReady =
      detail.status === 'ready'
        ? '(empty)'
        : `Theory is ${detail.status}; no computed artifact yet.`;
    if (kind === 'source') return detail.source_document || '(none)';
    if (kind === 'smtlib') return detail.computed_theory || notReady;
    if (kind === 'yaml') return detail.computed_theory_yaml || notReady;
    // qas: one question per line.
    const qs = detail.suggested_questions ?? [];
    return qs.length > 0 ? qs.join('\n') : notReady;
  })();

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal artifact-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`${TITLES[kind]} for ${theory.name}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div className="modal-title">
            <span className="job-name">{TITLES[kind]}</span>
            <span className="muted small">{theory.name}</span>
            {loading && <span className="muted small">loading…</span>}
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        {error && (
          <p className="error-text" style={{ margin: '12px 16px' }}>
            Error: {error}
          </p>
        )}
        {!error && !loading && (
          <pre className="modal-log artifact-body">{body}</pre>
        )}
      </div>
    </div>
  );
}
