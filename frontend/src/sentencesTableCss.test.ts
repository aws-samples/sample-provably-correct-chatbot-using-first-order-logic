// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// The sentences table must fit its panel: its text columns may not be kept on
// one line. Vitest stubs CSS imports, so apply the real stylesheet to a table
// in the DOM and read the cascaded values. (A later, equally specific
// .unit-qas-table last-column rule with white-space: nowrap once won the
// cascade and made the table scroll sideways.)
import fs from 'fs';
import path from 'path';

function cascaded(cell: HTMLElement) {
  const st = window.getComputedStyle(cell);
  return { whiteSpace: st.whiteSpace, width: st.width };
}

beforeAll(() => {
  const css = fs.readFileSync(path.join(__dirname, 'App.css'), 'utf8');
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);
});

function table(extraClass: string): HTMLTableElement {
  document.body.innerHTML = `
    <table class="unit-qas-table ${extraClass}">
      <thead><tr><th>#</th><th>Raw sentence</th><th>De-contextualized sentence</th></tr></thead>
      <tbody><tr><td>1</td><td>raw</td><td>rewrite</td></tr></tbody>
    </table>`;
  return document.querySelector('table')!;
}

test('both sentence columns wrap and take no fixed width', () => {
  const t = table('sentences-table');
  expect(window.getComputedStyle(t).tableLayout).toBe('fixed');
  for (const cell of Array.from(t.querySelectorAll<HTMLElement>('th:nth-child(n+2), td:nth-child(n+2)'))) {
    const { whiteSpace, width } = cascaded(cell);
    expect(whiteSpace).toBe('normal');
    expect(width).not.toBe('7em');
  }
});

test('the test-set table keeps its narrow, unwrapped Credited column', () => {
  const t = table('');
  const last = t.querySelector<HTMLElement>('td:last-child')!;
  expect(cascaded(last)).toEqual({ whiteSpace: 'nowrap', width: '7em' });
});
