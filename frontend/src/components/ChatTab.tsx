import React from 'react';
import { useChat } from '../useChat';
import { TheorySummary } from '../types';
import { expandOntologyName } from '../ontology';
import { Transcript } from './Transcript';
import { Composer } from './Composer';

interface ChatTabProps {
  theories: TheorySummary[];
  selectedId: string;
  onSelect: (id: string) => void;
}

/**
 * Tab 1: the chatbot. A dropdown picks which theory to query; the chosen id is
 * passed to useChat so /ask and /lucky target that theory.
 */
export function ChatTab({ theories, selectedId, onSelect }: ChatTabProps) {
  const { title, messages, isBusy, send, askLucky, fetchLuckyChoices } = useChat(selectedId);
  const ready = theories.filter((t) => t.status === 'ready');

  return (
    <div className="chat-tab">
      <div className="theory-select-row">
        <label htmlFor="theory-select">Theory:</label>
        <select
          id="theory-select"
          value={selectedId}
          onChange={(e) => onSelect(e.target.value)}
          disabled={isBusy || ready.length === 0}
        >
          {ready.length === 0 && <option value="">(no ready theories)</option>}
          {ready.map((t) => (
            <option key={t.id} value={t.id}>
              {expandOntologyName(t.name)}
            </option>
          ))}
        </select>
        {title && <span className="theory-subject">{title}</span>}
      </div>
      <div className="chat-container">
        <Transcript messages={messages} />
        <Composer
          isBusy={isBusy}
          disabled={!selectedId}
          disabledHint="No theory is ready yet. Build one in the Theories tab, then ask it questions here."
          onSend={send}
          fetchLuckyChoices={fetchLuckyChoices}
          onPickLucky={(q) => askLucky(q)}
        />
      </div>
    </div>
  );
}
