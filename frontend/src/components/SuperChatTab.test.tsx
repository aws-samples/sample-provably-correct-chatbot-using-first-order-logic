// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import type { Mock } from 'vitest';
import React from 'react';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SuperChatTab } from './SuperChatTab';
import { superChatReply } from '../useSuperChat';
import { TheorySummary, TheoryStatus } from '../types';

const theory = (id: string, name: string, status: TheoryStatus = 'ready'): TheorySummary => ({
  id, name, status, sentence_count: 3, created_at: 1700000000, job_id: null, doc_type: 'text',
});
const THEORIES = [
  theory('a', 'US elections'), theory('b', 'Hypertension'), theory('c', 'Tides', 'building'),
];

type Reply = { theory_id: string; theory_name: string; answer: string; verified: boolean;
               question_logic?: string | null; answer_logic?: string | null };

function mockAsk(replies: (ids: string[]) => Reply[]) {
  const calls: { theory_ids: string[]; text: string }[] = [];
  global.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const ok = (body: unknown) =>
      Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);
    if (url.endsWith('/ask-multi')) {
      const body = JSON.parse(String(init!.body));
      calls.push(body);
      return ok({ answers: replies(body.theory_ids) });
    }
    if (url.includes('/lucky-questions')) return ok({ questions: [] });
    return ok({});
  }) as Mock;
  return calls;
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

async function choose(...names: string[]) {
  await userEvent.click(screen.getByRole('button', { name: /^Theories:/ }));
  const menu = screen.getByRole('group', { name: 'Theories to ask' });
  for (const n of names) await userEvent.click(within(menu).getByLabelText(n));
  await userEvent.keyboard('{Escape}');
}

async function ask(text: string) {
  await userEvent.type(screen.getByLabelText('Ask a question'), text);
  await userEvent.click(screen.getByRole('button', { name: 'Send' }));
}

test('only ready theories can be chosen, and nothing can be asked until one is', async () => {
  mockAsk(() => []);
  render(<SuperChatTab theories={THEORIES} />);
  expect(screen.getByLabelText('Ask a question')).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Theories: Choose theories ▾' }));
  const menu = screen.getByRole('group', { name: 'Theories to ask' });
  expect(within(menu).getAllByRole('checkbox')).toHaveLength(2);
  expect(within(menu).queryByLabelText('Tides')).not.toBeInTheDocument();
});

test('a question goes to every chosen theory and each proved answer names its theory', async () => {
  const calls = mockAsk(() => [
    { theory_id: 'a', theory_name: 'US elections', answer: 'The Democratic Party.',
      verified: true, question_logic: '(q)', answer_logic: '(m)' },
    { theory_id: 'b', theory_name: 'Hypertension', answer: "I don't know", verified: false },
  ]);
  render(<SuperChatTab theories={THEORIES} />);
  await choose('US elections', 'Hypertension');
  expect(screen.getByRole('button', { name: 'Theories: US elections, Hypertension ▾' }))
    .toBeInTheDocument();
  await ask('Which party won?');
  const list = await screen.findByRole('list', { name: 'Answers by theory' });
  expect(calls).toEqual([{ text: 'Which party won?', theory_ids: ['a', 'b'] }]);
  const items = within(list).getAllByRole('listitem');
  expect(items).toHaveLength(1);
  expect(items[0]).toHaveTextContent('US elections');
  expect(items[0]).toHaveTextContent('The Democratic Party.');
  expect(screen.getByText('No answer from Hypertension.')).toBeInTheDocument();
  await userEvent.click(within(items[0]).getByRole('button', { name: 'Show logical form' }));
  expect(within(items[0]).getByLabelText('Logical form')).toHaveTextContent('(q)');
  expect(within(items[0]).getByLabelText('Logical form')).toHaveTextContent('(m)');
});

test('when no theory proves an answer the reply is I don\'t know', async () => {
  mockAsk((ids) => ids.map((id) => ({ theory_id: id, theory_name: id, answer: "I don't know",
                                        verified: false })));
  render(<SuperChatTab theories={THEORIES} />);
  await choose('US elections', 'Hypertension');
  await ask('Unknowable?');
  expect(await screen.findByText("I don't know")).toBeInTheDocument();
  expect(screen.getByText(/Unverifiable/)).toBeInTheDocument();
  expect(screen.queryByRole('list', { name: 'Answers by theory' })).not.toBeInTheDocument();
});

test('the chosen theories are remembered', async () => {
  mockAsk(() => []);
  const { unmount } = render(<SuperChatTab theories={THEORIES} />);
  await choose('Hypertension');
  unmount();
  render(<SuperChatTab theories={THEORIES} />);
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Theories: Hypertension ▾' })).toBeInTheDocument());
});

test('superChatReply lists every proved answer', () => {
  const reply = superChatReply(7, [
    { theory_id: 'a', theory_name: 'A', answer: 'Yes.', verified: true },
    { theory_id: 'b', theory_name: 'B', answer: 'No.', verified: true },
  ]);
  expect(reply.verified).toBe(true);
  expect(reply.answers!.map((x) => [x.theoryName, x.text])).toEqual([['A', 'Yes.'], ['B', 'No.']]);
  expect(reply.unanswered).toEqual([]);
});
