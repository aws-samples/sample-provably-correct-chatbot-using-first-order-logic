// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { TheorySummary } from '../types';
import { useSuperChat } from '../useSuperChat';
import { Transcript } from './Transcript';
import { Composer } from './Composer';

const SELECTED_KEY = 'chatbot.superChatTheories';
// Matches MAX_SUPER_CHAT_THEORIES on the server.
export const MAX_SUPER_CHAT_THEORIES = 8;

function loadSelected(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(SELECTED_KEY) ?? '[]');
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

interface SuperChatTabProps {
  theories: TheorySummary[];
}

/**
 * Super Chat: one question asked of several theories at once. The theories
 * are picked in a multi-choice dropdown (ready theories only; remembered).
 * Each proved answer is shown with the theory it came from.
 */
export function SuperChatTab({ theories }: SuperChatTabProps) {
  const ready = useMemo(() => theories.filter((t) => t.status === 'ready'), [theories]);
  const [picked, setPicked] = useState<string[]>(loadSelected);
  useEffect(() => {
    localStorage.setItem(SELECTED_KEY, JSON.stringify(picked));
  }, [picked]);
  // Only ready theories can be asked; a picked one that is rebuilding or was
  // deleted drops out of the question (and comes back when ready again).
  const selected = useMemo(
    () => picked.filter((id) => ready.some((t) => t.id === id)),
    [picked, ready]
  );
  const { messages, isBusy, send, fetchLuckyChoices } = useSuperChat(selected);

  const toggle = (id: string) =>
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));

  const hint =
    ready.length === 0
      ? 'No theory is ready yet. Build one in the Theories tab.'
      : 'Choose the theories to ask.';

  return (
    <div className="chat-tab super-chat-tab">
      <div className="chat-subject-row super-chat-row">
        <TheoryPicker
          ready={ready}
          selected={selected}
          onToggle={toggle}
          disabled={isBusy}
        />
        <span className="theory-subject">
          Each question goes to every chosen theory at once.
        </span>
      </div>
      <div className="chat-container">
        <Transcript messages={messages} />
        <Composer
          isBusy={isBusy}
          disabled={selected.length === 0}
          disabledHint={hint}
          onSend={send}
          fetchLuckyChoices={fetchLuckyChoices}
          onPickLucky={(q) => send(q)}
        />
      </div>
    </div>
  );
}

interface TheoryPickerProps {
  ready: TheorySummary[];
  selected: string[];
  onToggle: (id: string) => void;
  disabled: boolean;
}

/** A dropdown of checkboxes, one per ready theory. */
function TheoryPicker({ ready, selected, onToggle, disabled }: TheoryPickerProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  // Close when clicking outside or pressing Escape.
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const names = ready.filter((t) => selected.includes(t.id)).map((t) => t.name);
  const summary =
    names.length === 0
      ? 'Choose theories'
      : names.length <= 2
      ? names.join(', ')
      : `${names.length} theories`;
  const full = selected.length >= MAX_SUPER_CHAT_THEORIES;

  return (
    <div className="theory-picker" ref={ref}>
      <button
        type="button"
        className="theory-picker-button"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        disabled={disabled || ready.length === 0}
      >
        Theories: {summary} ▾
      </button>
      {open && (
        <div className="theory-picker-menu" role="group" aria-label="Theories to ask">
          {ready.map((t) => {
            const checked = selected.includes(t.id);
            return (
              <label key={t.id} className="theory-picker-option">
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={!checked && full}
                  onChange={() => onToggle(t.id)}
                />
                {t.name}
              </label>
            );
          })}
          {full && (
            <p className="muted small">
              At most {MAX_SUPER_CHAT_THEORIES} theories per question.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
