// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useRef, useState } from 'react';
import {
  createTheoryFromFile,
  createTheoryFromText,
  createTheoryFromSample,
  createTheoryFromWikipedia,
  deleteTheory,
  rebuildTheory,
  killJob,
  fetchSamplePatients,
  isWikipediaUrl,
  renameTheory,
  theoryNameTaken,
  ApiError,
} from '../api';
import { TheorySummary, DocType, SamplePatient, Job, BuildParams } from '../types';
import { SentencesModal } from './SentencesModal';
import { ArtifactModal, ArtifactKind } from './ArtifactModal';
import { BuildParamsModal } from './BuildParamsModal';

interface TheoriesTabProps {
  theories: TheorySummary[];
  jobs: Job[];
  onChanged: () => void; // ask the parent to refresh theories+jobs
  onOpenChat: (theoryId: string) => void; // open the theory's "Chat (<name>)" tab
  onOpenJob: (jobId: string) => void; // open the job's "Job Details (<name>)" tab
}

type Mode = 'paste' | 'upload' | 'sample' | 'wikipedia';

/**
 * The Theories tab: the theories, each with links to its chat and Job Details
 * tabs and a rename pencil, plus an "Add theory" form. Adding a theory (by
 * pasting a source document, uploading a file, picking a baked-in Synthea
 * sample patient or giving a Wikipedia URL) POSTs to /api/theories, which
 * creates a build job (visible in the Jobs tab) and a "building" theory here.
 * Theory names are unique.
 */
