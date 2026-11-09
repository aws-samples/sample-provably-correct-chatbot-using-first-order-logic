// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { ArtifactModal } from './ArtifactModal';

const THEORY = {
  id: 't1', name: 'Cape Canaveral', status: 'ready' as const, sentence_count: 69,
  created_at: 1, job_id: 'j1', doc_type: 'text' as const,
};

function mockTheory(detail: Record<string, unknown>) {
  global.fetch = jest.fn(() =>
    Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve({ ...THEORY, source_document: '', computed_theory: '', ...detail }),
    } as Response)
  ) as jest.Mock;
}

afterEach(() => jest.restoreAllMocks());

test('the Q&A artifact shows each question with its answer', async () => {
  mockTheory({
    suggested_questions: ['When was the cape renamed?', 'Who named it?'],
    suggested_qas: [
      { question: 'When was the cape renamed?', answer: 'In 1963.', source: 'test set' },
      { question: 'Who named it?', answer: null, source: 'variation' },
    ],
  });
  render(<ArtifactModal theory={THEORY} kind="qas" onClose={jest.fn()} />);
  const table = await screen.findByRole('table', { name: 'Verified questions and their answers' });
  const rows = within(table).getAllByRole('row').slice(1);
  expect(rows[0]).toHaveTextContent('When was the cape renamed?In 1963.');
  expect(rows[1]).toHaveTextContent('Who named it? (variation)not recorded');
  expect(screen.getByText(/1 from the test set/)).toBeInTheDocument();
  expect(screen.getByRole('dialog')).toHaveAccessibleName(
    'Test set for Cape Canaveral'
  );
});

test('without answers from the server it falls back to the question list', async () => {
  mockTheory({ suggested_questions: ['Only a question?'] });
  render(<ArtifactModal theory={THEORY} kind="qas" onClose={jest.fn()} />);
  expect(await screen.findByText('Only a question?')).toBeInTheDocument();
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});
