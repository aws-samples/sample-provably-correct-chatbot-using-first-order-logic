// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
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

beforeEach(() => {
  localStorage.clear();
  global.fetch = jest.fn(() =>
    Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) } as Response)
  ) as jest.Mock;
});

const rowNames = () =>
  screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell')[0].textContent);

test('only ready and building theories are listed by default', () => {
  render(<TheoriesTab theories={THEORIES} jobs={[]} onChanged={jest.fn()} />);
  expect(rowNames()).toEqual(['Photosynthesis', 'Cape Canaveral']);
  expect(screen.getByLabelText('Show failed and cancelled (2)')).not.toBeChecked();
});

test('the toggle reveals failed and cancelled theories with Rebuild, and is remembered', () => {
  const { unmount } = render(<TheoriesTab theories={THEORIES} jobs={[]} onChanged={jest.fn()} />);
  fireEvent.click(screen.getByLabelText('Show failed and cancelled (2)'));
  expect(rowNames()).toEqual(['Photosynthesis', 'Cape Canaveral', 'Hypertension', 'Tides']);
  const failedRow = screen.getAllByRole('row').find((r) => r.textContent?.includes('Hypertension'))!;
  expect(within(failedRow).getByRole('button', { name: 'Rebuild' })).toBeEnabled();
  unmount();
  render(<TheoriesTab theories={THEORIES} jobs={[]} onChanged={jest.fn()} />);
  expect(screen.getByLabelText('Show failed and cancelled (2)')).toBeChecked();
});

test('explains when every theory is hidden', () => {
  render(
    <TheoriesTab theories={[theory('c', 'Hypertension', 'failed')]} jobs={[]} onChanged={jest.fn()} />
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
    <TheoriesTab theories={[building]} jobs={[job('generating test set')]} onChanged={jest.fn()} />
  );
  expect(screen.getByText('building — test set')).toBeInTheDocument();
  rerender(
    <TheoriesTab theories={[building]} jobs={[job('kiro-cli session 2 (fixing after verification)')]}
      onChanged={jest.fn()} />
  );
  expect(screen.getByText('building — session 2')).toBeInTheDocument();
  rerender(<TheoriesTab theories={[building]} jobs={[job('post-processing')]} onChanged={jest.fn()} />);
  expect(screen.getByText('building — post-processing')).toBeInTheDocument();
});
