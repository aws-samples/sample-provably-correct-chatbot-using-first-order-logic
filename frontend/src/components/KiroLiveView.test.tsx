// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { KIRO_POLL_MS, KiroLiveView, classifyKiroLine } from './KiroLiveView';

type Reply = { lines: string[]; cursor: string | null; omitted?: number; note?: string | null };

function mockKiro(replies: Reply[]) {
  const urls: string[] = [];
  global.fetch = jest.fn((input: RequestInfo | URL) => {
    urls.push(String(input));
    const r = replies.shift() ?? { lines: [], cursor: null };
    const body = { omitted: 0, incomplete: false, note: null, ...r };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);
  }) as jest.Mock;
  return urls;
}

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

test('classifies session starts, harness verdicts and agent output', () => {
  expect(classifyKiroLine('kc: $ kiro-cli chat --resume <message>')).toEqual({
    kind: 'session', text: '$ kiro-cli chat --resume <message>',
  });
  expect(classifyKiroLine('kcvut: attempt #1 verified: valid=True correct=40/55 needed').kind)
    .toBe('verdict');
  expect(classifyKiroLine('kc: ● Reading sentences.txt')).toEqual({
    kind: 'line', text: '● Reading sentences.txt',
  });
});

test('shows the transcript with numbered session dividers', async () => {
  mockKiro([{
    cursor: '3:e2',
    lines: ['kc: $ kiro-cli chat --no-interactive', 'kc: writing theory.smt2',
            'kcvut: attempt #1 verified: valid=True correct=40/55 needed',
            'kc: $ kiro-cli chat --no-interactive --resume'],
  }]);
  render(<KiroLiveView jobId="job-1" live={false} />);
  expect(await screen.findByText('Session 1 started')).toBeInTheDocument();
  expect(screen.getByText('Session 2 started')).toBeInTheDocument();
  expect(screen.getByText('writing theory.smt2')).toBeInTheDocument();
  expect(screen.getByText(/correct=40\/55/)).toHaveClass('kiro-verdict');
  expect(screen.getByRole('status')).toHaveTextContent('Build ended · 4 lines');
});

test('a running build appends only new lines, sending the cursor back', async () => {
  jest.useFakeTimers();
  const urls = mockKiro([
    { cursor: '100:e0', lines: ['kc: first'] },
    { cursor: '200:e1', lines: ['kc: second'] },
  ]);
  render(<KiroLiveView jobId="job-1" live />);
  expect(await screen.findByText('first')).toBeInTheDocument();
  await act(async () => {
    jest.advanceTimersByTime(KIRO_POLL_MS);
  });
  expect(await screen.findByText('second')).toBeInTheDocument();
  // Nothing repeated.
  expect(screen.getAllByText('first')).toHaveLength(1);
  expect(screen.getAllByText('second')).toHaveLength(1);
  expect(screen.getByRole('status')).toHaveTextContent('2 lines');
  expect(urls[0]).not.toContain('cursor=');
  expect(urls[1]).toContain('cursor=100%3Ae0');
});

test('says when older lines were left out and stops numbering sessions', async () => {
  mockKiro([{ cursor: '9:e9', omitted: 120, lines: ['kc: $ kiro-cli chat --resume'] }]);
  render(<KiroLiveView jobId="job-1" live={false} />);
  expect(await screen.findByText('Session started')).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('120 older not shown');
});

test('shows the server note before any output', async () => {
  mockKiro([{ cursor: null, lines: [], note: 'No kiro-cli output yet: the first session starts after the test set is ready.' }]);
  render(<KiroLiveView jobId="job-1" live={false} />);
  expect(await screen.findByText(/No kiro-cli output yet/)).toBeInTheDocument();
});
