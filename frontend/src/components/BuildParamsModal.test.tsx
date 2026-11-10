// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import type { Mock } from 'vitest';
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { BuildParamsModal, validateDraft } from './BuildParamsModal';
import { TheoriesTab } from './TheoriesTab';
import { BuildParamSpec } from '../types';

const LINKS = { onOpenChat: vi.fn(), onOpenJob: vi.fn() };

const SPECS: BuildParamSpec[] = [
  { key: 'qa_window_sentences', group: 'Test set', type: 'int', label: 'Question window (sentences)',
    min: 2, max: 200, step: 1, default: 10, help: 'Window size.' },
  { key: 'ncar_ratio', group: 'Test set', type: 'float', label: 'Required correct ratio',
    min: 0.1, max: 1, step: 0.05, default: 0.8, help: 'Fraction required.' },
  { key: 'kiro_model', group: 'kiro-cli', type: 'text', label: 'kiro-cli model',
    default: '', help: 'Model.' },
  { key: 'kiro_max_attempts', group: 'kiro-cli', type: 'int', label: 'Sessions',
    min: 1, max: 10, step: 1, default: 3, help: 'Sessions.' },
];

type Call = { url: string; init?: RequestInit };

function mockFetch(calls: Call[], create: { status: number; body: unknown } = { status: 200, body: {} }) {
  global.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    const reply = (status: number, body: unknown) =>
      Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) } as Response);
    if (url.endsWith('/api/build-params')) return reply(200, { params: SPECS });
    if (url.endsWith('/api/sample-patients')) return reply(200, { sample_patients: [] });
    if (url.endsWith('/api/theories') || url.includes('/rebuild')) return reply(create.status, create.body);
    return reply(200, {});
  }) as Mock;
}

afterEach(() => vi.restoreAllMocks());

