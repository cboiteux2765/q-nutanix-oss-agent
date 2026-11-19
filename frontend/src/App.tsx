import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  FolderGit2,
  LoaderCircle,
  Menu,
  PanelRightOpen,
  RefreshCw,
  Settings2,
  X,
} from 'lucide-react';
import { api, arrayOf, errorText, mergeBySeq, post } from './api';
import { Logo, Toast, type ToastMessage } from './components';
import {
  ACTIVE_STATES,
  type Conversation,
  type Event,
  type Project,
  type ProjectDashboard,
  type Settings,
} from './types';
import ProjectModal from './ProjectModal';
import SettingsModal from './SettingsModal';
import ActivityPanel from './ActivityPanel';
import { MemoryView, SkillsView } from './Knowledge';
import { EvolutionView } from './Evolution';
import { SpecLabView } from './SpecLab';
import Onboarding from './Onboarding';
import MainAgentModal from './MainAgentModal';
import Sidebar, { RESOURCE_TABS, type View } from './Sidebar';
import { Composer, MessageList, NewConversation } from './Conversation';
import { Dashboard, DashboardRail } from './Dashboard';
import { useRun } from './useRun';
import { useWorkPanelKeyboard } from './useWorkPanelKeyboard';
import { useWorkspaceRoute } from './workspaceRoute';
import './workspaceMotion.css';

type ChatHistory = { messages: Event[]; firstSeq: number | null; hasMore: boolean };
type ComposerFocus = {
  projectId: string | null;
  view?: View;
  selection?: [number, number, HTMLTextAreaElement['selectionDirection']];
};
const emptyHistory: ChatHistory = { messages: [], firstSeq: null, hasMore: false };