export function TheoriesTab({ theories, jobs, onChanged, onOpenChat, onOpenJob }: TheoriesTabProps) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>('paste');
  const [docType, setDocType] = useState<DocType>('auto');
  const [name, setName] = useState('');
  const [text, setText] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [samplePatients, setSamplePatients] = useState<SamplePatient[]>([]);
  const [sampleId, setSampleId] = useState('');
  const [wikiUrl, setWikiUrl] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameInput = useRef<HTMLInputElement>(null);
  // Which theory's sentence-list modal is open (null = closed).
  const [sentencesFor, setSentencesFor] = useState<TheorySummary | null>(null);
  // Which theory artifact (source document / SMT-LIB theory) is open in a modal
  // (null = closed). Replaces the old "Current theory" tab.
  const [artifactFor, setArtifactFor] = useState<
    { theory: TheorySummary; kind: ArtifactKind } | null
  >(null);
  // Ids of theories whose delete request is in flight (disables the button and
  // shows "Deleting…").
  const [deleting, setDeleting] = useState<Set<string>>(new Set());
  // The Build parameters dialog: open for a new build (from the Add theory
  // form) or for a rebuild of an existing theory; null = closed.
  const [paramsFor, setParamsFor] = useState<
    { kind: 'create' } | { kind: 'rebuild'; theory: TheorySummary } | null
  >(null);
  // By default only Ready and Building theories are listed; failed and
  // cancelled ones are one click away (to Rebuild or Delete them). The choice
  // is remembered across reloads.
  const [showInactive, setShowInactive] = useState<boolean>(
    () => localStorage.getItem(SHOW_INACTIVE_KEY) === '1'
  );
  useEffect(() => {
    localStorage.setItem(SHOW_INACTIVE_KEY, showInactive ? '1' : '0');
  }, [showInactive]);
  const inactiveCount = theories.filter((t) => !isActive(t)).length;
  const visibleTheories = showInactive ? theories : theories.filter(isActive);

  const handleDelete = async (t: TheorySummary) => {
    if (deleting.has(t.id)) return;
    if (!window.confirm(`Delete the theory "${t.name}"? This cannot be undone.`)) {
      return;
    }
    setDeleting((prev) => new Set(prev).add(t.id));
    try {
      await deleteTheory(t.id);
      onChanged(); // parent refetches theories+jobs; the row disappears
    } catch {
      window.alert('Could not delete the theory. Please try again.');
    } finally {
      setDeleting((prev) => {
        const next = new Set(prev);
        next.delete(t.id);
        return next;
      });
    }
  };

  // Ids of theories whose rebuild request is in flight (disables the button and
  // shows "Rebuilding…").
  const [rebuilding, setRebuilding] = useState<Set<string>>(new Set());

  // Rebuild opens the Build parameters dialog; the dialog's confirm starts it.
  const handleRebuild = (t: TheorySummary) => {
    if (rebuilding.has(t.id)) return;
    setParamsFor({ kind: 'rebuild', theory: t });
  };

  // Errors propagate to the dialog, which stays open and shows them.
  const startRebuild = async (t: TheorySummary, params: BuildParams) => {
    setRebuilding((prev) => new Set(prev).add(t.id));
    try {
      await rebuildTheory(t.id, params);
      setParamsFor(null);
      onChanged(); // parent refetches: the theory flips to building + new job
    } finally {
      setRebuilding((prev) => {
        const next = new Set(prev);
        next.delete(t.id);
        return next;
      });
    }
  };

  // Ids of theories whose cancel request is in flight (disables the button and
  // shows "Cancelling…").
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());

  // Cancel a building theory: kill its job via the Jobs API. The backend flips
  // the job to `cancelled` AND the theory to `cancelled` (so it leaves the
  // "building" state and can then be deleted/rebuilt, shown as "Cancelled"
  // rather than "Failed"). Needs the theory's job_id; without one there is
  // nothing to kill.
  const handleCancel = async (t: TheorySummary) => {
    if (cancelling.has(t.id)) return;
    if (!t.job_id) {
      window.alert('No build job is associated with this theory to cancel.');
      return;
    }
    if (
      !window.confirm(
        `Cancel the build of "${t.name}"? It will stop and move to the ` +
          `Cancelled state; you can then delete or rebuild it.`
      )
    ) {
      return;
    }
    setCancelling((prev) => new Set(prev).add(t.id));
    try {
      await killJob(t.job_id);
      onChanged(); // parent refetches: theory flips building -> cancelled
    } catch {
      window.alert('Could not cancel the build. Please try again.');
    } finally {
      setCancelling((prev) => {
        const next = new Set(prev);
        next.delete(t.id);
        return next;
      });
    }
  };

  // Load the baked-in sample patients the first time the form is opened. If the
  // build shipped without them the list is empty and the "Sample patient"
  // option is hidden.
  useEffect(() => {
    if (open && samplePatients.length === 0) {
      fetchSamplePatients()
        .then((r) => setSamplePatients(r.sample_patients ?? []))
        .catch(() => setSamplePatients([]));
    }
  }, [open, samplePatients.length]);

  const reset = () => {
    setName('');
    setText('');
    setFile(null);
    setSampleId('');
    setWikiUrl('');
    setError(null);
    setMode('paste');
    setDocType('auto');
    setOpen(false);
  };

  const submit = async () => {
    setError(null);
    // Name is required EXCEPT in sample/wikipedia mode, where it defaults
    // server-side (to the patient's name / the URL).
    if (mode !== 'sample' && mode !== 'wikipedia' && !name.trim()) {
      setError('Please give the theory a name.');
      return;
    }
    if (mode === 'paste' && !text.trim()) {
      setError('Please paste a source document.');
      return;
    }
    if (mode === 'upload' && !file) {
      setError('Please choose a file to upload.');
      return;
    }
    if (mode === 'sample' && !sampleId) {
      setError('Please choose a sample patient.');
      return;
    }
    if (mode === 'wikipedia' && !wikiUrl.trim()) {
      setError('Please enter a page URL.');
      return;
    }
    // Theory names are unique, so a clash must be renamed before building.
    // The server refuses it too (409); checking here saves the round trip.
    const taken = theoryNameTaken(theories, effectiveName);
    if (taken) {
      setError(nameTakenMessage(taken.name));
      nameInput.current?.focus();
      return;
    }
    // The source is complete: choose the build's parameters next.
    setParamsFor({ kind: 'create' });
  };

  // Called by the Build parameters dialog. Errors propagate to the dialog,
  // which stays open and shows them (e.g. a value the server refused).
  const startCreate = async (params: BuildParams) => {
    setSubmitting(true);
    try {
      if (mode === 'paste') {
        await createTheoryFromText(name.trim(), text, docType, params);
      } else if (mode === 'upload' && file) {
        await createTheoryFromFile(name.trim(), file, docType, params);
      } else if (mode === 'sample') {
        await createTheoryFromSample(sampleId, name.trim() || undefined, params);
      } else if (mode === 'wikipedia') {
        await createTheoryFromWikipedia(wikiUrl.trim(), name.trim() || undefined, params);
      }
      setParamsFor(null);
      reset();
      onChanged();
    } catch (e) {
      // The name was taken meanwhile (e.g. in another browser tab): close the
      // dialog and ask for a different name in the form.
      if (e instanceof ApiError && e.status === 409) {
        setParamsFor(null);
        setError(e.message);
        onChanged();
        nameInput.current?.focus();
        return;
      }
      throw e;
    } finally {
      setSubmitting(false);
    }
  };

  // The name the new theory will get: the one typed, else (sample/wikipedia
  // only) the server's default, the patient's name or the page URL.
  const effectiveName =
    name.trim() ||
    (mode === 'wikipedia' ? wikiUrl.trim() : '') ||
    (mode === 'sample' ? samplePatients.find((p) => p.id === sampleId)?.name ?? sampleId : '');
  const createLabel = effectiveName || 'new theory';

  // Red warning when the entered URL is not a Wikipedia page: the HTML->sentence
  // parser is tuned for Wikipedia article markup, so other sites may extract
  // poorly. We only warn once something URL-ish has been typed, so a
  // half-finished URL doesn't flash the warning.
  const showNonWikiWarning =
    mode === 'wikipedia' && wikiUrl.trim() !== '' && !isWikipediaUrl(wikiUrl);

  // Index jobs by id so a "building" theory can show its current step from the
  // matching job's progress string (e.g. "kiro-cli session 2 (...)" ->
  // "building — session 2").
  const jobsById = React.useMemo(() => {
    const m = new Map<string, Job>();
    for (const j of jobs) m.set(j.id, j);
    return m;
  }, [jobs]);

  // A rebuild starts from the parameters of the theory's most recent job that
  // recorded them (jobs from before the Build parameters dialog have none).
  const lastBuild =
    paramsFor?.kind === 'rebuild' ? lastJobWithParams(jobs, paramsFor.theory.id) : null;

  return (
    <div className="theories-tab">
      {paramsFor && (
        <BuildParamsModal
          title={
            paramsFor.kind === 'create'
              ? `Build parameters: ${createLabel}`
              : `Rebuild parameters: ${paramsFor.theory.name}`
          }
          description={
            paramsFor.kind === 'create'
              ? 'Starts a build job (tracked in the Jobs tab).'
              : 'Rebuilds this theory in place from its stored source; it shows as building until the new job finishes.'
          }
          confirmLabel={paramsFor.kind === 'create' ? 'Start build' : 'Start rebuild'}
          initial={lastBuild?.build_params ?? null}
          initialNote={
            lastBuild
              ? `Starting from the parameters of the last build (${new Date(
                  (lastBuild.started_at ?? lastBuild.created_at) * 1000
                ).toLocaleString()}).`
              : undefined
          }
          onConfirm={(params) =>
            paramsFor.kind === 'create' ? startCreate(params) : startRebuild(paramsFor.theory, params)
          }
          onClose={() => setParamsFor(null)}
        />
      )}
      <div className="theories-header">
        <label className="show-inactive-toggle">
          <input
            type="checkbox"
            checked={showInactive}
            onChange={(e) => setShowInactive(e.target.checked)}
          />
          Show failed and cancelled ({inactiveCount})
        </label>
        <button onClick={() => setOpen((o) => !o)}>
          {open ? 'Cancel' : '+ Add theory'}
        </button>
      </div>

      {open && (
        <div className="add-theory-form">
          <div className="field">
            <label htmlFor="theory-name">
              Name{mode === 'sample' || mode === 'wikipedia' ? ' (optional)' : ''}
            </label>
            <input
              id="theory-name"
              ref={nameInput}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={
                mode === 'sample'
                  ? "Defaults to the patient's name"
                  : mode === 'wikipedia'
                  ? 'Defaults to the page URL'
                  : 'e.g. Photosynthesis'
              }
            />
          </div>

          {mode !== 'sample' && mode !== 'wikipedia' && (
            <div className="field">
              <label htmlFor="doc-type">Document type</label>
              <select
                id="doc-type"
                value={docType}
                onChange={(e) => setDocType(e.target.value as DocType)}
              >
                <option value="text">Text</option>
                <option value="fhir">FHIR</option>
                <option value="auto">Auto (guess from content)</option>
              </select>
              <p className="muted small">
                {docType === 'auto'
                  ? 'Auto inspects the document: a JSON FHIR Bundle is treated as FHIR, otherwise as prose text.'
                  : docType === 'fhir'
                  ? 'FHIR: a JSON HL7 FHIR Bundle for one patient (parsed as JSON, not sentence-split).'
                  : 'Text: prose (e.g. a Wikipedia page), split into sentences.'}
              </p>
            </div>
          )}

          <div className="mode-toggle">
            <label>
              <input
                type="radio"
                name="mode"
                checked={mode === 'paste'}
                onChange={() => setMode('paste')}
              />
              Paste text
            </label>
            <label>
              <input
                type="radio"
                name="mode"
                checked={mode === 'upload'}
                onChange={() => setMode('upload')}
              />
              Upload a file
            </label>
            {samplePatients.length > 0 && (
              <label>
                <input
                  type="radio"
                  name="mode"
                  checked={mode === 'sample'}
                  onChange={() => setMode('sample')}
                />
                Sample patient
              </label>
            )}
            <label>
              <input
                type="radio"
                name="mode"
                checked={mode === 'wikipedia'}
                onChange={() => setMode('wikipedia')}
              />
              Wikipedia page
            </label>
          </div>

          {mode === 'paste' && (
            <textarea
              className="doc-textarea"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Paste the source document here…"
              rows={10}
            />
          )}
          {mode === 'upload' && (
            <input
              type="file"
              accept=".txt,.md,.json,text/plain,application/json"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          )}
          {mode === 'sample' && (
            <div className="field">
              <label htmlFor="sample-patient">Sample patient (Synthea FHIR)</label>
              <select
                id="sample-patient"
                value={sampleId}
                onChange={(e) => setSampleId(e.target.value)}
              >
                <option value="">— Choose a patient —</option>
                {samplePatients.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
              <p className="muted small">
                A synthetic patient's HL7 FHIR Bundle, built as a FHIR theory
                (no upload needed).
              </p>
            </div>
          )}
          {mode === 'wikipedia' && (
            <div className="field">
              <label htmlFor="wiki-url">Page URL</label>
              <input
                id="wiki-url"
                type="url"
                value={wikiUrl}
                onChange={(e) => setWikiUrl(e.target.value)}
                placeholder="https://en.wikipedia.org/wiki/Photosynthesis"
              />
              {showNonWikiWarning && (
                <p className="warning-text">
                  ⚠ This doesn't look like a Wikipedia URL. The parser extracts
                  sentences from Wikipedia article markup, so other pages will
                  likely not parse properly.
                </p>
              )}
              <p className="muted small">
                The page is downloaded and its paragraphs are split into
                sentences (built as a Text theory). Works best on Wikipedia
                articles.
              </p>
            </div>
          )}

          {error && <p className="error-text">{error}</p>}

          <button onClick={submit} disabled={submitting}>
            {submitting ? 'Starting job…' : 'Build theory…'}
          </button>
          <p className="muted small">
            Building runs the WikiLogifier and may take a while. Track progress
            in the Jobs tab.
          </p>
        </div>
      )}

      <table className="theories-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Status</th>
            <th>Sentences</th>
            <th>Created</th>
            <th>Artifacts</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {theories.length === 0 && (
            <tr>
              <td colSpan={7} className="muted">
                No theories yet. Add one to get started.
              </td>
            </tr>
          )}
          {theories.length > 0 && visibleTheories.length === 0 && (
            <tr>
              <td colSpan={7} className="muted">
                No ready or building theories. {inactiveCount} failed or
                cancelled {inactiveCount === 1 ? 'theory is' : 'theories are'} hidden;
                tick “Show failed and cancelled” to rebuild or delete them.
              </td>
            </tr>
          )}
          {visibleTheories.map((t) => (
            <tr key={t.id}>
              <td className="theory-name-cell">
                <TheoryName theory={t} theories={theories} onRenamed={onChanged} />
              </td>
              <td>{(t.origin ?? t.doc_type ?? 'text').toUpperCase()}</td>
              <td>
                <span className={`status-pill status-${t.status}`}>
                  {t.status === 'building'
                    ? buildingLabel(t.job_id ? jobsById.get(t.job_id) : undefined)
                    : t.status === 'cancelled'
                    ? 'Cancelled'
                    : t.status}
                </span>
              </td>
              <td>
                {t.sentence_count > 0 ? (
                  <button
                    type="button"
                    className="sentence-count-link"
                    onClick={() => setSentencesFor(t)}
                    title="Show the sentences, one per line"
                  >
                    {t.sentence_count} sentence{t.sentence_count === 1 ? '' : 's'}
                  </button>
                ) : (
                  t.sentence_count
                )}
              </td>
              <td>{formatTime(t.created_at)}</td>
              <td className="theory-artifacts">
                <div className="artifact-buttons">
                  <button
                    type="button"
                    className="artifact-button"
                    onClick={() => setArtifactFor({ theory: t, kind: 'source' })}
                    title="View the source document in a dialog"
                  >
                    Source
                  </button>
                  <button
                    type="button"
                    className="artifact-button"
                    onClick={() => setArtifactFor({ theory: t, kind: 'smtlib' })}
                    disabled={t.status !== 'ready'}
                    title={
                      t.status === 'ready'
                        ? 'View the computed SMT-LIB theory in a dialog'
                        : `No computed theory yet (status: ${t.status})`
                    }
                  >
                    SMT-LIB
                  </button>
                  <button
                    type="button"
                    className="artifact-button"
                    onClick={() => setArtifactFor({ theory: t, kind: 'qas' })}
                    disabled={t.status !== 'ready'}
                    title={
                      t.status === 'ready'
                        ? 'View the verified test-set questions'
                        : `No questions yet (status: ${t.status})`
                    }
                  >
                    Q&amp;As
                  </button>
                  <button
                    type="button"
                    className="artifact-button"
                    onClick={() => setArtifactFor({ theory: t, kind: 'yaml' })}
                    disabled={t.status !== 'ready'}
                    title={
                      t.status === 'ready'
                        ? 'View the raw theory YAML'
                        : `No YAML yet (status: ${t.status})`
                    }
                  >
                    YAML
                  </button>
                </div>
              </td>
              <td className="theory-actions">
                {t.status === 'building' ? (
                  <div className="action-buttons">
                    {jobDetailsLink(t, onOpenJob)}
                    <button
                      type="button"
                      className="cancel-button"
                      onClick={() => handleCancel(t)}
                      disabled={cancelling.has(t.id)}
                      title="Cancel this build (moves the theory to Cancelled)"
                    >
                      {cancelling.has(t.id) ? 'Cancelling…' : 'Cancel'}
                    </button>
                  </div>
                ) : (
                  <div className="action-buttons">
                    {jobDetailsLink(t, onOpenJob)}
                    {t.status === 'ready' && (
                      <button
                        type="button"
                        className="chat-button"
                        onClick={() => onOpenChat(t.id)}
                        title="Open a chat tab that asks this theory questions"
                      >
                        Chat
                      </button>
                    )}
                    <button
                      type="button"
                      className="rebuild-button"
                      onClick={() => handleRebuild(t)}
                      disabled={rebuilding.has(t.id)}
                      title="Rebuild this theory from its source document"
                    >
                      {rebuilding.has(t.id) ? 'Rebuilding…' : 'Rebuild'}
                    </button>
                    <button
                      type="button"
                      className="delete-button"
                      onClick={() => handleDelete(t)}
                      disabled={deleting.has(t.id)}
                      title="Delete this theory"
                    >
                      {deleting.has(t.id) ? 'Deleting…' : 'Delete'}
                    </button>
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {sentencesFor && (
        <SentencesModal
          theory={sentencesFor}
          onClose={() => setSentencesFor(null)}
        />
      )}

      {artifactFor && (
        <ArtifactModal
          theory={artifactFor.theory}
          kind={artifactFor.kind}
          onClose={() => setArtifactFor(null)}
        />
      )}
    </div>
  );
}

const SHOW_INACTIVE_KEY = 'chatbot.showInactiveTheories';

function nameTakenMessage(existing: string): string {
  return `A theory named "${existing}" already exists. Choose a different name.`;
}

/** The row's link to its build job's Job Details tab (none without a job). */
function jobDetailsLink(t: TheorySummary, onOpenJob: (jobId: string) => void) {
  if (!t.job_id) return null;
  const jobId = t.job_id;
  return (
    <button
      type="button"
      className="job-details-button"
      onClick={() => onOpenJob(jobId)}
      title="Open the Job Details tab for this theory's latest build"
    >
      Job details
    </button>
  );
}

interface TheoryNameProps {
  theory: TheorySummary;
  theories: TheorySummary[];
  onRenamed: () => void;
}

/**
 * A theory's name with a pencil button that edits it in place. Enter or
 * leaving the field saves; Escape cancels. A name another theory already has
 * is refused (here, and by the server with a 409).
 */
function TheoryName({ theory, theories, onRenamed }: TheoryNameProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(theory.name);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set while Escape cancels, so the blur that follows does not save.
  const cancelled = useRef(false);

  const start = () => {
    setDraft(theory.name);
    setError(null);
    cancelled.current = false;
    setEditing(true);
  };

  const cancel = () => {
    cancelled.current = true;
    setEditing(false);
    setError(null);
  };

  const save = async () => {
    if (cancelled.current || saving) return;
    const next = draft.trim();
    if (next === theory.name) {
      setEditing(false);
      setError(null);
      return;
    }
    if (!next) {
      setError('A theory name is required.');
      return;
    }
    const taken = theoryNameTaken(theories, next, theory.id);
    if (taken) {
      setError(nameTakenMessage(taken.name));
      return;
    }
    setSaving(true);
    try {
      await renameTheory(theory.id, next);
      setEditing(false);
      setError(null);
      onRenamed();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not rename the theory.');
    } finally {
      setSaving(false);
    }
  };

  if (!editing) {
    return (
      <span className="theory-name">
        <span className="theory-name-text">{theory.name}</span>
        <button
          type="button"
          className="rename-button"
          onClick={start}
          aria-label={`Rename ${theory.name}`}
          title="Rename this theory"
        >
          ✎
        </button>
      </span>
    );
  }
  return (
    <span className="theory-name editing">
      <input
        className="rename-input"
        aria-label={`New name for ${theory.name}`}
        value={draft}
        autoFocus
        disabled={saving}
        onChange={(e) => {
          setDraft(e.target.value);
          setError(null);
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            save();
          } else if (e.key === 'Escape') {
            e.preventDefault();
            cancel();
          }
        }}
        onBlur={save}
      />
      {error && (
        <span className="error-text rename-error" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}

/** Ready and Building theories are listed by default; the rest are opt-in. */
function isActive(t: TheorySummary): boolean {
  return t.status === 'ready' || t.status === 'building';
}

function formatTime(epochSeconds: number): string {
  if (!epochSeconds) return '—';
  return new Date(epochSeconds * 1000).toLocaleString();
}

/**
 * Label for a "building" theory's status pill: the build's current step from
 * the matching job's progress string (store._progress_reporter), e.g.
 * "kiro-cli session 2 (fixing after verification)" -> "building — session 2",
 * then "building — post-processing" while the theory is finalized. Falls
 * back to plain "building" (queued, starting, or no matching job yet).
 */
export function buildingLabel(job: Job | undefined): string {
  const progress = job?.progress ?? '';
  if (/post-?processing/i.test(progress)) return 'building — post-processing';
  const session = progress.match(/session\s+(\d+)/i);
  if (session) return `building — session ${session[1]}`;
  if (/test set/i.test(progress)) return 'building — test set';
  if (/building theory/i.test(progress)) return 'building — theory';
  if (/loading sentences/i.test(progress)) return 'building — sentences';
  return 'building';
}

/** The theory's most recent job that recorded its build parameters, if any. */
export function lastJobWithParams(jobs: Job[], theoryId: string): Job | null {
  let best: Job | null = null;
  for (const j of jobs) {
    if (j.theory_id !== theoryId || !j.build_params) continue;
    if (!best || j.created_at > best.created_at) best = j;
  }
  return best;
}
