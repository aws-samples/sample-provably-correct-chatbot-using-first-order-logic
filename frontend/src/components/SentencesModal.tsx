import React, { useEffect, useState } from 'react';
import { TheorySummary, TheoryDetail } from '../types';
import { fetchTheory } from '../api';

interface SentencesModalProps {
  theory: TheorySummary;
  onClose: () => void;
}

/**
 * Modal dialog listing the individual sentences a theory's source document was
 * split into. Shows each sentence's ORIGINAL (contextual) form alongside its
 * DECONTEXTUALIZED ("standalone") form in a two-column table -- original on
 * the left, decontextualized on the right. Both come from GET
 * /api/theories/{id} (store.get_theory returns index-aligned `sentences` and
 * `standalone_sentences`). FHIR theories are not sentence-split and return
 * empty lists. When a theory carries no distinct standalone rewrite the two
 * columns are equal (the backend falls back to the original for both).
 */
export function SentencesModal({ theory, onClose }: SentencesModalProps) {
  const [sentences, setSentences] = useState<string[] | null>(null);
  const [standalone, setStandalone] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Close on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Fetch the full theory detail (which carries the sentences) on open.
  useEffect(() => {
    let cancelled = false;
    fetchTheory(theory.id)
      .then((d: TheoryDetail) => {
        if (!cancelled) {
          setSentences(d.sentences ?? []);
          setStandalone(d.standalone_sentences ?? []);
        }
      })
      .catch(() => {
        if (!cancelled) setError('Could not load the sentences.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [theory.id]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Sentences for ${theory.name}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div className="modal-title">
            <span className="job-name">{theory.name}</span>
            <span className="muted small">
              {sentences
                ? `${sentences.length} sentence${sentences.length === 1 ? '' : 's'}`
                : `${theory.sentence_count} sentences`}
            </span>
            {loading && <span className="muted small">loading…</span>}
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        {error && <p className="error-text">Error: {error}</p>}
        {!loading && !error && sentences && sentences.length === 0 && (
          <p className="muted small" style={{ margin: '12px 16px' }}>
            This theory has no sentence list (FHIR theories are not
            sentence-split).
          </p>
        )}
        {sentences && sentences.length > 0 && (
          <div className="sentence-table-wrap">
            <table className="sentence-table">
              <thead>
                <tr>
                  <th className="sentence-col-num" scope="col">
                    #
                  </th>
                  <th scope="col">Original</th>
                  <th scope="col">Decontextualized</th>
                </tr>
              </thead>
              <tbody>
                {sentences.map((s, i) => {
                  const d = standalone[i] ?? s;
                  const changed = d !== s;
                  return (
                    <tr key={i}>
                      <td className="sentence-col-num muted small">{i + 1}</td>
                      <td>{s}</td>
                      <td className={changed ? 'sentence-changed' : undefined}>
                        {d}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
