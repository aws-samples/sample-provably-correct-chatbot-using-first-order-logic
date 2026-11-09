// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useState } from 'react';
import './App.css';
import { TitleBar } from './components/TitleBar';
import { ChatTab } from './components/ChatTab';
import { TheoriesTab } from './components/TheoriesTab';
import { JobsTab } from './components/JobsTab';
import { JobIntrospectionTab } from './components/JobIntrospectionTab';
import { useStore } from './useStore';
import { Job } from './types';

type FixedTabKey = 'theories' | 'jobs';
// A per-theory chat tab is keyed "chat:<theory id>"; a per-job Job Details tab
// "job:<job id>".
type TabKey = FixedTabKey | `chat:${string}` | `job:${string}`;

const TABS: { key: FixedTabKey; label: string }[] = [
  { key: 'theories', label: 'Theories' },
  { key: 'jobs', label: 'Jobs' },
];

const ACTIVE_TAB_KEY = 'chatbot.activeTab';
const CHAT_TABS_KEY = 'chatbot.chatTabs';
const JOB_TABS_KEY = 'chatbot.jobTabs';

const chatTabKey = (theoryId: string): TabKey => `chat:${theoryId}`;
const jobTabKey = (jobId: string): TabKey => `job:${jobId}`;

// An open chat or Job Details tab: the theory or job id, and the name shown
// in its label. The name is stored with it so a reopened page labels the tab
// correctly before the theories and jobs lists have loaded.
interface OpenTab {
  id: string;
  name: string;
}

function loadTabs(storageKey: string): OpenTab[] {
  try {
    const v = JSON.parse(localStorage.getItem(storageKey) ?? '[]');
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
  // Chat and Job Details tabs, each in the order they were opened. A fresh
  // install has none; both lists are remembered across reloads.
  const [chatTabs, setChatTabs] = useState<OpenTab[]>(() => loadTabs(CHAT_TABS_KEY));
  const [jobTabs, setJobTabs] = useState<OpenTab[]>(() => loadTabs(JOB_TABS_KEY));
  // Remember the active tab across reloads. A stale entry (unknown key, or a
  // tab that has since been closed) falls back to the Theories tab.
  const [tab, setTab] = useState<TabKey>(() => {
    const saved = localStorage.getItem(ACTIVE_TAB_KEY) ?? '';
    if (TABS.some((t) => t.key === saved)) return saved as TabKey;
    if (saved.startsWith('chat:') && loadTabs(CHAT_TABS_KEY).some((t) => t.id === saved.slice(5))) {
      return saved as TabKey;
    }
    if (saved.startsWith('job:') && loadTabs(JOB_TABS_KEY).some((t) => t.id === saved.slice(4))) {
      return saved as TabKey;
    }
    return 'theories';
  });
  useEffect(() => {
    localStorage.setItem(ACTIVE_TAB_KEY, tab);
  }, [tab]);
  useEffect(() => {
    localStorage.setItem(CHAT_TABS_KEY, JSON.stringify(chatTabs));
  }, [chatTabs]);
  useEffect(() => {
    localStorage.setItem(JOB_TABS_KEY, JSON.stringify(jobTabs));
  }, [jobTabs]);

  const { theories, jobs, loading, error, refresh } = useStore();

  // Once the theories have loaded, keep the chat tabs in step with them: a
  // renamed theory's stored name follows the new name, and a deleted theory's
  // tab closes.
  useEffect(() => {
    if (loading || error) return;
    const next = chatTabs
      .filter((c) => theories.some((t) => t.id === c.id))
      .map((c) => ({ ...c, name: theories.find((t) => t.id === c.id)!.name }));
    const same =
      next.length === chatTabs.length && next.every((c, i) => c.name === chatTabs[i].name);
    if (same) return;
    setChatTabs(next);
    if (tab.startsWith('chat:') && !next.some((c) => chatTabKey(c.id) === tab)) {
      setTab('theories');
    }
  }, [theories, loading, error, chatTabs, tab]);

  const activeJobCount = jobs.filter(
    (j) => j.status === 'queued' || j.status === 'running'
  ).length;

  // Open (or switch to) the chat tab for one theory.
  const openChatTab = (theoryId: string) => {
    const name = theories.find((t) => t.id === theoryId)?.name ?? theoryId.slice(0, 8);
    setChatTabs((open) =>
      open.some((t) => t.id === theoryId) ? open : [...open, { id: theoryId, name }]
    );
    setTab(chatTabKey(theoryId));
  };

  const closeChatTab = (theoryId: string) => {
    setChatTabs((open) => open.filter((t) => t.id !== theoryId));
    if (tab === chatTabKey(theoryId)) setTab('theories');
  };

  // Open (or switch to) the Job Details tab for one job.
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
        {chatTabs.map(({ id, name }) => (
          <ClosableTab
            key={chatTabKey(id)}
            label={`Chat (${theories.find((t) => t.id === id)?.name ?? name})`}
            active={tab === chatTabKey(id)}
            onSelect={() => setTab(chatTabKey(id))}
            onClose={() => closeChatTab(id)}
          />
        ))}
        {jobTabs.map(({ id, name }) => (
          <ClosableTab
            key={jobTabKey(id)}
            label={`Job Details (${jobName(jobs.find((j) => j.id === id), name)})`}
            active={tab === jobTabKey(id)}
            onSelect={() => setTab(jobTabKey(id))}
            onClose={() => closeJobTab(id)}
          />
        ))}
      </nav>

      <main className="tab-panel" role="tabpanel">
        {tab === 'theories' && (
          <TheoriesTab
            theories={theories}
            jobs={jobs}
            onChanged={refresh}
            onOpenChat={openChatTab}
            onOpenJob={openJobTab}
          />
        )}
        {tab === 'jobs' && <JobsTab jobs={jobs} onIntrospect={openJobTab} />}
        {/* Chat tabs stay mounted while hidden so each keeps its conversation
            when the user switches tabs. */}
        {chatTabs.map(({ id }) => (
          <div key={id} className="chat-tab-panel" hidden={tab !== chatTabKey(id)}>
            <ChatTab theoryId={id} theory={theories.find((t) => t.id === id)} />
          </div>
        ))}
        {activeJobTab && (
          <JobIntrospectionTab key={activeJobTab} jobs={jobs} jobId={activeJobTab} />
        )}
      </main>
    </div>
  );
}

interface ClosableTabProps {
  label: string;
  active: boolean;
  onSelect: () => void;
  onClose: () => void;
}

function ClosableTab({ label, active, onSelect, onClose }: ClosableTabProps) {
  return (
    <span className={active ? 'tab closable-tab active' : 'tab closable-tab'}>
      <button
        role="tab"
        aria-selected={active}
        className="closable-tab-label"
        onClick={onSelect}
        title={label}
      >
        {label}
      </button>
      <button
        type="button"
        className="tab-close"
        aria-label={`Close ${label}`}
        title="Close tab"
        onClick={onClose}
      >
        ×
      </button>
    </span>
  );
}

export default App;
