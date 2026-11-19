import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';

// Mocked fetch router keyed by URL. Covers the multi-tab backend contract:
// /api/theories, /api/jobs, /subject, /ask, /lucky.
type Handlers = {
  theories?: () => Promise<any>;
  jobs?: () => Promise<any>;
  subject?: () => Promise<any>;
  ask?: (body: string) => Promise<any>;
  lucky?: () => Promise<any>;
  luckyQuestions?: () => Promise<any>;
};

const READY_THEORY = {
  id: 't1',
  name: 'New Zealand',
  status: 'ready',
  sentence_count: 42,
  created_at: 1_700_000_000,
  job_id: null,
};

function mockFetch(handlers: Handlers) {
  return jest.fn((url: string, init?: RequestInit) => {
    const ok = (data: any) =>
      Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) } as Response);

    if (url.endsWith('/api/theories')) {
      return handlers.theories
        ? handlers.theories().then(ok)
        : ok({ theories: [READY_THEORY] });
    }
    if (url.endsWith('/api/jobs')) {
      return handlers.jobs ? handlers.jobs().then(ok) : ok({ jobs: [] });
    }
    if (url.includes('/subject')) {
      return handlers.subject
        ? handlers.subject().then(ok)
        : ok({ title: 'A chatbot expert in New Zealand.' });
    }
    if (url.includes('/ask')) {
      const body = (init?.body as string) ?? '';
      return handlers.ask ? handlers.ask(body).then(ok) : ok({ answer: 'An answer.' });
    }
    if (url.includes('/lucky-questions')) {
      return handlers.luckyQuestions
        ? handlers.luckyQuestions().then(ok)
        : ok({ questions: ['What is the capital city of New Zealand?'] });
    }
    if (url.includes('/lucky')) {
      return handlers.lucky
        ? handlers.lucky().then(ok)
        : ok({ question: 'A lucky question?' });
    }
    return Promise.reject(new Error(`unexpected url ${url}`));
  });
}

afterEach(() => {
  jest.restoreAllMocks();
});

test('renders the three tabs', async () => {
  global.fetch = mockFetch({}) as any;
  render(<App />);
  expect(screen.getByRole('tab', { name: /Chatbot/ })).toBeInTheDocument();
  expect(screen.getByRole('tab', { name: /Theories/ })).toBeInTheDocument();
  expect(screen.getByRole('tab', { name: /Jobs/ })).toBeInTheDocument();
  // The old "Current theory" tab was merged into the Theories tab's
  // Artifacts column, so it no longer exists.
  expect(
    screen.queryByRole('tab', { name: /Current theory/ })
  ).not.toBeInTheDocument();
});

test('the chat tab lists ready theories in its selector', async () => {
  global.fetch = mockFetch({}) as any;
  render(<App />);
  await waitFor(() =>
    expect(
      within(screen.getByRole('combobox')).getByText('New Zealand')
    ).toBeInTheDocument()
  );
});

test('a verified answer renders the Verified badge', async () => {
  global.fetch = mockFetch({
    ask: () => Promise.resolve({ answer: 'Wellington is the capital.' }),
  }) as any;
  render(<App />);
  // The composer enables once the theory list has loaded and one is selected.
  await waitFor(() => expect(screen.getByLabelText('Ask a question')).toBeEnabled());

  await userEvent.type(
    screen.getByLabelText('Ask a question'),
    'What is the capital?'
  );
  await userEvent.click(screen.getByRole('button', { name: 'Send' }));

  expect(await screen.findByText('Wellington is the capital.')).toBeInTheDocument();
  expect(screen.getByText(/Verified/i)).toBeInTheDocument();
});

test('an "I don\'t know" answer is marked unverifiable', async () => {
  global.fetch = mockFetch({
    ask: () => Promise.resolve({ answer: "I don't know" }),
  }) as any;
  render(<App />);
  // The composer enables once the theory list has loaded and one is selected.
  await waitFor(() => expect(screen.getByLabelText('Ask a question')).toBeEnabled());

  await userEvent.type(screen.getByLabelText('Ask a question'), 'Unknowable?');
  await userEvent.click(screen.getByRole('button', { name: 'Send' }));

  expect(await screen.findByText("I don't know")).toBeInTheDocument();
  expect(screen.getByText(/Unverifiable/i)).toBeInTheDocument();
});
test('the lucky button opens a picker and asking the chosen question works', async () => {
  global.fetch = mockFetch({
    luckyQuestions: () =>
      Promise.resolve({
        questions: [
          'What is the capital city of New Zealand?',
          'What are the two main islands?',
        ],
      }),
    ask: () => Promise.resolve({ answer: 'Wellington is the capital.' }),
  }) as any;
  render(<App />);
  // The composer enables once the theory list has loaded and one is selected.
  await waitFor(() => expect(screen.getByLabelText('Ask a question')).toBeEnabled());

  // Open the picker.
  await userEvent.click(screen.getByRole('button', { name: /Feeling/ }));

  // Both theory-specific questions are offered.
  const dialog = await screen.findByRole('dialog', { name: /Pick a question/ });
  expect(
    within(dialog).getByText('What is the capital city of New Zealand?')
  ).toBeInTheDocument();
  expect(
    within(dialog).getByText('What are the two main islands?')
  ).toBeInTheDocument();

  // Pick one -> it gets asked -> the answer renders.
  await userEvent.click(
    within(dialog).getByText('What is the capital city of New Zealand?')
  );
  expect(await screen.findByText('Wellington is the capital.')).toBeInTheDocument();
  // The picker closed after choosing.
  expect(
    screen.queryByRole('dialog', { name: /Pick a question/ })
  ).not.toBeInTheDocument();
});


test('the Theories tab shows the add-theory form on demand', async () => {
  global.fetch = mockFetch({}) as any;
  render(<App />);
  await userEvent.click(screen.getByRole('tab', { name: /Theories/ }));

  expect(await screen.findByText('Available theories')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: /Add theory/ }));
  expect(screen.getByPlaceholderText(/Paste the source document/)).toBeInTheDocument();
});

test('the Jobs tab shows an empty state with no jobs', async () => {
  global.fetch = mockFetch({}) as any;
  render(<App />);
  await userEvent.click(screen.getByRole('tab', { name: /Jobs/ }));
  expect(await screen.findByText(/No jobs yet/)).toBeInTheDocument();
});

test('with no ready theories the chat tab claims no subject expertise', async () => {
  const fetchMock = mockFetch({
    theories: () => Promise.resolve({ theories: [{ ...READY_THEORY, status: 'building' }] }),
  });
  global.fetch = fetchMock as any;
  localStorage.clear(); // start on the Chatbot tab, not a tab an earlier test left
  render(<App />);
  expect(await screen.findByText('(no ready theories)')).toBeInTheDocument();
  expect(screen.queryByText(/expert in/)).not.toBeInTheDocument();
  expect(document.querySelector('.theory-subject')).toBeNull();
  // Nothing asked the server to describe a subject it happens to have loaded.
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/subject'))).toBe(false);
  // There is nothing to ask yet, and the composer says where to go.
  expect(screen.getByRole('textbox', { name: 'Ask a question' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
  expect(screen.getByText(/Build one in the Theories tab/)).toBeInTheDocument();
});
