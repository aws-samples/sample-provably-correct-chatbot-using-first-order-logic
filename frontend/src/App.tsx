// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useMemo, useState } from 'react';
import './App.css';
import { TitleBar } from './components/TitleBar';
import { ChatTab } from './components/ChatTab';
import { TheoriesTab } from './components/TheoriesTab';
import { JobsTab } from './components/JobsTab';
import { JobIntrospectionTab } from './components/JobIntrospectionTab';
import { useStore } from './useStore';
import { Job } from './types';

type FixedTabKey = 'chat' | 'theories' | 'jobs';
// A per-job introspection tab is keyed "job:<job id>".
type TabKey = FixedTabKey | `job:${string}`;

const TABS: { key: FixedTabKey; label: string }[] = [
  { key: 'chat', label: 'Chatbot' },
  { key: 'theories', label: 'Theories' },
  { key: 'jobs', label: 'Jobs' },
];

const ACTIVE_TAB_KEY = 'chatbot.activeTab';
const JOB_TABS_KEY = 'chatbot.jobTabs';

const jobTabKey = (jobId: string): TabKey => `job:${jobId}`;

// An open per-job tab. The job's name is stored with it so a reopened page
// labels the tab correctly before the jobs list has loaded.
interface JobTab {
  id: string;
  name: string;
}

function loadJobTabs(): JobTab[] {
  try {
    const v = JSON.parse(localStorage.getItem(JOB_TABS_KEY) ?? '[]');
    return Array.isArray(v)
      ? v.filter((x) => x && typeof x.id === 'string' && typeof x.name === 'string')
      : [];
  } catch {
    return [];
  }
}

function jobName(job: Job | undefined, fallback: string): string {
  return job?.theory_name ?? job?.theory_id ?? fallback;
}

function App() {
  // Per-job introspection tabs, in the order they were opened (remembered).
  const [jobTabs, setJobTabs] = useState<JobTab[]>(loadJobTabs);
  // Remember the active tab across reloads. Validate the stored value against
  // the known tabs so a stale/garbage entry falls back to 'chat'.
  const [tab, setTab] = useState<TabKey>(() => {
    const saved = localStorage.getItem(ACTIVE_TAB_KEY) ?? '';
    if (TABS.some((t) => t.key === saved)) return saved as TabKey;
    if (saved.startsWith('job:') && loadJobTabs().some((t) => t.id === saved.slice(4))) {
      return saved as TabKey;
    }
    return 'chat';
  });
  useEffect(() => {
    localStorage.setItem(ACTIVE_TAB_KEY, tab);
  }, [tab]);
  useEffect(() => {
    localStorage.setItem(JOB_TABS_KEY, JSON.stringify(jobTabs));
  }, [jobTabs]);

  const [selectedId, setSelectedId] = useState('');
  const { theories, jobs, refresh } = useStore();

  // Default the selected theory to the first ready one once theories load.
  const readyIds = useMemo(
    () => theories.filter((t) => t.status === 'ready').map((t) => t.id),
    [theories]
  );
  useEffect(() => {
    if (!selectedId && readyIds.length > 0) setSelectedId(readyIds[0]);
    // If the selected theory vanished, fall back to the first ready one.
    if (selectedId && !theories.some((t) => t.id === selectedId)) {
      setSelectedId(readyIds[0] ?? '');
    }
  }, [readyIds, selectedId, theories]);

  const activeJobCount = jobs.filter(
    (j) => j.status === 'queued' || j.status === 'running'
  ).length;

  // Open (or switch to) the introspection tab for one job.
  const openJobTab = (jobId: string) => {
    const name = jobName(jobs.find((j) => j.id === jobId), jobId.slice(0, 8));
    setJobTabs((open) =>
      open.some((t) => t.id === jobId) ? open : [...open, { id: jobId, name }]
    );
    setTab(jobTabKey(jobId));
  };

  const closeJobTab = (jobId: string) => {
    setJobTabs((open) => open.filter((t) => t.id !== jobId));
    if (tab === jobTabKey(jobId)) setTab('jobs');
  };

  const activeJobTab = tab.startsWith('job:') ? tab.slice(4) : null;

  return (
    <div className="App">
      <TitleBar title="Provably-correct agents that use first-order logic" />

      <nav className="tab-bar" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            className={tab === t.key ? 'tab active' : 'tab'}
            onClick={() => setTab(t.key)}
          >
            {t.label}
            {t.key === 'jobs' && activeJobCount > 0 && (
              <span className="tab-badge">{activeJobCount}</span>
            )}
          </button>
        ))}
        {jobTabs.map(({ id: jobId, name }) => {
          const key = jobTabKey(jobId);
          const label = `Job Introspection (${jobName(jobs.find((j) => j.id === jobId), name)})`;
          return (
            <span
              key={key}
              className={tab === key ? 'tab closable-tab active' : 'tab closable-tab'}
            >
              <button
                role="tab"
                aria-selected={tab === key}
                className="closable-tab-label"
                onClick={() => setTab(key)}
                title={label}
              >
                {label}
              </button>
              <button
                type="button"
                className="tab-close"
                aria-label={`Close ${label}`}
                title="Close tab"
                onClick={() => closeJobTab(jobId)}
              >
                ×
              </button>
            </span>
          );
        })}
      </nav>

      <main className="tab-panel" role="tabpanel">
        {tab === 'chat' && (
          <ChatTab
            theories={theories}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
        )}
        {tab === 'theories' && (
          <TheoriesTab
            theories={theories}
            jobs={jobs}
            onChanged={refresh}
            currentTheoryId={selectedId}
          />
        )}
        {tab === 'jobs' && <JobsTab jobs={jobs} onIntrospect={openJobTab} />}
        {activeJobTab && (
          <JobIntrospectionTab key={activeJobTab} jobs={jobs} jobId={activeJobTab} />
        )}
      </main>
    </div>
  );
}

export default App;
