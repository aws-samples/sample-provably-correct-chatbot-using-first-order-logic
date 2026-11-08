// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import { LuckyPickerBubble } from './LuckyPickerBubble';

interface ComposerProps {
  isBusy: boolean;
  // No theory to ask yet: the controls are disabled and `disabledHint` says why.
  disabled?: boolean;
  disabledHint?: string;
  onSend: (text: string) => void;
  // Loads the current theory's suggested questions for the lucky bubble.
  fetchLuckyChoices: () => Promise<string[]>;
  // Called with the question the user picks from the bubble.
  onPickLucky: (question: string) => void;
}

export function Composer({
  isBusy,
  disabled = false,
  disabledHint,
  onSend,
  fetchLuckyChoices,
  onPickLucky,
}: ComposerProps) {
  const [input, setInput] = useState('');
  // The "I'm Feeling Lucky" speech bubble: anchored to the lucky button,
  // slides up when opened.
  const [luckyOpen, setLuckyOpen] = useState(false);

  const off = isBusy || disabled;
  const hintId = disabled && disabledHint ? 'composer-hint' : undefined;

  const submit = () => {
    if (!input.trim() || off) return;
    onSend(input);
    setInput('');
  };

  // onKeyDown, not onKeyPress (deprecated in React 19).
  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') submit();
  };

  return (
    <div className="input-area">
      {hintId && (
        <p id={hintId} className="muted small composer-hint">
          {disabledHint}
        </p>
      )}
      <input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Ask a question..."
        aria-label="Ask a question"
        aria-describedby={hintId}
        disabled={off}
      />
      <button onClick={submit} disabled={off}>
        Send
      </button>
      {/* Anchor wraps the button so the bubble can position above it. */}
      <div className="lucky-bubble-anchor">
        {luckyOpen && (
          <LuckyPickerBubble
            fetchChoices={fetchLuckyChoices}
            onPick={(q) => {
              setLuckyOpen(false);
              onPickLucky(q);
            }}
            onClose={() => setLuckyOpen(false)}
          />
        )}
        <button
          onClick={() => setLuckyOpen((o) => !o)}
          disabled={off}
          aria-expanded={luckyOpen}
          aria-haspopup="dialog"
        >
          I'm Feeling Lazy
        </button>
      </div>
    </div>
  );
}
