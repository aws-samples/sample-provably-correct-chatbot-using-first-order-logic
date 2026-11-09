// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useCallback, useRef, useState } from 'react';
import { askMultipleTheories, fetchLuckyQuestions, MultiAnswer, UNKNOWN_ANSWER } from './api';
import { Message, TheoryAnswer } from './types';

const PENDING_TEXT = 'Asking the theories…';

export interface UseSuperChat {
  messages: Message[];
  isBusy: boolean;
  send: (text: string) => Promise<void>;
  fetchLuckyChoices: () => Promise<string[]>;
}

/**
 * The bot message for a Super Chat reply: every proved answer, labelled with
 * its theory, or "I don't know" when no theory proved one.
 */
export function superChatReply(id: number, replies: MultiAnswer[]): Message {
  const proved: TheoryAnswer[] = replies
    .filter((r) => r.verified)
    .map((r) => ({
      theoryId: r.theory_id,
      theoryName: r.theory_name,
      text: r.answer,
      ...(r.question_logic ? { questionLogic: r.question_logic } : {}),
      ...(r.answer_logic ? { answerLogic: r.answer_logic } : {}),
    }));
  if (proved.length === 0) {
    return { id, kind: 'bot', text: UNKNOWN_ANSWER, verified: false };
  }
  return {
    id,
    kind: 'bot',
    text: proved.map((a) => `${a.theoryName}: ${a.text}`).join('\n'),
    verified: true,
    answers: proved,
    unanswered: replies.filter((r) => !r.verified).map((r) => r.theory_name),
  };
}

/** Interleave lists (a1, b1, a2, b2, ...), dropping repeats. */
function interleave(lists: string[][]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (let i = 0; lists.some((l) => i < l.length); i++) {
    for (const l of lists) {
      const q = l[i];
      if (q !== undefined && !seen.has(q.toLowerCase())) {
        seen.add(q.toLowerCase());
        out.push(q);
      }
    }
  }
  return out;
}

/**
 * The Super Chat conversation: each question goes to every theory in
 * `theoryIds` at once (POST /ask-multi). The selection is read when a
 * question is sent, so changing it affects only later questions.
 */
export function useSuperChat(theoryIds: string[]): UseSuperChat {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isBusy, setIsBusy] = useState(false);
  const nextId = useRef(0);
  const makeId = () => nextId.current++;

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || isBusy || theoryIds.length === 0) return;
      const userId = makeId();
      const pendingId = makeId();
      setMessages((m) => [
        ...m,
        { id: userId, kind: 'user', text: trimmed },
        { id: pendingId, kind: 'pending', text: PENDING_TEXT },
      ]);
      setIsBusy(true);
      let reply: Message;
      try {
        const { answers } = await askMultipleTheories(trimmed, theoryIds);
        reply = superChatReply(pendingId, answers ?? []);
      } catch (e) {
        const why = e instanceof Error && e.message ? ` (${e.message})` : '';
        reply = { id: pendingId, kind: 'error', text: `Could not ask the theories${why}.` };
      }
      setMessages((m) => m.map((x) => (x.id === pendingId ? reply : x)));
      setIsBusy(false);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [isBusy, theoryIds]
  );

  // The suggested questions of every chosen theory, interleaved.
  const fetchLuckyChoices = useCallback(async (): Promise<string[]> => {
    const lists = await Promise.all(
      theoryIds.map((id) =>
        fetchLuckyQuestions(id)
          .then((d) => d.questions ?? [])
          .catch(() => [] as string[])
      )
    );
    return interleave(lists);
  }, [theoryIds]);

  return { messages, isBusy, send, fetchLuckyChoices };
}
