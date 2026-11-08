// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { JobIntrospectionTab, buildStatus, phaseLabel, verifiedLine } from './JobIntrospectionTab';
import { IntrospectionState, Job, JobIntrospection } from '../types';

const JOB: Job = {
  id: 'job-1',
  theory_id: 't1',
  theory_name: 'Photosynthesis',
  status: 'finished',
  progress: 'done',
  log_tail: '',
  error: null,
  created_at: 1700000000,
  started_at: 1700000000,
  finished_at: 1700003600,
};

const STATE: IntrospectionState = {
  version: 2,
  job_id: 'job-1',
  updated_at: 1700003600,
  status: 'succeeded',
  phase: 'done',
  started_at: 1700000000,
  finished_at: 1700003600,
  duration: 3600,
  n_sentences: 3,
  sentences: ['Plants photosynthesize.', 'Chlorophyll is green.', 'Photosynthesis releases oxygen.'],
  raw_sentences: ['Plants photosynthesize.', 'Chlorophyll is green.', 'It releases oxygen.'],
  standalone_sentences: ['Plants photosynthesize.', 'Chlorophyll is green.', 'Photosynthesis releases oxygen.'],
  n_questions: 3,
  n_required: 2,
  n_correct: 2,
  theory_chars: 20,
  sessions: [
    {
      session: 1, kind: 'build', status: 'done', started_at: 1700000100,
      finished_at: 1700001900, duration: 1800, rc: 0, stalled: false, credits: 0.69,
      valid: true, n_correct: 1,
    },
    {
      session: 2, kind: 'feedback', status: 'done', started_at: 1700002000,
      finished_at: 1700003500, duration: 1500, rc: 0, stalled: false, credits: null,
      valid: true, n_correct: 2,
    },
  ],
};

const INTROSPECTION: JobIntrospection = {
  job: JOB,
  available: true,
  note: null,
  state: STATE,
  theory: '(declare-sort Plant 0)',
  qas: {
    n_required: 2,
    questions: [
      { question: 'What is green?', expected_answer: 'Chlorophyll.', correct: true },
      { question: 'What is released?', expected_answer: 'Oxygen.', correct: false },
    ],
  },
  cost: {
    credits: 0.69, dollars: 0.0276, dollars_per_credit: 0.04, sessions: 2,
    unmetered_sessions: 1,
    per_session: [
      { session: 1, credits: 0.69, dollars: 0.0276 },
      { session: 2, credits: null, dollars: null },
    ],
  },
};

function mockFetch(routes: Record<string, unknown>) {
  global.fetch = jest.fn((input: RequestInfo | URL) => {
    const url = String(input);
    // Longest matching route wins, so /introspection/logs beats /introspection.
    const key = Object.keys(routes)
      .filter((k) => url.includes(k))
      .sort((a, b) => b.length - a.length)[0];
    const body = key ? routes[key] : {};
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);
  }) as jest.Mock;
}

afterEach(() => jest.restoreAllMocks());

test('summarizes the build: status, phases, counts and cost', async () => {
  mockFetch({ '/introspection': INTROSPECTION });
  render(<JobIntrospectionTab jobs={[JOB]} jobId="job-1" />);
  const summary = await screen.findByRole('region', { name: 'Build summary' });
  expect(within(summary).getByText('succeeded')).toBeInTheDocument();
  const phases = within(summary).getByRole('list', { name: 'Build phases' });
  expect(within(phases).getAllByRole('listitem')).toHaveLength(4);
  expect(summary).toHaveTextContent('3 sentences');
  expect(summary).toHaveTextContent('2/3 verified (2 needed)');
  expect(summary).toHaveTextContent('duration: 1.0 hours');
  expect(summary).toHaveTextContent('0.69 credits · $0.03 · 2 sessions · 1 unmetered (lower bound)');
});

test('lists every kiro-cli session with its kind, cost and verified count', async () => {
  mockFetch({ '/introspection': INTROSPECTION });
  render(<JobIntrospectionTab jobs={[JOB]} jobId="job-1" />);
  const table = await screen.findByRole('table', { name: 'kiro-cli sessions' });
  const rows = within(table).getAllByRole('row').slice(1);
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveTextContent('first build');
  expect(rows[0]).toHaveTextContent('0.69 credits · $0.03');
  expect(rows[0]).toHaveTextContent('1/2 needed');
  expect(rows[1]).toHaveTextContent('fix after verification');
  expect(rows[1]).toHaveTextContent('not metered');
});

