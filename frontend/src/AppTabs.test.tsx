// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import App from './App';

const job = (id: string, name: string) => ({
  id, theory_id: `t-${id}`, theory_name: name, status: 'finished', progress: 'done',
  log_tail: '', error: null, created_at: 1700000000, started_at: 1700000000,
  finished_at: 1700003600,
});
const JOBS = [job('j1', 'Cape Canaveral'), job('j2', 'Hypertension')];

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('chatbot.activeTab', 'jobs');
  global.fetch = jest.fn((input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.endsWith('/api/jobs')
      ? { jobs: JOBS }
      : url.endsWith('/api/theories')
        ? { theories: [] }
        : url.includes('/introspection')
          ? { job: JOBS[0], available: false, state: null, note: 'Nothing recorded.' }
          : {};
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);
  }) as jest.Mock;
});

const tabNames = () => screen.getAllByRole('tab').map((t) => t.textContent);

test('there is no general Job Introspection tab', async () => {
  render(<App />);
  await screen.findByRole('button', { name: 'Cape Canaveral' });
  expect(tabNames()).toEqual(['Chatbot', 'Theories', 'Jobs']);
});

test('clicking a job name opens its own tab, and again just switches to it', async () => {
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'Cape Canaveral' }));
  expect(screen.getByRole('tab', { name: 'Job Introspection (Cape Canaveral)' }))
    .toHaveAttribute('aria-selected', 'true');
  expect(await screen.findByText('Nothing recorded.')).toBeInTheDocument();

  fireEvent.click(screen.getByRole('tab', { name: 'Jobs' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Hypertension' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Jobs' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Cape Canaveral' }));
  expect(tabNames()).toEqual([
    'Chatbot', 'Theories', 'Jobs',
    'Job Introspection (Cape Canaveral)', 'Job Introspection (Hypertension)',
  ]);
  expect(screen.getByRole('tab', { name: 'Job Introspection (Cape Canaveral)' }))
    .toHaveAttribute('aria-selected', 'true');
});

test('the close button removes a tab, and open tabs are remembered', async () => {
  const { unmount } = render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'Cape Canaveral' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Jobs' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Hypertension' }));
  unmount();

  render(<App />);
  expect(screen.getByRole('tab', { name: 'Job Introspection (Hypertension)' }))
    .toHaveAttribute('aria-selected', 'true');
  fireEvent.click(screen.getByRole('button', { name: 'Close Job Introspection (Hypertension)' }));
  expect(tabNames()).toEqual(['Chatbot', 'Theories', 'Jobs', 'Job Introspection (Cape Canaveral)']);
  // Closing the active tab falls back to the Jobs tab.
  expect(screen.getByRole('tab', { name: 'Jobs' })).toHaveAttribute('aria-selected', 'true');
  expect(within(screen.getByRole('tablist')).queryByText(/Hypertension/)).not.toBeInTheDocument();
});
