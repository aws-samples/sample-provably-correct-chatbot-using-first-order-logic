// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { KiroCostTotals } from './types';

/** "$0.49", or "<$0.01" for a non-zero amount that rounds to nothing. */
export function formatDollars(d: number): string {
  if (d > 0 && d < 0.005) return '<$0.01';
  return `$${d.toFixed(2)}`;
}

/** "12.3 credits", with two decimals below 10. */
export function formatCredits(credits: number): string {
  return `${credits.toFixed(credits < 10 ? 2 : 1)} credits`;
}

/**
 * e.g. "12.3 credits · $0.49 · 3 sessions", plus a lower-bound warning when
 * some sessions reported no usage (killed or crashed).
 */
export function formatCost(t: KiroCostTotals): string {
  const parts = [
    formatCredits(t.credits),
    formatDollars(t.dollars),
    `${t.sessions} session${t.sessions === 1 ? '' : 's'}`,
  ];
  if (t.unmetered_sessions > 0) {
    parts.push(`${t.unmetered_sessions} unmetered (lower bound)`);
  }
  return parts.join(' · ');
}

/** One session's spend: "0.69 credits · $0.03", or "not metered". */
export function formatSessionCost(credits: number | null, dollarsPerCredit: number | null): string {
  if (credits == null) return 'not metered';
  if (dollarsPerCredit == null) return formatCredits(credits);
  return `${formatCredits(credits)} · ${formatDollars(credits * dollarsPerCredit)}`;
}

/** "<1 minute", "12 minutes", "1.4 hours". */
export function formatDuration(secs: number): string {
  const mins = Math.max(0, secs) / 60;
  if (mins < 1) return '<1 minute';
  if (mins < 60) {
    const m = Math.round(mins);
    return `${m} minute${m === 1 ? '' : 's'}`;
  }
  return `${(mins / 60).toFixed(1)} hours`;
}
