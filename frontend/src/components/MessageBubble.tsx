// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import { Message } from '../types';

interface MessageBubbleProps {
  message: Message;
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const { kind, text, verified, logic } = message;
  // Whether the bubble shows its logical form instead of the text.
  const [showLogic, setShowLogic] = useState(false);
  const logical = showLogic && !!logic;

  // Distinguish verified answers from the "I don't know" fallback.
  const isUnknown = kind === 'bot' && verified === false;
  const isVerified = kind === 'bot' && verified === true;

  const classes = ['message', kind];
  if (isUnknown) classes.push('unverified');
  if (isVerified) classes.push('verified');
  if (logic) classes.push('has-logic');

  const flipLabel = logical ? 'Show natural language' : 'Show logical form';

  return (
    <div
      className={classes.join(' ')}
      aria-busy={kind === 'pending' || undefined}
    >
      {isVerified && (
        <span className="badge badge-verified" title="Checked by the theorem prover">
          ✓ Verified
        </span>
      )}
      {isUnknown && (
        <span className="badge badge-unverified" title="No provable answer">
          Unverifiable
        </span>
      )}
      <div className="message-body">
        {logical ? (
          <pre className="message-logic" aria-label="Logical form">{logic}</pre>
        ) : (
          <span className="message-text">{text}</span>
        )}
        {logic && (
          <button
            type="button"
            className="logic-toggle"
            onClick={() => setShowLogic((v) => !v)}
            aria-label={flipLabel}
            aria-pressed={logical}
            title={flipLabel}
          >
            <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"
                 fill="none" stroke="currentColor" strokeWidth="2"
                 strokeLinecap="round" strokeLinejoin="round">
              {/* Two circular arrows chasing each other. */}
              <path d="M20 11a8 8 0 0 0-14.3-4.9L4 8" />
              <path d="M4 3v5h5" />
              <path d="M4 13a8 8 0 0 0 14.3 4.9L20 16" />
              <path d="M20 21v-5h-5" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}
