// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { BuildParamSpec, BuildParams, BuildParamValue } from '../types';
import { fetchBuildParams } from '../api';

interface BuildParamsModalProps {
  // e.g. 'Build "Photosynthesis"' or 'Rebuild "Photosynthesis"'.
  title: string;
  // One line under the title explaining what will happen.
  description?: string;
  confirmLabel?: string;
  // Values to start from instead of the server defaults, e.g. a rebuild
  // starts from the theory's most recent job's parameters. Keys it lacks
  // (parameters added since that job ran) take the server default. "Reset"
  // returns to these values.
  initial?: BuildParams | null;
  // Shown under the description when `initial` is used, e.g. where it came from.
  initialNote?: string;
  // Starts the build. A rejected promise keeps the dialog open and shows the
  // error (e.g. the server refusing a value).
  onConfirm: (params: BuildParams) => Promise<void>;
  onClose: () => void;
}

type Draft = Record<string, string>;

function toDraft(specs: BuildParamSpec[], from: (s: BuildParamSpec) => BuildParamValue): Draft {
  return Object.fromEntries(specs.map((s) => [s.key, String(from(s) ?? '')]));
}

/** Per-field problems, keyed by spec key ('' = whole-form problem). */
export function validateDraft(specs: BuildParamSpec[], draft: Draft): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const s of specs) {
    const raw = (draft[s.key] ?? '').trim();
    if (s.type === 'int' || s.type === 'float') {
      const n = Number(raw);
      if (raw === '' || !Number.isFinite(n)) {
        errors[s.key] = 'Enter a number.';
      } else if (s.type === 'int' && !Number.isInteger(n)) {
        errors[s.key] = 'Enter a whole number.';
      } else if (s.max == null && s.min != null && n < s.min) {
        errors[s.key] = `Must be at least ${s.min}.`;
      } else if (s.max != null && ((s.min != null && n < s.min) || n > s.max)) {
        errors[s.key] = `Must be between ${s.min} and ${s.max}.`;
      }
    } else if (s.type === 'choice' && !(s.choices ?? []).includes(raw)) {
      errors[s.key] = 'Choose an option.';
    }
  }
  return errors;
}

/** A field's starting value: from `initial` when it has one, else the default. */
function startingValue(s: BuildParamSpec, initial: BuildParams | null): BuildParamValue {
  return initial && initial[s.key] != null ? initial[s.key] : s.default;
}

function toParams(specs: BuildParamSpec[], draft: Draft): BuildParams {
  const out: BuildParams = {};
  for (const s of specs) {
    const raw = (draft[s.key] ?? '').trim();
    out[s.key] = s.type === 'int' || s.type === 'float' ? Number(raw) : raw;
  }
  return out;
}

/**
 * Modal form for a build's parameters (test set, kiro-cli budget, ...). The
 * fields and their defaults come from GET /api/build-params, so an untouched
 * form starts a default build.
 */
