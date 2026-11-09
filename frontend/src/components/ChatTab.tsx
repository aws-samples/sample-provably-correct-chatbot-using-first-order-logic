// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { useChat } from '../useChat';
import { TheorySummary } from '../types';
import { Transcript } from './Transcript';
import { Composer } from './Composer';

interface ChatTabProps {
  // The theory this tab chats with. Each "Chat (<name>)" tab is bound to one
  // theory, opened from the Theories tab.
  theoryId: string;
  // That theory's summary, or undefined while the list loads or once it has
  // been deleted.
  theory: TheorySummary | undefined;
}

/**
 * A chatbot tab for one theory: /ask and /lucky target `theoryId`. Questions
 * can be asked only while the theory is ready (not while it rebuilds).
 */
export function ChatTab({ theoryId, theory }: ChatTabProps) {
  const ready = theory?.status === 'ready';
  const { title, messages, isBusy, send, askLucky, fetchLuckyChoices } = useChat(
    ready ? theoryId : undefined
  );

  const hint = !theory
    ? 'This theory no longer exists. Close this tab, or open a chat from the Theories tab.'
    : `This theory is not ready (status: ${theory.status}). Questions can be asked once it is.`;

  return (
    <div className="chat-tab">
      {title && (
        <div className="chat-subject-row">
          <span className="theory-subject">{title}</span>
        </div>
      )}
      <div className="chat-container">
        <Transcript messages={messages} />
        <Composer
          isBusy={isBusy}
          disabled={!ready}
          disabledHint={hint}
          onSend={send}
          fetchLuckyChoices={fetchLuckyChoices}
          onPickLucky={(q) => askLucky(q)}
        />
      </div>
    </div>
  );
}
