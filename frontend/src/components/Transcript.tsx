import React, { useEffect, useRef } from 'react';
import { Message } from '../types';
import { MessageBubble } from './MessageBubble';

interface TranscriptProps {
  messages: Message[];
}

export function Transcript({ messages }: TranscriptProps) {
  const endRef = useRef<HTMLDivElement>(null);

  // Auto-scroll the newest message into view.
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: 'smooth' });
  }, [messages]);

  return (
    <div
      className="messages"
      role="log"
      aria-live="polite"
      aria-relevant="additions"
    >
      {messages.map((msg) => (
        <MessageBubble key={msg.id} message={msg} />
      ))}
      <div ref={endRef} />
    </div>
  );
}