export default function App() {
  const { route, navigate } = useWorkspaceRoute();
  const { view, runId, agentId, trace } = route;
  const [projects, setProjects] = useState<Project[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [loading, setLoading] = useState(true);
  const [serverError, setServerError] = useState('');
  const [toast, setToast] = useState<ToastMessage | null>(null);
  const [draft, setDraft] = useState('');
  const [pendingSend, setPendingSend] = useState<{
    projectId: string;
    text: string;
    after: number;
  } | null>(null);
  const [newAssignment, setNewAssignment] = useState(false);
  const [projectModal, setProjectModal] = useState(false);
  const [settingsModal, setSettingsModal] = useState(false);
  const [agentModal, setAgentModal] = useState(false);
  const [projectMenu, setProjectMenu] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [storedHistory, setHistory] = useState(emptyHistory);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [conversationLoading, setConversationLoading] = useState(false);
  const [conversationError, setConversationError] = useState('');
  const [dashboardData, setDashboard] = useState<ProjectDashboard | null>(null);
  const [dashboardError, setDashboardError] = useState('');
  const [inspectorError, setInspectorError] = useState(false);
  const [setup, setSetup] = useState(() => localStorage.getItem('evo-onboarding-v1') || 'invite');
  const [setupVersion, setSetupVersion] = useState(0);
  const [setupRequested, setSetupRequested] = useState(false);
  const [dataProject, setDataProject] = useState<string | null>(null);
  const [streamStart, setStreamStart] = useState<{ projectId: string; after: number } | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const composerFocus = useRef<ComposerFocus | null>(null);
  const workspaceBody = useRef<HTMLDivElement>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const currentProject = useRef<string | null>(null);
  const conversationGeneration = useRef(0);
  const loadingSupervisor = useRef<string | null>(null);
  const followMessages = useRef(true);
  const dashboardRequest = useRef(0);

  const [savedProject] = useState(() => localStorage.getItem('evo-project'));
  const routedProject = projects.find((item) => item.id === route.projectId);
  const project =
    routedProject || projects.find((item) => item.id === savedProject) || projects[0] || null;
  const projectId = project?.id || null;
  const dashboard = dataProject === projectId ? dashboardData : null;
  const history = dataProject === projectId ? storedHistory : emptyHistory;
  const chatMessages = history.messages;
  const onboarding = setup === 'invite' && (!project || setupRequested);
  const home = view === 'workspace' && !!project && !onboarding;
  const chatting = view === 'conversation' || (view === 'workspace' && !home);
  const workView = home || (chatting && !onboarding);
  const mainName = project?.main_agent?.name || 'EVO';
  const submitting = !!pendingSend && pendingSend.projectId === projectId;
  const pendingText =
    submitting &&
    !chatMessages.some(
      (event) =>
        event.seq > pendingSend.after &&
        event.data.role === 'user' &&
        event.data.text === pendingSend.text,
    )
      ? pendingSend.text
      : undefined;

  const notify = useCallback((message: string, error = false) => {
    setToast({ text: message, error });
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), error ? 9000 : 4500);
  }, []);
  const supervisor = useRun(notify);
  const inspector = useRun(notify);
  const {
    run: loadedSupervisor,
    detail: supervisorDetail,
    events: supervisorEvents,
    streaming,
    draft: supervisorDraft,
    clearRun: clearSupervisor,
    loadRun: loadSupervisor,
    isOpen: supervisorIsOpen,
    setDetail: setSupervisorDetail,
  } = supervisor;
  const supervisorRun = loadedSupervisor?.project_id === projectId ? loadedSupervisor : null;
  const { clearRun: clearInspector, loadRun: loadInspector } = inspector;
  const usesSupervisor = !!runId && runId === supervisorRun?.id;
  const inspectorRunId = workView && !usesSupervisor ? runId : null;
  const work = usesSupervisor ? supervisor : inspector;
  const panelOpen = workView && !!runId;
  const workRun =
    panelOpen && work.run?.id === runId && work.run.project_id === projectId ? work.run : null;
  const closeWork = useCallback(() => {
    navigate({ runId: null, agentId: null, trace: false });
  }, [navigate]);
  useWorkPanelKeyboard(
    workspaceBody,
    panelOpen && !loading && !serverError,
    workRun?.id,
    closeWork,
  );
  const workAgents = workRun ? work.detail?.agents || [] : [];
  const roster = workRun
    ? workAgents
    : (dashboard?.workflows || []).flatMap((workflow) =>
        (workflow.agents || []).filter((agent) => ACTIVE_STATES.includes(agent.status)),
      );

  const active = !!supervisorRun && ACTIVE_STATES.includes(supervisorRun.status);
  const delivered = useMemo(
    () =>
      new Set(
        supervisorEvents
          .filter((event) => event.kind === 'message.delivered')
          .map((event) => String(event.data.message_event_id)),
      ),
    [supervisorEvents],
  );

  const initialize = useCallback(async () => {
    setLoading(true);
    setServerError('');
    try {
      const [p, s] = await Promise.all([api<unknown>('/projects'), api<Settings>('/settings')]);
      setProjects(arrayOf<Project>(p, 'projects'));
      setSettings(s);
    } catch (cause) {
      setServerError(errorText(cause));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void initialize();
    return () => clearTimeout(toastTimer.current);
  }, [initialize]);

  useEffect(() => {
    if (projectId && !routedProject) {
      navigate({ projectId, runId: null, agentId: null, trace: false }, { replace: true });
    }
  }, [projectId, routedProject, navigate]);

  const loadConversation = useCallback(
    async (id: string) => {
      if (currentProject.current !== id) return;
      const generation = conversationGeneration.current;
      const conversation = await api<Conversation>(`/projects/${id}/conversation`);
      if (currentProject.current !== id || conversationGeneration.current !== generation) return;
      setHistory((old) => ({
        messages: mergeBySeq(old.messages, conversation.messages),
        firstSeq: old.firstSeq ?? conversation.messages[0]?.seq ?? null,
        hasMore: old.firstSeq === null ? conversation.has_more : old.hasMore,
      }));
      setConversationError('');
      const supervisorId = conversation.supervisor_run_id;
      if (
        supervisorId &&
        !supervisorIsOpen(supervisorId) &&
        loadingSupervisor.current !== supervisorId
      ) {
        loadingSupervisor.current = supervisorId;
        void loadSupervisor(supervisorId, id).finally(() => {
          if (conversationGeneration.current === generation) loadingSupervisor.current = null;
        });
      }
    },
    [loadSupervisor, supervisorIsOpen],
  );
  const refreshDashboard = useCallback(async (id: string) => {
    if (currentProject.current !== id) return;
    const request = ++dashboardRequest.current;
    try {
      const data = await api<ProjectDashboard>(`/projects/${id}/dashboard`);
      if (currentProject.current === id && request === dashboardRequest.current) {
        setDashboard(data);
        setDashboardError('');
        if (data.event_cursor !== undefined) {
          const after = data.event_cursor;
          setStreamStart((old) => (old?.projectId === id ? old : { projectId: id, after }));
        }
      }
    } catch (cause) {
      if (currentProject.current === id && request === dashboardRequest.current) {
        setDashboardError(errorText(cause));
      }
    }
  }, []);
  const [evolving, setEvolving] = useState<string | null>(null);
  useEffect(() => {
    currentProject.current = projectId;
    conversationGeneration.current++;
    loadingSupervisor.current = null;
    setDataProject(projectId);
    setStreamStart(null);
    clearSupervisor();
    setHistory(emptyHistory);
    setLoadingEarlier(false);
    setConversationLoading(!!projectId);
    setConversationError('');
    setDashboard(null);
    setDashboardError('');
    setDraft('');
    setNewAssignment(false);
    followMessages.current = true;
    if (!projectId) return;
    const id = projectId;
    let stopped = false;
    localStorage.setItem('evo-project', id);
    void loadConversation(id)
      .catch((cause) => {
        if (!stopped) setConversationError(errorText(cause));
      })
      .finally(() => {
        if (!stopped) setConversationLoading(false);
      });
    const refresh = () => {
      void refreshDashboard(id);
      // Workflow updates also appear in the project history, across run event streams.
      void loadConversation(id).catch(() => {});
    };
    void refreshDashboard(id);
    const timer = setInterval(refresh, 5000);
    return () => {
      stopped = true;
      currentProject.current = null;
      clearInterval(timer);
    };
  }, [projectId, clearSupervisor, loadConversation, refreshDashboard]);
  useEffect(() => {
    if (!projectId || streamStart?.projectId !== projectId) return;
    const source = new EventSource(`/api/projects/${projectId}/events?after=${streamStart.after}`);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let refreshChat = false;
    let refreshBoard = false;
    source.onmessage = (message) => {
      let event: Event;
      try {
        event = JSON.parse(message.data) as Event;
      } catch {
        return;
      }
      if (event.kind === 'message' && event.data.visible === true) refreshChat = true;
      if (
        event.kind.startsWith('workflow.') ||
        ['usage', 'run.status', 'agent.status', 'agent.created'].includes(event.kind)
      ) {
        refreshBoard = true;
      }
      if ((!refreshChat && !refreshBoard) || timer) return;
      timer = setTimeout(() => {
        if (refreshChat) void loadConversation(projectId).catch(() => {});
        if (refreshBoard) void refreshDashboard(projectId);
        refreshChat = false;
        refreshBoard = false;
        timer = undefined;
      }, 260);
    };
    return () => {
      source.close();
      clearTimeout(timer);
    };
  }, [projectId, streamStart, loadConversation, refreshDashboard]);
  useEffect(() => {
    if (!projectId) return;
    let stopped = false;
    const check = () =>
      api<{ experiments: { status: string; stage: string }[] }>(`/projects/${projectId}/evolution`)
        .then(({ experiments }) => {
          if (stopped) return;
          const experiment = experiments.find((entry) => ACTIVE_STATES.includes(entry.status));
          setEvolving(experiment?.stage || null);
        })
        .catch(() => {});
    void check();
    const timer = setInterval(check, 15000);
    return () => {
      stopped = true;
      clearInterval(timer);
      setEvolving(null);
    };
  }, [projectId]);
  useEffect(() => {
    clearInspector();
    setInspectorError(false);
    if (!projectId || !inspectorRunId) return;
    let superseded = false;
    void loadInspector(inspectorRunId, projectId).then((opened) => {
      if (!superseded && !opened) setInspectorError(true);
    });
    return () => {
      superseded = true;
    };
  }, [projectId, inspectorRunId, clearInspector, loadInspector]);

  useEffect(() => {
    if (!supervisorRun) return;
    const visible = supervisorEvents.filter(
      (event) =>
        event.kind === 'message' &&
        event.data.visible === true &&
        (!event.agent_id || event.agent_id === supervisorRun.root_agent_id),
    );
    if (!visible.length) return;
    setHistory((old) => {
      if (old.firstSeq === null) return old;
      // The run includes its complete history. Keep the project-level page boundary so
      // earlier workflow messages can still be fetched in order alongside supervisor replies.
      const firstSeq = old.firstSeq;
      return {
        ...old,
        messages: mergeBySeq(
          old.messages,
          visible.filter((event) => event.seq >= firstSeq),
        ),
      };
    });
  }, [supervisorEvents, supervisorRun?.root_agent_id]);

  async function earlierMessages() {
    const before = history.firstSeq;
    if (!projectId || before === null || !history.hasMore || loadingEarlier) return;
    const generation = conversationGeneration.current;
    const sameProject = () =>
      currentProject.current === projectId && conversationGeneration.current === generation;
    setLoadingEarlier(true);
    try {
      const page = await api<Conversation>(`/projects/${projectId}/conversation?before=${before}`);
      if (!sameProject()) return;
      followMessages.current = false;
      setHistory((old) =>
        old.firstSeq === before
          ? {
              messages: mergeBySeq(page.messages, old.messages),
              firstSeq: page.messages[0]?.seq ?? before,
              hasMore: page.has_more,
            }
          : old,
      );
    } catch (cause) {
      if (sameProject()) notify(errorText(cause), true);
    } finally {
      if (sameProject()) setLoadingEarlier(false);
    }
  }
  useLayoutEffect(() => {
    if (followMessages.current && chatting) bottom.current?.scrollIntoView({ block: 'end' });
  }, [chatting, chatMessages.length, streaming, supervisorDraft, submitting]);

  const attachComposer = useCallback(
    (input: HTMLTextAreaElement | null) => {
      const previous = composer.current;
      if (!input && previous && previous === document.activeElement) {
        // Capture the selection before switching views unmounts the input.
        composerFocus.current = {
          projectId,
          selection: [previous.selectionStart, previous.selectionEnd, previous.selectionDirection],
        };
      }
      composer.current = input;
    },
    [projectId],
  );
  useLayoutEffect(() => {
    const focus = composerFocus.current;
    const input = composer.current;
    composerFocus.current = null;
    if (
      !focus ||
      focus.projectId !== projectId ||
      (focus.view && focus.view !== route.view) ||
      !input
    )
      return;
    input.focus({ preventScroll: true });
    if (focus.selection) input.setSelectionRange(...focus.selection);
  }, [route, projectId, onboarding, loading, serverError]);

  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'k') {
        event.preventDefault();
        if (composer.current) composer.current.focus();
        else if (projectId && !onboarding && !loading && !serverError) {
          composerFocus.current = { projectId, view: 'conversation' };
          navigate({ view: 'conversation', runId: null, agentId: null, trace: false });
        }
      }
      if (event.key === 'Escape') {
        setSidebarOpen(false);
        setProjectMenu(false);
      }
    };
    document.addEventListener('keydown', key);
    return () => document.removeEventListener('keydown', key);
  }, [projectId, onboarding, loading, serverError, navigate]);

  function selectProject(next: Project) {
    setSetupRequested(false);
    setProjectMenu(false);
    setSidebarOpen(false);
    navigate({ projectId: next.id, view: 'workspace', runId: null, agentId: null, trace: false });
  }
  function changeView(next: View) {
    setSetupRequested(false);
    setSidebarOpen(false);
    setProjectMenu(false);
    if (next === 'conversation') followMessages.current = true;
    else setNewAssignment(false);
    navigate({ view: next, runId: null, agentId: null, trace: false });
  }
  function openWork(id: string, agent?: string) {
    if (!projectId || currentProject.current !== projectId) return;
    navigate({
      view: workView ? view : 'workspace',
      runId: id,
      agentId: agent || null,
      trace: !!agent,
    });
    setSidebarOpen(false);
  }
  function showSupervisorWork() {
    if (supervisorRun) openWork(supervisorRun.id);
  }
  async function send() {
    const message = draft.trim();
    if (!projectId || !message || pendingSend) return;
    if (!settings?.api_key_configured) {
      navigate({ view: 'conversation' }, { animate: false });
      setSettingsModal(true);
      return;
    }
    if (view !== 'conversation') composerFocus.current = { projectId, view: 'conversation' };
    setPendingSend({ projectId, text: message, after: chatMessages.at(-1)?.seq || 0 });
    setDraft('');
    followMessages.current = true;
    navigate({ view: 'conversation' });
    try {
      await post(`/projects/${projectId}/messages`, {
        message,
        new_assignment: view === 'conversation' && newAssignment,
      });
      if (currentProject.current === projectId) {
        setNewAssignment(false);
        void refreshDashboard(projectId);
        await loadConversation(projectId).catch((cause) => {
          if (currentProject.current === projectId) notify(errorText(cause), true);
        });
      }
    } catch (cause) {
      if (currentProject.current === projectId) setDraft((current) => current || message);
      notify(errorText(cause), true);
    } finally {
      setPendingSend(null);
    }
  }
  async function setSupervisorState(action: 'cancel' | 'resume') {
    if (!supervisorRun) return;
    const id = supervisorRun.id;
    try {
      await post(`/runs/${id}/${action}`);
      if (!supervisorIsOpen(id)) return;
      if (action === 'resume') await loadSupervisor(id, supervisorRun.project_id);
      else {
        setSupervisorDetail((current) =>
          current?.run.id === id
            ? { ...current, run: { ...current.run, status: 'cancelled' } }
            : current,
        );
        notify('Run stopped.');
      }
    } catch (cause) {
      if (supervisorIsOpen(id)) notify(errorText(cause), true);
    }
  }

  function beginSetup() {
    setSetupRequested(true);
    setDraft('');
    setSidebarOpen(false);
    setProjectMenu(false);
    setSetup('invite');
    setSetupVersion((version) => version + 1);
    localStorage.setItem('evo-onboarding-v1', 'invite');
    navigate(
      { view: 'conversation', runId: null, agentId: null, trace: false },
      { animate: false },
    );
  }
  function finishSetup(message: string) {
    localStorage.setItem('evo-onboarding-v1', 'complete');
    setSetup('complete');
    setSetupRequested(false);
    setDraft(message);
    composerFocus.current = { projectId, view: 'conversation' };
    navigate({ view: 'conversation' });
  }
  function addProject(next: Project, fromSetup = false) {
    setProjects((old) => [...old.filter((item) => item.id !== next.id), next]);
    setSetupRequested(fromSetup);
    setProjectMenu(false);
    navigate({
      projectId: next.id,
      view: fromSetup ? 'conversation' : 'workspace',
      runId: null,
      agentId: null,
      trace: false,
    });
  }
  function renderComposer(compact = false) {
    return (
      <Composer
        inputRef={attachComposer}
        project={project}
        settings={settings}
        run={supervisorRun}
        active={active}
        mainName={mainName}
        draft={draft}
        setDraft={setDraft}
        submitting={!!pendingSend}
        disabled={!project || loading}
        newAssignment={newAssignment}
        setNewAssignment={setNewAssignment}
        compact={compact}
        onSend={() => void send()}
        onStop={() => void setSupervisorState('cancel')}
        onRepository={() => {
          if (project) {
            setSidebarOpen(true);
            setProjectMenu(true);
          } else beginSetup();
        }}
        onModel={() => (project ? setAgentModal(true) : setSettingsModal(true))}
      />
    );
  }

  const conversation = (
    <>
      <div
        className="conversation-scroll"
        onScroll={(event) => {
          const container = event.currentTarget;
          followMessages.current =
            container.scrollHeight - container.scrollTop - container.clientHeight < 100;
        }}
      >
        {onboarding ? (
          <Onboarding
            key={setupVersion}
            project={project}
            settings={settings}
            onProject={(next) => addProject(next, true)}
            onSettings={() => setSettingsModal(true)}
            onComplete={finishSetup}
            onLater={() => {
              setSetup('later');
              setSetupRequested(false);
              localStorage.setItem('evo-onboarding-v1', 'later');
              if (project) changeView('workspace');
            }}
          />
        ) : conversationLoading && !submitting ? (
          <div className="app-loading">
            <LoaderCircle size={20} className="spin" />
            <p>Loading conversation…</p>
          </div>
        ) : !supervisorRun && !chatMessages.length && !submitting ? (
          <NewConversation
            project={project}
            onStarter={(text) => {
              setDraft(text);
              composer.current?.focus({ preventScroll: true });
            }}
            onSetup={beginSetup}
          />
        ) : (
          <MessageList
            run={supervisorRun}
            agents={supervisorDetail?.agents || []}
            messages={chatMessages}
            pendingText={pendingText}
            hasMore={history.hasMore}
            loadingEarlier={loadingEarlier}
            onEarlier={earlierMessages}
            streaming={streaming}
            draft={supervisorDraft}
            delivered={delivered}
            active={active}
            mainName={mainName}
            onShowWork={showSupervisorWork}
            onOpenSource={openWork}
            onResume={() => void setSupervisorState('resume')}
          />
        )}
        {conversationError && !onboarding && (
          <div className="conversation-load-error" role="status">
            <span>Conversation couldn’t load. {conversationError}</span>
            <button
              className="text-button"
              onClick={() => {
                if (!projectId) return;
                const generation = conversationGeneration.current;
                void loadConversation(projectId).catch((cause) => {
                  if (
                    currentProject.current === projectId &&
                    conversationGeneration.current === generation
                  ) {
                    setConversationError(errorText(cause));
                  }
                });
              }}
            >
              <RefreshCw size={14} />
              Retry
            </button>
          </div>
        )}
        <div ref={bottom} />
      </div>
      {!onboarding && renderComposer()}
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
  ) : view === 'spec' ? (
    <SpecLabView project={project} notify={notify} />
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
        onView={changeView}
        mainName={mainName}
        active={active}
        mainSelected={view === 'conversation'}
        onMainAgent={() => changeView('conversation')}
        onConfigureAgent={() => setAgentModal(true)}
        agents={roster}
        openAgentId={panelOpen ? agentId : null}
        onOpenAgent={(id) => {
          const agent = roster.find((entry) => entry.id === id);
          if (agent) openWork(agent.run_id, id);
        }}
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
            <div>
              <strong>
                {home
                  ? 'Dashboard'
                  : chatting
                    ? mainName
                    : RESOURCE_TABS.find((tab) => tab.id === view)?.label}
              </strong>
              {project?.name && <span>{project.name}</span>}
            </div>
          </div>
          <div className="topbar-actions">
            {chatting && panelOpen && (
              <button
                className="work-toggle dashboard-return"
                aria-label="Dashboard"
                onClick={() => changeView('workspace')}
              >
                <ArrowLeft size={15} />
                <span>Dashboard</span>
              </button>
            )}
            {workView && (supervisorRun || panelOpen) && (
              <button
                className={`work-toggle ${panelOpen ? 'selected' : ''}`}
                onClick={panelOpen ? closeWork : showSupervisorWork}
                aria-expanded={panelOpen}
                aria-label={panelOpen ? 'Close work panel' : 'Show agent activity'}
              >
                <PanelRightOpen size={17} />
                <span>Work</span>
                {(panelOpen ? workRun && ACTIVE_STATES.includes(workRun.status) : active) && (
                  <span className="live-dot" />
                )}
              </button>
            )}
            <button
              className="icon-button"
              aria-label="Settings"
              title="Settings"
              onClick={() => setSettingsModal(true)}
              disabled={!settings}
            >
              <Settings2 size={18} />
            </button>
          </div>
        </header>
        <div
          ref={workspaceBody}
          className={`workspace-body ${chatting ? 'workspace-focused' : ''}`}
        >
          <main
            className={`main-content ${chatting ? '' : 'scrollable'} ${home ? 'dashboard-main' : ''}`}
          >
            {loading ? (
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
                <button className="button primary" onClick={initialize}>
                  <RefreshCw size={15} />
                  Try again
                </button>
                <p className="field-help">
                  From the project folder, run <code>uv run evo --no-browser</code>.
                </p>
              </div>
            ) : home ? (
              <Dashboard
                key={project.id}
                project={project}
                data={dashboard}
                error={dashboardError}
                composer={renderComposer(true)}
                mainName={mainName}
                selectedRunId={runId}
                hasConversation={!!chatMessages.length}
                onConversation={() => {
                  composerFocus.current = { projectId, view: 'conversation' };
                  changeView('conversation');
                }}
                onOpenRun={openWork}
                onRefresh={() => void refreshDashboard(project.id)}
                notify={notify}
              />
            ) : chatting ? (
              conversation
            ) : (
              resources
            )}
          </main>
          {chatting && project && !onboarding && !panelOpen && !loading && !serverError && (
            <DashboardRail
              data={dashboard}
              onDashboard={() => changeView('workspace')}
              onOpenRun={openWork}
            />
          )}
          {panelOpen && !loading && !serverError && (
            <>
              <button
                className="activity-scrim"
                aria-label="Close work panel"
                tabIndex={-1}
                onClick={closeWork}
              />
              {workRun ? (
                <ActivityPanel
                  key={workRun.id}
                  run={workRun}
                  agents={workAgents}
                  events={work.events}
                  onClose={closeWork}
                  notify={notify}
                  selectedAgent={agentId}
                  {...{
                    traceAgentId: trace ? agentId : null,
                    onTraceChange: (id: string | null) =>
                      navigate({ agentId: id || agentId, trace: !!id }),
                  }}
                />
              ) : (
                <aside className="activity-panel dashboard-run-loading" aria-label="Work">
                  <div className="activity-heading">
                    <span>Work</span>
                    <button
                      className="icon-button"
                      aria-label="Close activity panel"
                      onClick={closeWork}
                    >
                      <X size={16} />
                    </button>
                  </div>
                  <div role="status">
                    {!inspectorError && <LoaderCircle size={16} className="spin" />}
                    {inspectorError ? 'Unable to open this run.' : 'Loading run…'}
                  </div>
                </aside>
              )}
            </>
          )}
        </div>
      </div>
      {projectModal && (
        <ProjectModal
          onClose={() => setProjectModal(false)}
          onCreate={(next) => {
            addProject(next);
            notify('Repository connected.');
          }}
        />
      )}
      {agentModal && project && (
        <MainAgentModal
          project={project}
          onClose={() => setAgentModal(false)}
          onSave={(next) => {
            setProjects((old) => old.map((item) => (item.id === next.id ? next : item)));
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