export function BuildParamsModal({
  title,
  description,
  confirmLabel = 'Start build',
  initial = null,
  initialNote,
  onConfirm,
  onClose,
}: BuildParamsModalProps) {
  // The values the form starts from and "Reset" returns to. A ref, so the
  // load-once effect below reads the values the dialog was opened with.
  const initialRef = useRef(initial);
  const startValue = (s: BuildParamSpec) => startingValue(s, initialRef.current);
  const [specs, setSpecs] = useState<BuildParamSpec[] | null>(null);
  const [draft, setDraft] = useState<Draft>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [touched, setTouched] = useState(false);
  const firstField = useRef<HTMLInputElement | HTMLSelectElement | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, submitting]);

  useEffect(() => {
    let cancelled = false;
    fetchBuildParams()
      .then((r) => {
        if (cancelled) return;
        setSpecs(r.params);
        setDraft(toDraft(r.params, (s) => startingValue(s, initialRef.current)));
      })
      .catch(() => {
        if (!cancelled) setLoadError('Could not load the build parameters.');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (specs) firstField.current?.focus();
  }, [specs]);

  const errors = useMemo(() => (specs ? validateDraft(specs, draft) : {}), [specs, draft]);
  const hasErrors = Object.keys(errors).length > 0;

  const groups = useMemo(() => {
    const m = new Map<string, BuildParamSpec[]>();
    for (const s of specs ?? []) {
      if (!m.has(s.group)) m.set(s.group, []);
      m.get(s.group)!.push(s);
    }
    return Array.from(m.entries());
  }, [specs]);

  const isDefault =
    !!specs && specs.every((s) => (draft[s.key] ?? '') === String(startValue(s) ?? ''));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!specs || hasErrors) return;
    setSubmitError(null);
    setSubmitting(true);
    try {
      await onConfirm(toParams(specs, draft));
    } catch (ex) {
      setSubmitError(ex instanceof Error ? ex.message : 'Could not start the build.');
      setSubmitting(false);
    }
  };

  let fieldIndex = 0;

  return (
    <div className="modal-backdrop" onClick={submitting ? undefined : onClose}>
      <div
        className="modal build-params-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="build-params-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div className="modal-title">
            <span className="job-name" id="build-params-title">
              {title}
            </span>
          </div>
          <button
            className="modal-close"
            onClick={onClose}
            aria-label="Close"
            disabled={submitting}
          >
            ×
          </button>
        </div>

        <form className="build-params-form" onSubmit={submit} noValidate>
          {description && <p className="muted small">{description}</p>}
          {initial && initialNote && <p className="muted small">{initialNote}</p>}
          {loadError && <p className="error-text">{loadError}</p>}
          {!specs && !loadError && <p className="muted">Loading parameters…</p>}

          {groups.map(([group, fields]) => (
            <fieldset key={group} className="build-params-group">
              <legend>{group}</legend>
              {fields.map((s) => {
                const id = `bp-${s.key}`;
                const helpId = `${id}-help`;
                const errId = `${id}-err`;
                const err = touched || draft[s.key] !== String(s.default) ? errors[s.key] : undefined;
                const isFirst = fieldIndex++ === 0;
                const refProp = isFirst
                  ? {
                      ref: (el: HTMLInputElement | HTMLSelectElement | null) => {
                        firstField.current = el;
                      },
                    }
                  : {};
                return (
                  <div key={s.key} className="bp-field">
                    <label htmlFor={id}>{s.label}</label>
                    {s.type === 'choice' ? (
                      <select
                        id={id}
                        {...refProp}
                        value={draft[s.key] ?? ''}
                        disabled={submitting}
                        aria-describedby={helpId}
                        onChange={(e) => setDraft((d) => ({ ...d, [s.key]: e.target.value }))}
                      >
                        {(s.choices ?? []).map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        id={id}
                        {...refProp}
                        type={s.type === 'text' ? 'text' : 'number'}
                        inputMode={s.type === 'int' ? 'numeric' : s.type === 'float' ? 'decimal' : undefined}
                        min={s.min}
                        max={s.max}
                        step={s.step}
                        value={draft[s.key] ?? ''}
                        disabled={submitting}
                        placeholder={s.type === 'text' ? 'default' : undefined}
                        aria-invalid={!!err}
                        aria-describedby={err ? `${helpId} ${errId}` : helpId}
                        onChange={(e) => setDraft((d) => ({ ...d, [s.key]: e.target.value }))}
                      />
                    )}
                    <span className="bp-help muted small" id={helpId}>
                      {s.help} Default: {String(s.default) || 'none'}.
                    </span>
                    {err && (
                      <span className="bp-error" id={errId} role="alert">
                        {err}
                      </span>
                    )}
                  </div>
                );
              })}
            </fieldset>
          ))}

          {submitError && (
            <p className="error-text" role="alert">
              {submitError}
            </p>
          )}

          <div className="build-params-actions">
            <button
              type="button"
              className="link-button"
              disabled={!specs || isDefault || submitting}
              onClick={() => specs && setDraft(toDraft(specs, startValue))}
            >
              {initial ? 'Reset to last build' : 'Reset to defaults'}
            </button>
            <span className="spacer" />
            <button type="button" onClick={onClose} disabled={submitting}>
              Cancel
            </button>
            <button type="submit" disabled={!specs || submitting || (touched && hasErrors)}>
              {submitting ? 'Starting…' : confirmLabel}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
