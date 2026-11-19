import { useCallback, useEffect, useRef, useState } from 'react';
import {
  askQuestion,
  fetchLuckyQuestion,
  fetchLuckyQuestions,
  fetchSubject,
  isVerifiedAnswer,
} from './api';
import { Message } from './types';

const FALLBACK_TITLE = 'Provably-correct chatbot';
const PENDING_TEXT = 'Thinking…';

export interface UseChat {
  title: string;
  messages: Message[];
  isBusy: boolean;
  send: (text: string) => Promise<void>;
  askLucky: (question?: string) => Promise<void>;
  fetchLuckyChoices: () => Promise<string[]>;
}

export function useChat(theoryId?: string): UseChat {
  const [title, setTitle] = useState(theoryId ? FALLBACK_TITLE : '');
  // Rendered transcript for the CURRENTLY selected theory.
  const [messages, setMessages] = useState<Message[]>([]);
  const [isBusy, setIsBusy] = useState(false);

  // One conversation history per theory, keyed by theory id, so switching the
  // theory selector swaps to that theory's own transcript instead of carrying
  // the previous theory's messages over. The empty string keys the
  // "(no theory)" case. This ref is the source of truth; `messages` mirrors
  // the active theory's slice for rendering.
  const historiesRef = useRef<Record<string, Message[]>>({});
  // The theory id whose history is currently loaded into `messages`.
  const activeKeyRef = useRef<string>('');

  // Monotonic id source for message keys; a ref so it survives re-renders.
  // Shared across theories — only one theory renders at a time, and global
  // uniqueness keeps keys stable even if a message is moved between views.
  const nextId = useRef(0);
  const makeId = () => nextId.current++;

  const keyFor = (id?: string) => id ?? '';

  // On theory switch: load the newly selected theory's stored transcript
  // (empty if it has never been used). Histories are kept live by appendTo /
  // replaceIn as messages arrive, so there is nothing extra to persist here.
  useEffect(() => {
    const key = keyFor(theoryId);
    activeKeyRef.current = key;
    setMessages(historiesRef.current[key] ?? []);
  }, [theoryId]);

  useEffect(() => {
    // No theory selected (e.g. none are ready): show no subtitle. Without a
    // theory_id the backend would describe the subject the server process
    // happened to load at startup, claiming expertise it cannot serve.
    if (!theoryId) {
      setTitle('');
      return undefined;
    }
    let cancelled = false;
    fetchSubject(theoryId)
      .then((data) => {
        if (!cancelled && data.title) setTitle(data.title);
      })
      .catch(() => {
        // Non-blocking fallback title on failure.
        if (!cancelled) setTitle(FALLBACK_TITLE);
      });
    return () => {
      cancelled = true;
    };
  }, [theoryId]);

  // Run a question through the backend with a shared loading/error lifecycle.
  // The user bubble for `text` must already be appended.
  // Captures the theory id at call time so a late response lands in the right
  // history even if the user switches theories while it's in flight.
  const runQuestion = useCallback(async (text: string, askTheoryId?: string) => {
    const key = keyFor(askTheoryId);
    const pendingId = makeId();
    setIsBusy(true);
    appendTo(key, { id: pendingId, kind: 'pending', text: PENDING_TEXT });

    try {
      const { answer } = await askQuestion(text, askTheoryId);
      replaceIn(key, pendingId, {
        id: pendingId,
        kind: 'bot',
        text: answer,
        verified: isVerifiedAnswer(answer),
      });
    } catch {
      replaceIn(key, pendingId, {
        id: pendingId,
        kind: 'error',
        text: 'Error connecting to the server. Please try again.',
      });
    } finally {
      setIsBusy(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Mutate a specific theory's history by key. If that key is the one being
  // rendered, mirror the change into `messages` too.
  const appendTo = useCallback((key: string, msg: Message) => {
    const prev = historiesRef.current[key] ?? [];
    const next = [...prev, msg];
    historiesRef.current[key] = next;
    if (activeKeyRef.current === key) setMessages(next);
  }, []);

  const replaceIn = useCallback(
    (key: string, id: number, msg: Message) => {
      const prev = historiesRef.current[key] ?? [];
      const next = prev.map((m) => (m.id === id ? msg : m));
      historiesRef.current[key] = next;
      if (activeKeyRef.current === key) setMessages(next);
    },
    []
  );

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || isBusy) return; // no blank/whitespace, no concurrent sends.
      const askTheoryId = theoryId;
      appendTo(keyFor(askTheoryId), {
        id: makeId(),
        kind: 'user',
        text: trimmed,
      });
      await runQuestion(trimmed, askTheoryId);
    },
    [isBusy, runQuestion, appendTo, theoryId]
  );

  // Fetch the theory-specific suggested questions for the picker. Does not
  // touch the transcript or the busy guard — the caller (picker) renders the
  // returned list and asks the chosen one via askLucky(question).
  const fetchLuckyChoices = useCallback(async (): Promise<string[]> => {
    const data = await fetchLuckyQuestions(theoryId);
    return data.questions ?? [];
  }, [theoryId]);

  // Ask a "lucky" question. When `chosen` is given (the user picked one from
  // the picker), ask it directly. Otherwise fall back to the backend's
  // generated question (/lucky).
  const askLucky = useCallback(async (chosen?: string) => {
    if (isBusy) return;
    const askTheoryId = theoryId;
    setIsBusy(true);
    let question: string;
    if (chosen && chosen.trim()) {
      question = chosen.trim();
    } else {
      try {
        const data = await fetchLuckyQuestion(askTheoryId);
        question = data.question;
      } catch {
        // Surface a visible error, consistent with send().
        appendTo(keyFor(askTheoryId), {
          id: makeId(),
          kind: 'error',
          text: 'Could not fetch a question. Please try again.',
        });
        setIsBusy(false);
        return;
      }
    }
    // Hand off to the normal path. runQuestion resets isBusy; set the user
    // bubble first, then release the guard so send-equivalent flow proceeds.
    appendTo(keyFor(askTheoryId), {
      id: makeId(),
      kind: 'user',
      text: question,
    });
    setIsBusy(false);
    await runQuestion(question, askTheoryId);
  }, [isBusy, runQuestion, appendTo, theoryId]);

  return { title, messages, isBusy, send, askLucky, fetchLuckyChoices };
}