test('the views show sentences, the test set and the theory', async () => {
  mockFetch({ '/introspection': INTROSPECTION });
  render(<JobIntrospectionTab jobs={[JOB]} jobId="job-1" />);
  const sentences = await screen.findByRole('table', { name: 'Raw and de-contextualized sentences' });
  const rows = within(sentences).getAllByRole('row').slice(1);
  expect(rows).toHaveLength(3);
  expect(rows[0]).toHaveTextContent('Plants photosynthesize.unchanged');
  expect(rows[2]).toHaveTextContent('It releases oxygen.Photosynthesis releases oxygen.');
  expect(screen.getByRole('tabpanel')).toHaveTextContent('rewrote 1 of them');

  fireEvent.click(screen.getByRole('tab', { name: 'Test set' }));
  const qas = screen.getByRole('table', { name: 'Test questions and expected answers' });
  expect(within(qas).getAllByRole('row')).toHaveLength(3);
  expect(screen.getByText('✓ correct')).toBeInTheDocument();
  expect(screen.getByRole('tabpanel')).toHaveTextContent('1 of 2 credited correct');

  fireEvent.click(screen.getByRole('tab', { name: 'Theory' }));
  expect(screen.getByRole('tabpanel')).toHaveTextContent('(declare-sort Plant 0)');
  expect(screen.getByRole('tabpanel')).toHaveTextContent('The final theory.');
});

test('the logs view reads the whole build log and offers a download', async () => {
  mockFetch({
    '/introspection/logs': {
      source: 'cloudwatch', lines: ['start', 'kc: $ kiro-cli chat'], truncated: false,
      note: null, total_lines: 2, omitted: 0,
    },
    '/introspection': INTROSPECTION,
  });
  render(<JobIntrospectionTab jobs={[JOB]} jobId="job-1" />);
  fireEvent.click(await screen.findByRole('tab', { name: 'Logs' }));
  await waitFor(() => expect(screen.getByRole('tabpanel')).toHaveTextContent('kc: $ kiro-cli chat'));
  expect(screen.getByText('2 lines.')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Download all lines' })).toHaveAttribute(
    'href',
    expect.stringContaining('/api/jobs/job-1/introspection/logs.txt')
  );
  const calls = (global.fetch as jest.Mock).mock.calls.map((c) => String(c[0]));
  expect(calls.some((u) => u.includes('/introspection/logs?head=500&tail=1500'))).toBe(true);
});

test('says when nothing was recorded', async () => {
  mockFetch({
    '/introspection': {
      job: JOB, available: false, state: null, theory: null, qas: null, cost: null,
      note: 'No progress was recorded for this job.',
    },
  });
  render(<JobIntrospectionTab jobs={[JOB]} jobId="job-1" />);
  expect(await screen.findByText('No progress was recorded for this job.')).toBeInTheDocument();
  expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
});

test('a running build whose job ended is shown as stopped', () => {
  expect(buildStatus({ ...STATE, status: 'running' }, { ...JOB, status: 'cancelled' })).toBe('stopped');
  expect(buildStatus({ ...STATE, status: 'running' }, { ...JOB, status: 'running' })).toBe('running');
  expect(buildStatus(STATE, JOB)).toBe('succeeded');
});

test('finished phases show how long they took', async () => {
  const timed = {
    ...STATE,
    phase_times: {
      sentences: { started_at: 1700000000, ended_at: 1700000180 },
      test_set: { started_at: 1700000180, ended_at: 1700000900 },
      theory: { started_at: 1700000900, ended_at: 1700003600 },
    },
  };
  mockFetch({ '/introspection': { ...INTROSPECTION, state: timed } });
  render(<JobIntrospectionTab jobs={[JOB]} jobId="job-1" />);
  const phases = await screen.findByRole('list', { name: 'Build phases' });
  expect(within(phases).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
    'Load sentences (3 minutes)',
    'Generate test set (12 minutes)',
    'Build theory (45 minutes)',
    'Done',
  ]);
});

test('a phase still running, or with no recorded times, shows no duration', () => {
  expect(phaseLabel('Build theory', { started_at: 10, ended_at: null })).toBe('Build theory');
  expect(phaseLabel('Build theory', undefined)).toBe('Build theory');
  expect(phaseLabel('Load sentences', { started_at: 0, ended_at: 30 })).toBe('Load sentences (<1 minute)');
});

test('the verified line grows with the build', () => {
  expect(verifiedLine({ ...STATE, n_questions: null })).toBeNull();
  expect(verifiedLine({ ...STATE, n_correct: null })).toBe('3 test questions (2 needed)');
  expect(verifiedLine(STATE)).toBe('2/3 verified (2 needed)');
});

test('older builds without raw sentences show what the agent was given', async () => {
  const old = { ...STATE, raw_sentences: undefined, standalone_sentences: undefined };
  mockFetch({ '/introspection': { ...INTROSPECTION, state: old } });
  render(<JobIntrospectionTab jobs={[JOB]} jobId="job-1" />);
  const panel = await screen.findByRole('tabpanel');
  expect(panel).toHaveTextContent('recorded before raw sentences were kept');
  expect(panel).toHaveTextContent('3. Photosynthesis releases oxygen.');
});

test('FHIR builds say entries are not rewritten', async () => {
  const fhir = { ...STATE, raw_sentences: ['{"resourceType":"Observation"}'], standalone_sentences: [null] };
  mockFetch({ '/introspection': { ...INTROSPECTION, state: fhir } });
  render(<JobIntrospectionTab jobs={[JOB]} jobId="job-1" />);
  const panel = await screen.findByRole('tabpanel');
  expect(panel).toHaveTextContent('FHIR entries are not rewritten');
  expect(panel).toHaveTextContent('—');
});
