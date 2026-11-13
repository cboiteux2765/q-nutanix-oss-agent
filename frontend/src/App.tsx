import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  ArrowRight,
  FolderGit2,
  LoaderCircle,
  Menu,
  PanelRightOpen,
  RefreshCw,
  Settings2,
} from 'lucide-react';
import { api, arrayOf, errorText, mergeBySeq, post } from './api';
import { Logo, Toast, type ToastMessage } from './components';
import {
  ACTIVE_STATES,
  type Conversation,
  type Event,
  type Project,
  type Run,
  type Settings,
} from './types';
import ProjectModal from './ProjectModal';
import SettingsModal from './SettingsModal';
import ActivityPanel from './ActivityPanel';
import { MemoryView, SkillsView } from './Knowledge';
import { EvolutionView } from './Evolution';
import Onboarding from './Onboarding';
import MainAgentModal from './MainAgentModal';
import Sidebar, { RESOURCE_TABS, type View } from './Sidebar';
import { Composer, MessageList, NewConversation } from './Conversation';
import { useRun } from './useRun';

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]),
    [project, setProject] = useState<Project | null>(null),
    [runs, setRuns] = useState<Run[]>([]),
    [settings, setSettings] = useState<Settings | null>(null),
    [view, setView] = useState<View>('conversation'),
    [draft, setDraft] = useState(''),
    [submitting, setSubmitting] = useState(false),
    [projectModal, setProjectModal] = useState(false),
    [settingsModal, setSettingsModal] = useState(false),
    [projectMenu, setProjectMenu] = useState(false),
    [panelOpen, setPanelOpen] = useState(false),
    [sidebarOpen, setSidebarOpen] = useState(false),
    [loading, setLoading] = useState(true),
    [serverError, setServerError] = useState(''),
    [toast, setToast] = useState<ToastMessage | null>(null),
    [chatMessages, setChatMessages] = useState<Event[]>([]),
    [hasMore, setHasMore] = useState(false),
    [selectedAgent, setSelectedAgent] = useState<string | null>(null),
    [agentModal, setAgentModal] = useState(false),
    [newAssignment, setNewAssignment] = useState(false),
    [conversationLoading, setConversationLoading] = useState(false),
    [setup, setSetup] = useState(() => localStorage.getItem('evo-onboarding-v1') || 'invite'),
    [setupVersion, setSetupVersion] = useState(0);
  const bottom = useRef<HTMLDivElement>(null),
    composer = useRef<HTMLTextAreaElement>(null),
    toastTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined),
    currentProject = useRef<string | null>(null),
    followMessages = useRef(true),
    onboardingVisible = useRef(false);
  const notify = useCallback((text: string, error = false) => {
    setToast({ text, error });
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), error ? 9000 : 4500);
  }, []);
  const {
    detail,
    setDetail,
    run,
    events,
    streaming,
    draft: runDraft,
    loadRun,
    clearRun,
    isOpen,
  } = useRun(notify);
  // A discreet sidebar indicator while an evolution experiment runs in the background.
  const [evolving, setEvolving] = useState<string | null>(null);
  useEffect(() => {
    if (!project) return;
    const id = project.id;
    let stopped = false;
    const check = () =>
      api<{ experiments: { status: string; stage: string }[] }>(`/projects/${id}/evolution`)
        .then((d) => {
          if (stopped) return;
          const running = d.experiments.find(
            (e) => e.status === 'running' || e.status === 'queued',
          );
          setEvolving(running ? running.stage : null);
        })
        .catch(() => {});
    void check();
    const timer = setInterval(check, 15000);
    return () => {
      stopped = true;
      clearInterval(timer);
      setEvolving(null);
    };
  }, [project?.id]);
  // User messages the coordinator has read (a quick reply may come first; reading is not applying).
  const delivered = useMemo(
    () =>
      new Set(
        events
          .filter((e) => e.kind === 'message.delivered')
          .map((e) => String(e.data.message_event_id)),
      ),
    [events],
  );
  const active = !!run && ACTIVE_STATES.includes(run.status);
  const agents = detail?.agents || [];
  const mainName = project?.main_agent?.name || 'EVO';

  const reloadRuns = useCallback(
    async (id: string) => {
      try {
        const list = arrayOf<Run>(await api(`/projects/${id}/runs`), 'runs').reverse();
        if (currentProject.current === id) setRuns(list);
      } catch (e) {
        notify(errorText(e), true);
      }
    },
    [notify],
  );
  const initialize = useCallback(async () => {
    setLoading(true);
    setServerError('');
    try {
      const [p, s] = await Promise.all([api<unknown>('/projects'), api<Settings>('/settings')]);
      const list = arrayOf<Project>(p, 'projects');
      setProjects(list);
      setSettings(s);
      const saved = localStorage.getItem('evo-project');
      setProject(list.find((x) => x.id === saved) || list[0] || null);
    } catch (e) {
      setServerError(errorText(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void initialize();
    return () => clearTimeout(toastTimer.current);
  }, [initialize]);

  /** Open a run in the conversation view. */
  const openRun = useCallback(
    async (id: string) => {
      if (!(await loadRun(id))) return;
      setSelectedAgent(null);
      followMessages.current = true;
      setView('conversation');
      setSidebarOpen(false);
    },
    [loadRun],
  );
  /** Show the project's central conversation and open its current run. */
  const loadConversation = useCallback(
    async (id: string) => {
      const conversation = await api<Conversation>(`/projects/${id}/conversation`);
      if (currentProject.current !== id) return;
      setChatMessages(conversation.messages);
      setHasMore(conversation.has_more);
      if (conversation.run_id) await openRun(conversation.run_id);
    },
    [openRun],
  );
  useEffect(() => {
    const id = project?.id || null;
    currentProject.current = id;
    clearRun();
    setChatMessages([]);
    setRuns([]);
    setSelectedAgent(null);
    setPanelOpen(false);
    setDraft('');
    setNewAssignment(false);
    setHasMore(false);
    if (!id) return;
    localStorage.setItem('evo-project', id);
    setConversationLoading(true);
    void reloadRuns(id);
    loadConversation(id)
      .catch((e) => {
        if (currentProject.current === id) notify(errorText(e), true);
      })
      .finally(() => {
        if (currentProject.current === id) setConversationLoading(false);
      });
  }, [project?.id, clearRun, loadConversation, reloadRuns, notify]);
  // Visible main-agent messages from the live event log join the conversation.
  useEffect(() => {
    if (!run) return;
    const visible = events.filter(
      (e) =>
        e.kind === 'message' &&
        e.data.visible === true &&
        (!e.agent_id || e.agent_id === run.root_agent_id),
    );
    setChatMessages((old) => mergeBySeq(old, visible));
  }, [events, run?.root_agent_id]);
  async function earlierMessages() {
    if (!project || !chatMessages.length) return;
    const id = project.id;
    try {
      const page = await api<Conversation>(
        `/projects/${id}/conversation?before=${chatMessages[0].seq}`,
      );
      if (currentProject.current !== id) return;
      followMessages.current = false;
      setChatMessages((old) => mergeBySeq(page.messages, old));
      setHasMore(page.has_more);
    } catch (e) {
      notify(errorText(e), true);
    }
  }
  useEffect(() => {
    if (followMessages.current)
      bottom.current?.scrollIntoView({ behavior: active ? 'instant' : 'smooth', block: 'end' });
  }, [run?.id, events.length, streaming, runDraft]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        composer.current?.focus();
      }
      if (e.key === 'Escape') {
        setSidebarOpen(false);
        setProjectMenu(false);
      }
    };
    document.addEventListener('keydown', key);
    return () => document.removeEventListener('keydown', key);
  }, []);
  function newConversation() {
    clearRun();
    setDraft('');
    setView('conversation');
    setSidebarOpen(false);
    setProjectMenu(false);
    setPanelOpen(false);
    followMessages.current = true;
    setTimeout(() => composer.current?.focus(), 10);
  }
  function selectProject(p: Project) {
    setProject(p);
    setProjectMenu(false);
    setView('conversation');
    setSidebarOpen(false);
  }
  async function send() {
    if (!draft.trim() || submitting) return;
    if (!project) {
      setSetup('invite');
      return;
    }
    if (!settings?.api_key_configured) {
      setSettingsModal(true);
      return;
    }
    const text = draft.trim();
    const projectId = project.id;
    setSubmitting(true);
    setDraft('');
    followMessages.current = true;
    try {
      const next = await post<Run>(`/projects/${projectId}/messages`, {
        message: text,
        new_assignment: newAssignment,
      });
      if (currentProject.current === projectId) {
        setNewAssignment(false);
        await openRun(next.id);
        const conversation = await api<Conversation>(`/projects/${projectId}/conversation`);
        if (currentProject.current === projectId)
          setChatMessages((old) => mergeBySeq(old, conversation.messages));
      }
      void reloadRuns(projectId);
    } catch (e) {
      if (currentProject.current === projectId) setDraft(text);
      notify(errorText(e), true);
    } finally {
      setSubmitting(false);
    }
  }
  // Stop and Resume can finish after the user has switched repositories or runs, so each
  // updates the screen only if the same run is still open.
  async function stop() {
    if (!run) return;
    const runId = run.id;
    try {
      await post(`/runs/${runId}/cancel`);
      if (!isOpen(runId)) return;
      setDetail((d) =>
        d && d.run.id === runId ? { ...d, run: { ...d.run, status: 'cancelled' } } : d,
      );
      notify('Run stopped. You can continue the conversation.');
    } catch (e) {
      if (isOpen(runId)) notify(errorText(e), true);
    }
  }
  async function resume() {
    if (!run) return;
    const runId = run.id;
    try {
      await post(`/runs/${runId}/resume`);
      if (isOpen(runId)) await openRun(runId);
    } catch (e) {
      if (isOpen(runId)) notify(errorText(e), true);
    }
  }
  function openAgent(id: string) {
    setSelectedAgent(id);
    setView('conversation');
    setPanelOpen(true);
    setSidebarOpen(false);
  }
  function showWork() {
    setSelectedAgent(null);
    setPanelOpen(true);
  }
  // Keep setup mounted while a project connected from setup loads its conversation;
  // unmounting it here would reset the setup step back to the start.
  const onboarding =
    !run && setup === 'invite' && (!conversationLoading || onboardingVisible.current);
  onboardingVisible.current = onboarding;
  function beginSetup() {
    newConversation();
    localStorage.setItem('evo-onboarding-v1', 'invite');
    setSetupVersion((version) => version + 1);
    setSetup('invite');
  }
  function finishSetup(message: string) {
    localStorage.setItem('evo-onboarding-v1', 'complete');
    setSetup('complete');
    setDraft(message);
    if (project && !run) {
      void loadConversation(project.id).catch((e) => notify(errorText(e), true));
    }
    setTimeout(() => composer.current?.focus(), 50);
  }
  function addProject(p: Project) {
    setProjects((old) => [...old.filter((x) => x.id !== p.id), p]);
    setProject(p);
    setProjectMenu(false);
  }
  function startWith(text: string) {
    setDraft(text);
    composer.current?.focus();
  }

  const conversation = (
    <>
      <div
        className="conversation-scroll"
        onScroll={(e) => {
          const el = e.currentTarget;
          followMessages.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
        }}
      >
        {onboarding ? (
          <Onboarding
            key={setupVersion}
            project={project}
            settings={settings}
            onProject={addProject}
            onSettings={() => setSettingsModal(true)}
            onComplete={finishSetup}
            onLater={() => {
              setSetup('later');
              localStorage.setItem('evo-onboarding-v1', 'later');
            }}
          />
        ) : !run && !chatMessages.length ? (
          <NewConversation project={project} onStarter={startWith} onSetup={beginSetup} />
        ) : (
          <MessageList
            run={run}
            agents={agents}
            messages={chatMessages}
            hasMore={hasMore}
            onEarlier={earlierMessages}
            streaming={streaming}
            draft={runDraft}
            delivered={delivered}
            active={active}
            mainName={mainName}
            onShowWork={showWork}
            onResume={resume}
          />
        )}
        <div ref={bottom} />
      </div>
      {!onboarding && (
        <Composer
          inputRef={composer}
          project={project}
          settings={settings}
          run={run}
          runs={runs}
          active={active}
          mainName={mainName}
          draft={draft}
          setDraft={setDraft}
          submitting={submitting}
          disabled={!project || loading}
          newAssignment={newAssignment}
          setNewAssignment={setNewAssignment}
          onSend={() => void send()}
          onStop={stop}
          onRepository={() => {
            if (project) {
              setSidebarOpen(true);
              setProjectMenu(true);
            } else beginSetup();
          }}
          onModel={() => (project ? setAgentModal(true) : setSettingsModal(true))}
          onViewRun={(id) => {
            void openRun(id);
            setPanelOpen(true);
          }}
        />
      )}
    </>
  );

  const resources = !project ? (
    <div className="empty-workspace">
      <FolderGit2 size={30} />
      <h1>A home for your project.</h1>
      <p>Connect a repository to start collecting memory, skills, and ideas.</p>
      <button className="button primary" onClick={beginSetup}>
        Set up a project
        <ArrowRight size={15} />
      </button>
    </div>
  ) : view === 'memory' ? (
    <MemoryView project={project} notify={notify} />
  ) : view === 'skills' ? (
    <SkillsView project={project} notify={notify} />
  ) : (
    <EvolutionView project={project} notify={notify} />
  );

  return (
    <div className="app-shell">
      {sidebarOpen && (
        <button
          className="sidebar-scrim"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <Sidebar
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        projects={projects}
        project={project}
        projectMenu={projectMenu}
        setProjectMenu={setProjectMenu}
        onSelectProject={selectProject}
        onConnectProject={() => setProjectModal(true)}
        onSetup={beginSetup}
        view={view}
        onView={(next) => {
          setView(next);
          setSidebarOpen(false);
        }}
        mainName={mainName}
        active={active}
        mainSelected={view === 'conversation' && !selectedAgent}
        onMainAgent={() => {
          setView('conversation');
          setSelectedAgent(null);
          setPanelOpen(false);
          setSidebarOpen(false);
        }}
        onConfigureAgent={() => setAgentModal(true)}
        agents={agents}
        openAgentId={panelOpen && view === 'conversation' ? selectedAgent : null}
        onOpenAgent={openAgent}
        settings={settings}
        evolving={evolving}
        onSettings={() => setSettingsModal(true)}
      />
      <div className="workspace">
        <header className="topbar">
          <button
            className="icon-button mobile-menu"
            aria-label="Open navigation"
            onClick={() => setSidebarOpen(true)}
          >
            <Menu size={20} />
          </button>
          <div className="chat-contact">
            <Logo small />
            <div>
              <strong>
                {view === 'conversation'
                  ? mainName
                  : RESOURCE_TABS.find((t) => t.id === view)?.label}
              </strong>
              {project?.name && <span>{project.name}</span>}
            </div>
          </div>
          <div className="topbar-actions">
            {view === 'conversation' && run && (
              <button
                className={`work-toggle ${panelOpen ? 'selected' : ''}`}
                onClick={() => {
                  setSelectedAgent(null);
                  setPanelOpen(!panelOpen);
                }}
                aria-expanded={panelOpen}
                title="Show agent activity"
              >
                <PanelRightOpen size={17} />
                <span>Work</span>
                {active && <span className="live-dot" />}
              </button>
            )}
            <button
              className="icon-button"
              aria-label="Conversation settings"
              title="Settings"
              onClick={() => setSettingsModal(true)}
              disabled={!settings}
            >
              <Settings2 size={18} />
            </button>
          </div>
        </header>
        <div className="workspace-body">
          <main className={`main-content ${view !== 'conversation' ? 'scrollable' : ''}`}>
            {loading || (conversationLoading && !onboarding) ? (
              <div className="app-loading">
                <Logo />
                <LoaderCircle size={20} className="spin" />
                <p>Opening your workspace…</p>
              </div>
            ) : serverError ? (
              <div className="connection-error">
                <AlertCircle size={28} />
                <h1>Let’s get connected.</h1>
                <p>The local EVO server isn’t available yet.</p>
                <code>{serverError}</code>
                <button className="button primary" onClick={() => initialize()}>
                  <RefreshCw size={15} />
                  Try again
                </button>
                <p className="field-help">
                  From the project folder, run <code>uv run evo --no-browser</code>.
                </p>
              </div>
            ) : view !== 'conversation' ? (
              resources
            ) : (
              conversation
            )}
          </main>
          {view === 'conversation' && panelOpen && run && !loading && !serverError && (
            <>
              <button
                className="activity-scrim"
                aria-label="Close work panel"
                onClick={() => setPanelOpen(false)}
              />
              <ActivityPanel
                run={run}
                agents={agents}
                events={events}
                onClose={() => setPanelOpen(false)}
                notify={notify}
                selectedAgent={selectedAgent}
              />
            </>
          )}
        </div>
      </div>
      {projectModal && (
        <ProjectModal
          onClose={() => setProjectModal(false)}
          onCreate={(p) => {
            addProject(p);
            setView('conversation');
            notify('Repository connected.');
          }}
        />
      )}
      {agentModal && project && (
        <MainAgentModal
          project={project}
          onClose={() => setAgentModal(false)}
          onSave={(p) => {
            setProjects((old) => old.map((item) => (item.id === p.id ? p : item)));
            if (currentProject.current === p.id) setProject(p);
            notify('Main agent updated for this repository.');
          }}
        />
      )}
      {settingsModal && settings && (
        <SettingsModal
          settings={settings}
          onClose={() => setSettingsModal(false)}
          onSave={setSettings}
        />
      )}
      {toast && <Toast toast={toast} onDismiss={() => setToast(null)} />}
    </div>
  );
}