test('the form is pre-filled with the defaults', async () => {
  mockFetch([]);
  render(<BuildParamsModal title="Build" onConfirm={vi.fn()} onClose={vi.fn()} />);
  expect(await screen.findByLabelText('Question window (sentences)')).toHaveValue(10);
  expect(screen.getByLabelText('Required correct ratio')).toHaveValue(0.8);
  expect(screen.getByLabelText('kiro-cli model')).toHaveValue('');
  expect(screen.getByLabelText('Sessions')).toHaveValue(3);
  expect(screen.getByRole('group', { name: 'Test set' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Reset to defaults' })).toBeDisabled();
});

test('confirm sends the edited values; reset restores defaults', async () => {
  mockFetch([]);
  const onConfirm = vi.fn(() => Promise.resolve());
  render(<BuildParamsModal title="Build" onConfirm={onConfirm} onClose={vi.fn()} />);
  const window = await screen.findByLabelText('Question window (sentences)');
  fireEvent.change(window, { target: { value: '6' } });
  fireEvent.click(screen.getByRole('button', { name: 'Reset to defaults' }));
  expect(window).toHaveValue(10);
  fireEvent.change(window, { target: { value: '6' } });
  fireEvent.change(screen.getByLabelText('Sessions'), { target: { value: '4' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start build' }));
  await waitFor(() => expect(onConfirm).toHaveBeenCalled());
  expect(onConfirm).toHaveBeenCalledWith({
    qa_window_sentences: 6, ncar_ratio: 0.8, kiro_model: '', kiro_max_attempts: 4,
  });
});

test('invalid values block submit and are explained', async () => {
  mockFetch([]);
  const onConfirm = vi.fn(() => Promise.resolve());
  render(<BuildParamsModal title="Build" onConfirm={onConfirm} onClose={vi.fn()} />);
  fireEvent.change(await screen.findByLabelText('Sessions'), { target: { value: '0' } });
  expect(await screen.findByRole('alert')).toHaveTextContent('Must be between 1 and 10.');
  fireEvent.click(screen.getByRole('button', { name: 'Start build' }));
  expect(onConfirm).not.toHaveBeenCalled();
});

test('a server refusal is shown in the dialog', async () => {
  mockFetch([]);
  const onConfirm = vi.fn(() => Promise.reject(new Error('Sessions must be between 1 and 10')));
  render(<BuildParamsModal title="Build" onConfirm={onConfirm} onClose={vi.fn()} />);
  await screen.findByLabelText('Sessions');
  fireEvent.click(screen.getByRole('button', { name: 'Start build' }));
  expect(await screen.findByText('Sessions must be between 1 and 10')).toBeInTheDocument();
});

test('validateDraft checks bounds and integers', () => {
  const ok = { qa_window_sentences: '10', ncar_ratio: '0.8', kiro_model: '', kiro_max_attempts: '3' };
  expect(validateDraft(SPECS, ok)).toEqual({});
  expect(validateDraft(SPECS, { ...ok, kiro_max_attempts: '2.5' }).kiro_max_attempts).toMatch(/whole number/);
  expect(validateDraft(SPECS, { ...ok, ncar_ratio: '2' }).ncar_ratio).toMatch(/between/);
  expect(validateDraft(SPECS, { ...ok, qa_window_sentences: '' }).qa_window_sentences).toMatch(/number/);
  expect(validateDraft(SPECS, { ...ok, qa_window_sentences: '1' }).qa_window_sentences).toBe('Must be between 2 and 200.');
});

test('Build theory opens the dialog and creates with the chosen parameters', async () => {
  const calls: Call[] = [];
  mockFetch(calls, { status: 200, body: { theory: {}, job: {} } });
  const onChanged = vi.fn();
  render(<TheoriesTab {...LINKS} theories={[]} jobs={[]} onChanged={onChanged} />);
  fireEvent.click(screen.getByRole('button', { name: '+ Add theory' }));
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Tides' } });
  fireEvent.change(screen.getByPlaceholderText('Paste the source document here…'), {
    target: { value: 'The moon causes tides.' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Build theory…' }));
  expect(await screen.findByRole('dialog', { name: 'Build parameters: Tides' })).toBeInTheDocument();
  expect(calls.some((c) => c.url.endsWith('/api/theories'))).toBe(false); // not started yet
  fireEvent.change(await screen.findByLabelText('Question window (sentences)'), { target: { value: '5' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start build' }));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  const create = calls.find((c) => c.url.endsWith('/api/theories'))!;
  const body = JSON.parse(String(create.init!.body));
  expect(body.name).toBe('Tides');
  expect(body.build_params.qa_window_sentences).toBe(5);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

test('a name the server says is taken closes the dialog and asks for another', async () => {
  const detail = 'A theory named "Tides" already exists. Choose a different name.';
  mockFetch([], { status: 409, body: { detail } });
  render(<TheoriesTab {...LINKS} theories={[]} jobs={[]} onChanged={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: '+ Add theory' }));
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Tides' } });
  fireEvent.change(screen.getByPlaceholderText('Paste the source document here…'), {
    target: { value: 'The moon causes tides.' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Build theory…' }));
  await screen.findByLabelText('Question window (sentences)');
  fireEvent.click(screen.getByRole('button', { name: 'Start build' }));
  expect(await screen.findByText(detail)).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByLabelText('Name')).toHaveFocus();
});

const THEORY = {
  id: 't1', name: 'Tides', status: 'ready', sentence_count: 3, created_at: 1_700_000_000,
  job_id: 'j2',
};
const job = (id: string, created_at: number, build_params: Record<string, number | string> | null) => ({
  id, theory_id: 't1', theory_name: 'Tides', status: 'finished', progress: 'done', log_tail: '',
  error: null, created_at, started_at: created_at, finished_at: created_at + 60, build_params,
});

test('a rebuild starts from the most recent job\u2019s parameters', async () => {
  const calls: Call[] = [];
  mockFetch(calls, { status: 200, body: { theory: {}, job: {} } });
  const jobs = [
    job('j1', 1_700_000_000, { qa_window_sentences: 3, ncar_ratio: 0.6 }),
    // Newest; it set no kiro_max_attempts, so that one stays default. Keys the
    // dialog no longer offers are dropped rather than sent back.
    job('j2', 1_700_100_000, { qa_window_sentences: 9, ncar_ratio: 0.7,
                               kiro_model: 'm1', sentences_per_chunk: 4 }),
    job('j3', 1_700_200_000, null), // no recorded params: ignored
  ];
  render(<TheoriesTab {...LINKS} theories={[THEORY as any]} jobs={jobs as any} onChanged={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Rebuild' }));
  expect(await screen.findByLabelText('Question window (sentences)')).toHaveValue(9);
  expect(screen.getByLabelText('Required correct ratio')).toHaveValue(0.7);
  expect(screen.getByLabelText('kiro-cli model')).toHaveValue('m1');
  expect(screen.getByLabelText('Sessions')).toHaveValue(3);
  expect(screen.getByText(/Starting from the parameters of the last build/)).toBeInTheDocument();
  // Reset returns to the last build's values, not the server defaults.
  const reset = screen.getByRole('button', { name: 'Reset to last build' });
  expect(reset).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Question window (sentences)'), { target: { value: '2' } });
  fireEvent.click(reset);
  expect(screen.getByLabelText('Question window (sentences)')).toHaveValue(9);
  fireEvent.click(screen.getByRole('button', { name: 'Start rebuild' }));
  await waitFor(() => expect(calls.some((c) => c.url.includes('/rebuild'))).toBe(true));
  const body = JSON.parse(String(calls.find((c) => c.url.includes('/rebuild'))!.init!.body));
  expect(body.build_params).toEqual({ qa_window_sentences: 9, ncar_ratio: 0.7, kiro_model: 'm1',
                                     kiro_max_attempts: 3 });
});

test('a rebuild with no recorded parameters starts from the defaults', async () => {
  mockFetch([]);
  render(<TheoriesTab {...LINKS} theories={[THEORY as any]} jobs={[job('j1', 1, null)] as any} onChanged={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Rebuild' }));
  expect(await screen.findByLabelText('Question window (sentences)')).toHaveValue(10);
  expect(screen.getByRole('button', { name: 'Reset to defaults' })).toBeInTheDocument();
  expect(screen.queryByText(/last build/)).not.toBeInTheDocument();
});
