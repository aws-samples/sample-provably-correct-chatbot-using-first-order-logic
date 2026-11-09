// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useMemo, useState } from 'react';
import { BuildParamSpec, BuildParams, BuildParamValue } from '../types';
import { fetchBuildParams } from '../api';

interface JobParamsModalProps {
  // e.g. "Ali Krajcik".
  jobName: string;
  // The parameters the job was built with (null for jobs from before they
  // were recorded).
  params: BuildParams | null | undefined;
  onClose: () => void;
}

function show(v: BuildParamValue | undefined): string {
  if (v === undefined || v === null || v === '') return '(default model)';
  return String(v);
}

/**
 * A read-only summary of the parameters one job was built with, grouped and
 * labelled like the Build parameters dialog. Values that differ from today's
 * defaults are marked.
 */
export function JobParamsModal({ jobName, params, onClose }: JobParamsModalProps) {
  const [specs, setSpecs] = useState<BuildParamSpec[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchBuildParams()
      .then((r) => !cancelled && setSpecs(r.params ?? []))
      .catch(() => !cancelled && setLoadError('Could not load the parameter descriptions.'));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Group the known parameters as the Build parameters dialog does; keys the
  // dialog no longer offers are listed last under "Other".
  const groups = useMemo(() => {
    if (!params || !specs) return [];
    const out: [string, { label: string; help?: string; value: string; changed: boolean }[]][] = [];
    const add = (group: string, row: { label: string; help?: string; value: string; changed: boolean }) => {
      const g = out.find(([name]) => name === group);
      if (g) g[1].push(row);
      else out.push([group, [row]]);
    };
    for (const s of specs) {
      if (!(s.key in params)) continue;
      const v = params[s.key];
      add(s.group, { label: s.label, help: s.help, value: show(v),
                     changed: String(v ?? '') !== String(s.default ?? '') });
    }
    for (const [k, v] of Object.entries(params)) {
      if (!specs.some((s) => s.key === k)) add('Other', { label: k, value: show(v), changed: false });
    }
    return out;
  }, [params, specs]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal job-params-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="job-params-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div className="modal-title">
            <span className="job-name" id="job-params-title">
              Job parameters: {jobName}
            </span>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="job-params-body">
          {!params ? (
            <p className="muted">
              This job did not record its parameters (it ran before they were
              kept), so it used the defaults of the time.
            </p>
          ) : loadError ? (
            <p className="error-text">{loadError}</p>
          ) : !specs ? (
            <p className="muted">Loading parameters…</p>
          ) : (
            <>
              {groups.map(([group, rows]) => (
                <table key={group} className="job-params-table" aria-label={group}>
                  <caption>{group}</caption>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.label} className={r.changed ? 'changed' : undefined}>
                        <th scope="row" title={r.help}>{r.label}</th>
                        <td>
                          {r.value}
                          {r.changed && <span className="job-params-changed"> (not the default)</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ))}
              <p className="muted small">
                “Not the default” compares with today's defaults.
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
