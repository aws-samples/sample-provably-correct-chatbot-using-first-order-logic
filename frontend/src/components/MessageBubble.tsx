import React from 'react';
import { Message } from '../types';

interface MessageBubbleProps {
  message: Message;
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const { kind, text, verified } = message;

  // Distinguish verified answers from the "I don't know" fallback.
  const isUnknown = kind === 'bot' && verified === false;
  const isVerified = kind === 'bot' && verified === true;

  const classes = ['message', kind];
  if (isUnknown) classes.push('unverified');
  if (isVerified) classes.push('verified');

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
      <span className="message-text">{text}</span>
    </div>
  );
}
