// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import { Message } from '../types';

interface MessageBubbleProps {
  message: Message;
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const { kind, text, verified, logic, answers, unanswered } = message;

  // Distinguish verified answers from the "I don't know" fallback.
  const isUnknown = kind === 'bot' && verified === false;
  const isVerified = kind === 'bot' && verified === true;

  const classes = ['message', kind];
  if (isUnknown) classes.push('unverified');
  if (isVerified) classes.push('verified');
  if (logic) classes.push('has-logic');
  if (answers) classes.push('multi-answer');

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
      {answers ? (
        <>
          <ul className="theory-answers" aria-label="Answers by theory">
            {answers.map((a) => (
              <li key={a.theoryId} className="theory-answer">
                <span className="theory-answer-source">{a.theoryName}</span>
                <FlippableText
                  text={a.text}
                  logic={joinLogic(a.questionLogic, a.answerLogic)}
                />
              </li>
            ))}
          </ul>
          {unanswered && unanswered.length > 0 && (
            <span className="theory-answers-none">
              No answer from {unanswered.join(', ')}.
            </span>
          )}
        </>
      ) : (
        <FlippableText text={text} logic={logic} />
      )}
    </div>
  );
}

/** A Super Chat answer flips to its question's SMT-LIB and the model. */
function joinLogic(question?: string, answer?: string): string | undefined {
  const parts = [
    question ? `; question\n${question}` : null,
    answer ? `; answer\n${answer}` : null,
  ].filter(Boolean);
  return parts.length ? parts.join('\n\n') : undefined;
}

/**
 * Text with, when it has a logical form, a circular-arrows button on its
 * right that flips between the two.
 */
function FlippableText({ text, logic }: { text: string; logic?: string }) {
  const [showLogic, setShowLogic] = useState(false);
  const logical = showLogic && !!logic;
  const flipLabel = logical ? 'Show natural language' : 'Show logical form';
  return (
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
  );
}
