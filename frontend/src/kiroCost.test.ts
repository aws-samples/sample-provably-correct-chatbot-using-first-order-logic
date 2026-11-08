// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { formatCost, formatDollars, formatDuration, formatSessionCost } from './kiroCost';

test('formatDollars rounds to cents and flags tiny amounts', () => {
  expect(formatDollars(0)).toBe('$0.00');
  expect(formatDollars(0.001)).toBe('<$0.01');
  expect(formatDollars(12.345)).toBe('$12.35');
});

test('formatDuration uses minutes, then hours', () => {
  expect(formatDuration(20)).toBe('<1 minute');
  expect(formatDuration(60)).toBe('1 minute');
  expect(formatDuration(59 * 60)).toBe('59 minutes');
  expect(formatDuration(150 * 60)).toBe('2.5 hours');
});

test('formatCost warns when some sessions were unmetered', () => {
  expect(formatCost({ credits: 1.234, dollars: 0.049, sessions: 1, unmetered_sessions: 0 })).toBe(
    '1.23 credits · $0.05 · 1 session'
  );
  expect(formatCost({ credits: 42, dollars: 1.68, sessions: 5, unmetered_sessions: 2 })).toBe(
    '42.0 credits · $1.68 · 5 sessions · 2 unmetered (lower bound)'
  );
});

test('formatSessionCost says when a session was not metered', () => {
  expect(formatSessionCost(null, 0.04)).toBe('not metered');
  expect(formatSessionCost(0.69, 0.04)).toBe('0.69 credits · $0.03');
  expect(formatSessionCost(12.5, null)).toBe('12.5 credits');
});
