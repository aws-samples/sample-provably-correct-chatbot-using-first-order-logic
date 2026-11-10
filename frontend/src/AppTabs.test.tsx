// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import type { Mock } from 'vitest';
import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import App from './App';

const job = (id: string, name: string) => ({
  id, theory_id: `t-${id}`, theory_name: name, status: 'finished', progress: 'done',
  log_tail: '', error: null, created_at: 1700000000, started_at: 1700000000,
  finished_at: 1700003600,
});
const JOBS = [job('j1', 'Cape Canaveral'), job('j2', 'Hypertension')];
const THEORIES = [{
  id: 't-j1', name: 'Cape Canaveral', status: 'ready', sentence_count: 3,
  created_at: 1700000000, job_id: 'j1', doc_type: 'text',
}];

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('chatbot.activeTab', 'jobs');
  global.fetch = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.endsWith('/api/jobs')
      ? { jobs: JOBS }
      : url.endsWith('/api/theories')
        ? { theories: THEORIES }
        : url.includes('/introspection')
          ? { job: JOBS[0], available: false, state: null, note: 'Nothing recorded.' }
          : {};
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);
  }) as Mock;
});

const tabNames = () => screen.getAllByRole('tab').map((t) => t.textContent);

test('there is no general Job Details tab', async () => {
  render(<App />);
  await screen.findByRole('button', { name: 'Cape Canaveral' });
  expect(tabNames()).toEqual(['Super Chat', 'Theories', 'Jobs']);
});

test('clicking a job name opens its own tab, and again just switches to it', async () => {
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'Cape Canaveral' }));
  expect(screen.getByRole('tab', { name: 'Job Details (Cape Canaveral)' }))
    .toHaveAttribute('aria-selected', 'true');
  expect(await screen.findByText('Nothing recorded.')).toBeInTheDocument();

  fireEvent.click(screen.getByRole('tab', { name: 'Jobs' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Hypertension' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Jobs' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Cape Canaveral' }));
  expect(tabNames()).toEqual([
    'Super Chat', 'Theories', 'Jobs',
    'Job Details (Cape Canaveral)', 'Job Details (Hypertension)',
  ]);
  expect(screen.getByRole('tab', { name: 'Job Details (Cape Canaveral)' }))
    .toHaveAttribute('aria-selected', 'true');
});

test('the close button removes a tab, and open tabs are remembered', async () => {
  const { unmount } = render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'Cape Canaveral' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Jobs' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Hypertension' }));
  unmount();

  render(<App />);
  expect(screen.getByRole('tab', { name: 'Job Details (Hypertension)' }))
    .toHaveAttribute('aria-selected', 'true');
  fireEvent.click(screen.getByRole('button', { name: 'Close Job Details (Hypertension)' }));
  expect(tabNames()).toEqual(['Super Chat', 'Theories', 'Jobs', 'Job Details (Cape Canaveral)']);
  // Closing the active tab falls back to the Jobs tab.
  expect(screen.getByRole('tab', { name: 'Jobs' })).toHaveAttribute('aria-selected', 'true');
  expect(within(screen.getByRole('tablist')).queryByText(/Hypertension/)).not.toBeInTheDocument();
});

test('a theory row opens its Job Details tab, and chat tabs sit before job tabs', async () => {
  localStorage.setItem('chatbot.activeTab', 'theories');
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'Job details' }));
  expect(screen.getByRole('tab', { name: 'Job Details (Cape Canaveral)' }))
    .toHaveAttribute('aria-selected', 'true');
  fireEvent.click(screen.getByRole('tab', { name: 'Theories' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Chat' }));
  expect(tabNames()).toEqual([
    'Super Chat', 'Theories', 'Jobs', 'Chat (Cape Canaveral)', 'Job Details (Cape Canaveral)',
  ]);
  // Closing the active chat tab falls back to the Theories tab.
  fireEvent.click(screen.getByRole('button', { name: 'Close Chat (Cape Canaveral)' }));
  expect(screen.getByRole('tab', { name: 'Theories' })).toHaveAttribute('aria-selected', 'true');
});

test('Super Chat is always the first tab, with no close button', async () => {
  localStorage.clear();
  render(<App />);
  expect(tabNames()[0]).toBe('Super Chat');
  expect(screen.queryByRole('button', { name: 'Close Super Chat' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: 'Super Chat' }));
  expect(screen.getByRole('tab', { name: 'Super Chat' })).toHaveAttribute('aria-selected', 'true');
  expect(await screen.findByRole('button', { name: /^Theories:/ })).toBeInTheDocument();
});
