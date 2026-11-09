// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { TheoriesTab } from './TheoriesTab';
import { TheorySummary, TheoryStatus } from '../types';

const theory = (id: string, name: string, status: TheoryStatus): TheorySummary => ({
  id, name, status, sentence_count: 3, created_at: 1700000000, job_id: null,
  doc_type: 'text',
});

const THEORIES = [
  theory('a', 'Photosynthesis', 'ready'),
  theory('b', 'Cape Canaveral', 'building'),
  theory('c', 'Hypertension', 'failed'),
  theory('d', 'Tides', 'cancelled'),
];

const LINKS = { onOpenChat: jest.fn(), onOpenJob: jest.fn() };

beforeEach(() => {
  LINKS.onOpenChat.mockClear();
  LINKS.onOpenJob.mockClear();
  localStorage.clear();
  global.fetch = jest.fn(() =>
    Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) } as Response)
  ) as jest.Mock;
});

const rowNames = () =>
  screen.getAllByRole('row').slice(1).map((r) => r.querySelector('.theory-name-text')?.textContent);
const rowOf = (name: string) =>
  screen.getAllByRole('row').find((r) => r.querySelector('.theory-name-text')?.textContent === name)!;

test('only ready and building theories are listed by default', () => {
  render(<TheoriesTab {...LINKS} theories={THEORIES} jobs={[]} onChanged={jest.fn()} />);
  expect(rowNames()).toEqual(['Photosynthesis', 'Cape Canaveral']);
  expect(screen.getByLabelText('Show failed and cancelled (2)')).not.toBeChecked();
});

test('the toggle reveals failed and cancelled theories with Rebuild, and is remembered', () => {
  const { unmount } = render(<TheoriesTab {...LINKS} theories={THEORIES} jobs={[]} onChanged={jest.fn()} />);
  fireEvent.click(screen.getByLabelText('Show failed and cancelled (2)'));
  expect(rowNames()).toEqual(['Photosynthesis', 'Cape Canaveral', 'Hypertension', 'Tides']);
  const failedRow = screen.getAllByRole('row').find((r) => r.textContent?.includes('Hypertension'))!;
  expect(within(failedRow).getByRole('button', { name: 'Rebuild' })).toBeEnabled();
  unmount();
  render(<TheoriesTab {...LINKS} theories={THEORIES} jobs={[]} onChanged={jest.fn()} />);
  expect(screen.getByLabelText('Show failed and cancelled (2)')).toBeChecked();
});

test('explains when every theory is hidden', () => {
  render(
    <TheoriesTab {...LINKS} theories={[theory('c', 'Hypertension', 'failed')]} jobs={[]} onChanged={jest.fn()} />
  );
  expect(screen.getByText(/No ready or building theories\. 1 failed or cancelled theory is hidden/))
    .toBeInTheDocument();
});

test('a building theory shows its current step, then post-processing', () => {
  const job = (progress: string) => ({
    id: 'j', theory_id: 'b', theory_name: 'Cape Canaveral', status: 'running' as const,
    progress, log_tail: '', error: null, created_at: 1, started_at: 1, finished_at: null,
  });
  const building = { ...theory('b', 'Cape Canaveral', 'building'), job_id: 'j' };
  const { rerender } = render(
    <TheoriesTab {...LINKS} theories={[building]} jobs={[job('generating test set')]} onChanged={jest.fn()} />
  );
  expect(screen.getByText('building — test set')).toBeInTheDocument();
  rerender(
    <TheoriesTab {...LINKS} theories={[building]} jobs={[job('kiro-cli session 2 (fixing after verification)')]}
      onChanged={jest.fn()} />
  );
  expect(screen.getByText('building — session 2')).toBeInTheDocument();
  rerender(<TheoriesTab {...LINKS} theories={[building]} jobs={[job('generating suggested questions')]} onChanged={jest.fn()} />);
  expect(screen.getByText('building — suggested questions')).toBeInTheDocument();
  rerender(<TheoriesTab {...LINKS} theories={[building]} jobs={[job('post-processing')]} onChanged={jest.fn()} />);
  expect(screen.getByText('building — post-processing')).toBeInTheDocument();
});

test('there is no "Available theories" header', () => {
  render(<TheoriesTab {...LINKS} theories={THEORIES} jobs={[]} onChanged={jest.fn()} />);
  expect(screen.queryByText('Available theories')).not.toBeInTheDocument();
});

test('each row links to its Job Details tab, and ready theories to a chat tab', () => {
  const withJobs = THEORIES.map((t) => ({ ...t, job_id: `job-${t.id}` }));
  render(<TheoriesTab {...LINKS} theories={withJobs} jobs={[]} onChanged={jest.fn()} />);
  fireEvent.click(within(rowOf('Cape Canaveral')).getByRole('button', { name: 'Job details' }));
  expect(LINKS.onOpenJob).toHaveBeenCalledWith('job-b');
  expect(within(rowOf('Cape Canaveral')).queryByRole('button', { name: 'Chat' })).toBeNull();
  fireEvent.click(within(rowOf('Photosynthesis')).getByRole('button', { name: 'Chat' }));
  expect(LINKS.onOpenChat).toHaveBeenCalledWith('a');
});

const patchCalls = () =>
  (global.fetch as jest.Mock).mock.calls.filter(([, init]) => init?.method === 'PATCH');

test('the pencil renames a theory on Enter', async () => {
  const onChanged = jest.fn();
  render(<TheoriesTab {...LINKS} theories={THEORIES} jobs={[]} onChanged={onChanged} />);
  fireEvent.click(screen.getByRole('button', { name: 'Rename Photosynthesis' }));
  const input = screen.getByLabelText('New name for Photosynthesis');
  fireEvent.change(input, { target: { value: '  Light reactions ' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  const [url, init] = patchCalls()[0];
  expect(String(url)).toMatch(/\/api\/theories\/a$/);
  expect(JSON.parse(String(init.body))).toEqual({ name: 'Light reactions' });
  expect(screen.queryByLabelText('New name for Photosynthesis')).not.toBeInTheDocument();
});

test('a rename to a taken name is refused, and Escape cancels', () => {
  render(<TheoriesTab {...LINKS} theories={THEORIES} jobs={[]} onChanged={jest.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Rename Photosynthesis' }));
  const input = screen.getByLabelText('New name for Photosynthesis');
  fireEvent.change(input, { target: { value: 'cape canaveral' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(screen.getByRole('alert')).toHaveTextContent('A theory named "Cape Canaveral" already exists');
  expect(patchCalls()).toHaveLength(0);
  fireEvent.keyDown(input, { key: 'Escape' });
  expect(screen.queryByLabelText('New name for Photosynthesis')).not.toBeInTheDocument();
  expect(rowNames()).toContain('Photosynthesis');
});

test('a new theory whose name is taken must be renamed before building', () => {
  render(<TheoriesTab {...LINKS} theories={THEORIES} jobs={[]} onChanged={jest.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: '+ Add theory' }));
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'TIDES' } });
  fireEvent.change(screen.getByPlaceholderText('Paste the source document here…'), {
    target: { value: 'The moon causes tides.' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Build theory…' }));
  expect(screen.getByText('A theory named "Tides" already exists. Choose a different name.'))
    .toBeInTheDocument();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByLabelText('Name')).toHaveFocus();
});
