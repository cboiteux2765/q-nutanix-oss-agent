import { useState, type FormEvent, type ReactNode } from 'react';
import {
  Activity,
  ArrowUpRight,
  Bell,
  ChevronRight,
  CircleDot,
  GitCommitHorizontal,
  GitPullRequest,
  LayoutDashboard,
  LoaderCircle,
  MessageCircle,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Settings2,
  Workflow as WorkflowIcon,
} from 'lucide-react';
import { api, errorText, post } from './api';
import { Modal, relativeDate, Status } from './components';
import {
  ACTIVE_STATES,
  type ActivitySettings,
  type Project,
  type ProjectDashboard,
  type RepoEvent,
  type Run,
  type Suggestion,
  type Usage,
  type Workflow,
  type WorkflowLimits,
} from './types';

const repositorySections = [
  { label: 'Issues', kinds: ['issue', 'issues'], icon: CircleDot, tone: 'green' },
  {
    label: 'Pull requests',
    kinds: ['pull_request', 'pull_requests', 'pr'],
    icon: GitPullRequest,
    tone: 'violet',
  },
  { label: 'Commits', kinds: ['commit', 'commits'], icon: GitCommitHorizontal, tone: 'blue' },
];
const phases = ['spec', 'code', 'test', 'qa'] as const;
const inProgress = (run: Run) =>
  ACTIVE_STATES.includes(run.status) || ['waiting', 'paused', 'blocked'].includes(run.status);
const needsAttention = (run: Run) =>
  ['waiting', 'failed', 'interrupted', 'blocked'].includes(run.status);
const label = (text: string) =>
  text === 'qa' ? 'QA' : text.charAt(0).toUpperCase() + text.slice(1).replaceAll('_', ' ');
const safeUrl = (value?: string) => {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : undefined;
  } catch {
    return undefined;
  }
};
const money = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

function usageLabel(usage?: Usage) {
  if (!usage) return { cost: '—', tokens: 'No recorded usage' };
  const tokens = usage.tokens ?? (usage.input || 0) + (usage.output || 0);
  const unresolved = usage.unresolved_cost_usd || 0;
  const cost = usage.cost_usd;
  return {
    cost:
      typeof cost === 'number' && cost > 0
        ? cost < 0.0001
          ? '<$0.0001'
          : money.format(cost)
        : '—',
    tokens: `${tokens.toLocaleString()} tokens`,
    unresolved: unresolved > 0 ? `${money.format(unresolved)} unsettled` : undefined,
  };
}

function FeedItem({ event }: { event: RepoEvent }) {
  const url = safeUrl(event.url);
  const date = relativeDate(event.occurred_at || event.created_at);
  const body = (
    <>
      <span>{event.title}</span>
      {date && <small>{date}</small>}
    </>
  );
  return url ? (
    <a
      href={url}
      className="dashboard-feed-item"
      target="_blank"
      rel="noreferrer"
      title={event.title}
    >
      {body}
      <ArrowUpRight size={12} aria-hidden="true" />
    </a>
  ) : (
    <div className="dashboard-feed-item" title={event.title}>
      {body}
    </div>
  );
}

function WorkflowRow({
  workflow,
  selected,
  busy,
  onOpen,
  onAction,
}: {
  workflow: Workflow;
  selected: boolean;
  busy: boolean;
  onOpen: (id: string, agentId?: string) => void;
  onAction: (id: string, action: 'pause' | 'resume') => void;
}) {
  const agents = workflow.agents || [];
  const agentCount = workflow.agent_count ?? agents.length;
  const activeAgents =
    workflow.active_agent_count ??
    agents.filter((agent) => ACTIVE_STATES.includes(agent.status)).length;
  const phaseIndex = phases.findIndex((phase) => phase === workflow.phase);
  const usage = usageLabel(workflow.usage || workflow.token_usage);
  const action =
    ACTIVE_STATES.includes(workflow.status) || workflow.status === 'waiting'
      ? 'pause'
      : ['paused', 'interrupted', 'failed', 'cancelled'].includes(workflow.status)
        ? 'resume'
        : null;
  return (
    <article className={`dashboard-workflow-row ${selected ? 'selected' : ''}`}>
      <button className="dashboard-workflow-name" onClick={() => onOpen(workflow.id)}>
        <span className="dashboard-workflow-icon">
          <WorkflowIcon size={17} />
        </span>
        <span>
          <strong title={workflow.title}>{workflow.title}</strong>
          <small>
            {workflow.phase ? label(workflow.phase) : 'Workflow'}
            {workflow.updated_at || workflow.created_at
              ? ` · ${relativeDate(workflow.updated_at || workflow.created_at)}`
              : ''}
          </small>
        </span>
      </button>
      <div className="dashboard-workflow-state">
        <Status status={workflow.status} />
        {!!agentCount && (
          <small>
            {activeAgents} of {agentCount} agents active
          </small>
        )}
      </div>
      <div className="dashboard-phase-track" aria-label="Workflow phases">
        {phases.map((phase, index) => {
          const matching = agents.filter((agent) => agent.role === phase);
          const agent =
            matching.find((agent) => ACTIVE_STATES.includes(agent.status)) || matching.at(-1);
          const past = phaseIndex > index || workflow.phase === 'ready';
          const state = phase === workflow.phase ? 'current' : past ? 'past' : '';
          const title = `${label(phase)}${agent ? ` · ${label(agent.status)}` : ''}`;
          return agent ? (
            <button
              key={phase}
              className={state}
              title={title}
              aria-label={`Inspect ${title}`}
              onClick={() => onOpen(workflow.id, agent.id)}
            >
              <i data-status={agent.status} aria-hidden="true" />
              {label(phase)}
            </button>
          ) : (
            <span key={phase} className={state} title={title}>
              {label(phase)}
            </span>
          );
        })}
      </div>
      <div
        className="dashboard-workflow-usage"
        title={usage.unresolved || (usage.cost === '—' ? 'Cost unavailable' : undefined)}
      >
        <strong>{usage.cost}</strong>
        <small>{usage.tokens}</small>
      </div>
      <div className="dashboard-workflow-actions">
        {action && (
          <button
            className="icon-button"
            aria-label={`${label(action)} ${workflow.title}`}
            title={`${label(action)} workflow`}
            onClick={() => onAction(workflow.id, action)}
            disabled={busy}
          >
            {busy ? (
              <LoaderCircle size={15} className="spin" />
            ) : action === 'pause' ? (
              <Pause size={15} />
            ) : (
              <Play size={15} />
            )}
          </button>
        )}
        <button
          className="icon-button"
          aria-label={`Open ${workflow.title}`}
          onClick={() => onOpen(workflow.id)}
        >
          <ChevronRight size={17} />
        </button>
      </div>
    </article>
  );
}

function ActivitySettingsDialog({
  initial,
  projectId,
  onClose,
  onSaved,
}: {
  initial: ActivitySettings;
  projectId: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [settings, setSettings] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError('');
    try {
      await api(`/projects/${projectId}/activity/settings`, {
        method: 'PATCH',
        body: JSON.stringify({
          repository: settings.repository?.trim() || null,
          enabled: settings.enabled,
          paused: settings.paused,
        }),
      });
      onSaved();
      onClose();
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="Repository activity"
      onClose={() => {
        if (!saving) onClose();
      }}
    >
      <form className="dashboard-dialog" onSubmit={save}>
        <label className="dashboard-field">
          GitHub repository
          <input
            value={settings.repository || ''}
            placeholder="owner/repository"
            onChange={(e) => setSettings({ ...settings, repository: e.target.value })}
          />
        </label>
        <label className="dashboard-check">
          <input
            type="checkbox"
            checked={settings.enabled}
            onChange={(e) => setSettings({ ...settings, enabled: e.target.checked })}
          />
          Enable repository activity
        </label>
        <label className="dashboard-check">
          <input
            type="checkbox"
            checked={settings.paused}
            onChange={(e) => setSettings({ ...settings, paused: e.target.checked })}
          />
          Pause activity checks
        </label>
        <p className="field-help">Review suggestions before starting a workflow.</p>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="dashboard-dialog-actions">
          <button className="button secondary" type="button" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button className="button primary" disabled={saving}>
            {saving && <LoaderCircle size={15} className="spin" />}Save settings
          </button>
        </div>
      </form>
    </Modal>
  );
}

function StartWorkflowDialog({
  projectId,
  suggestion,
  onClose,
  onStarted,
}: {
  projectId: string;
  suggestion: Suggestion | null;
  onClose: () => void;
  onStarted: (id: string) => void;
}) {
  const links = (suggestion?.source_urls || []).map(safeUrl).filter((url) => url !== undefined);
  const [message, setMessage] = useState(() => {
    if (!suggestion) return '';
    const number = suggestion.events?.find(
      (event) =>
        typeof event.number === 'number' && Number.isSafeInteger(event.number) && event.number > 0,
    )?.number;
    const subject = number ? ` on GitHub item #${number}` : '';
    return `Investigate the recorded repository activity${subject} and recommend next steps.`;
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  async function start(event: FormEvent) {
    event.preventDefault();
    if (!message.trim() || saving) return;
    setSaving(true);
    setError('');
    try {
      const workflow = await post<Run>(`/projects/${projectId}/workflows`, {
        message: message.trim(),
        ...(suggestion ? { source: { suggestion_id: suggestion.id } } : {}),
      });
      onStarted(workflow.id);
      onClose();
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setSaving(false);
    }
  }
  return (
    <Modal
      title={suggestion ? 'Review suggestion' : 'New workflow'}
      onClose={() => {
        if (!saving) onClose();
      }}
    >
      <form className="dashboard-dialog" onSubmit={start}>
        {suggestion?.summary && (
          <p className="dashboard-suggestion-summary">{suggestion.summary}</p>
        )}
        <label className="dashboard-field">
          Workflow instructions
          <textarea value={message} rows={6} onChange={(e) => setMessage(e.target.value)} />
        </label>
        {!!links.length && (
          <div className="dashboard-source-links">
            {links.map((url) => (
              <a href={url} target="_blank" rel="noreferrer" key={url}>
                {url}
                <ArrowUpRight size={13} />
              </a>
            ))}
          </div>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="dashboard-dialog-actions">
          <button className="button secondary" type="button" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button className="button primary" disabled={saving || !message.trim()}>
            {saving ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}
            Start workflow
          </button>
        </div>
      </form>
    </Modal>
  );
}

function WorkflowLimitsDialog({
  initial,
  projectId,
  onClose,
  onSaved,
}: {
  initial: WorkflowLimits;
  projectId: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [concurrency, setConcurrency] = useState(String(initial.max_concurrent_workflows));
  const [tokens, setTokens] = useState(String(initial.max_project_tokens ?? ''));
  const [cost, setCost] = useState(String(initial.max_project_cost_usd ?? ''));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError('');
    try {
      await api(`/projects/${projectId}/workflow-settings`, {
        method: 'PATCH',
        body: JSON.stringify({
          max_concurrent_workflows: Number(concurrency),
          max_project_tokens: tokens ? Number(tokens) : null,
          max_project_cost_usd: cost ? Number(cost) : null,
        }),
      });
      onSaved();
      onClose();
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="Workflow limits"
      onClose={() => {
        if (!saving) onClose();
      }}
    >
      <form className="dashboard-dialog" onSubmit={save}>
        <label className="dashboard-field">
          Concurrent workflows
          <input
            type="number"
            min={1}
            max={20}
            step={1}
            required
            value={concurrency}
            onChange={(e) => setConcurrency(e.target.value)}
          />
        </label>
        <label className="dashboard-field">
          Project token limit
          <input
            type="number"
            min={1}
            step={1}
            placeholder="No limit"
            value={tokens}
            onChange={(e) => setTokens(e.target.value)}
          />
        </label>
        <label className="dashboard-field">
          Project cost limit (USD)
          <input
            type="number"
            min={0}
            step="any"
            placeholder="No limit"
            value={cost}
            onChange={(e) => setCost(e.target.value)}
          />
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="dashboard-dialog-actions">
          <button className="button secondary" type="button" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button className="button primary" disabled={saving}>
            {saving && <LoaderCircle size={15} className="spin" />}Save limits
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function Dashboard({
  project,
  data,
  error,
  mainName,
  composer,
  selectedRunId,
  hasConversation,
  onConversation,
  onOpenRun,
  onRefresh,
  notify,
}: {
  project: Project;
  data: ProjectDashboard | null;
  error: string;
  mainName: string;
  composer: ReactNode;
  selectedRunId: string | null;
  hasConversation: boolean;
  onConversation: () => void;
  onOpenRun: (id: string, agentId?: string) => void;
  onRefresh: () => void;
  notify: (text: string, error?: boolean) => void;
}) {
  const [tab, setTab] = useState<'current' | 'history' | null>(null);
  const [settingsOpen, setSettingsOpen] = useState<'activity' | 'workflow' | null>(null);
  const [newWorkflow, setNewWorkflow] = useState(false);
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [polling, setPolling] = useState(false);
  const [pending, setPending] = useState<string[]>([]);
  const activity = data?.activity;
  const workflows = data?.workflows || [];
  const current = workflows.filter(inProgress);
  const history = workflows.filter((workflow) => !inProgress(workflow));
  const filter =
    tab ?? (history.some((workflow) => workflow.id === selectedRunId) ? 'history' : 'current');
  const suggestions = activity?.suggestions.filter((item) => item.status === 'pending') || [];
  const attention = workflows.filter(needsAttention);
  const usage = usageLabel(data?.usage);
  const pollingEnabled = activity?.settings.enabled && !activity.settings.paused;
  const feedStatus = activity?.settings.paused
    ? 'Paused'
    : activity?.settings.enabled
      ? 'Enabled'
      : 'Off';
  const selectedList = filter === 'current' ? current : history;
  const repoEmpty = activity?.settings.last_poll_at ? 'No recent activity' : '—';

  async function poll() {
    if (!pollingEnabled || polling) return;
    setPolling(true);
    try {
      const result = await post<ProjectDashboard['activity']>(
        `/projects/${project.id}/activity/poll`,
      );
      onRefresh();
      const error = result.settings.last_error;
      notify(error || 'Repository activity refreshed.', !!error);
    } catch (cause) {
      notify(errorText(cause), true);
    } finally {
      setPolling(false);
    }
  }
  async function changeWorkflow(id: string, action: 'pause' | 'resume') {
    setPending((old) => [...old, id]);
    try {
      await post(`/projects/${project.id}/workflows/${id}/${action}`);
      onRefresh();
    } catch (cause) {
      notify(errorText(cause), true);
    } finally {
      setPending((old) => old.filter((item) => item !== id));
    }
  }
  function workflowStarted(id: string) {
    setTab('current');
    onRefresh();
    onOpenRun(id);
    notify('Workflow started.');
  }

  return (
    <div className="dashboard">
      <div className="dashboard-heading">
        <div>
          <h1>{project.name}</h1>
          <p title={project.path}>{activity?.settings.repository || project.path}</p>
        </div>
        {data && (
          <div className="dashboard-metrics" aria-label="Project usage">
            <div>
              <span>Workflow slots</span>
              <strong>
                {
                  workflows.filter(
                    (run) => ACTIVE_STATES.includes(run.status) && !run.queue_pending,
                  ).length
                }
                {data.settings.max_concurrent_workflows
                  ? ` / ${data.settings.max_concurrent_workflows}`
                  : ''}
              </strong>
            </div>
            <div
              title={
                data.settings.max_project_tokens
                  ? `Limit: ${data.settings.max_project_tokens.toLocaleString()} tokens`
                  : undefined
              }
            >
              <span>Project tokens</span>
              <strong>
                {(
                  data.usage.tokens ?? (data.usage.input || 0) + (data.usage.output || 0)
                ).toLocaleString()}
              </strong>
            </div>
            <div
              title={
                usage.unresolved ||
                (data.settings.max_project_cost_usd
                  ? `Limit: ${money.format(data.settings.max_project_cost_usd)}`
                  : undefined)
              }
            >
              <span>Project cost</span>
              <strong>{usage.cost}</strong>
            </div>
          </div>
        )}
      </div>
      {error && (
        <div className="dashboard-error" role="status">
          <span>Dashboard couldn’t refresh. {error}</span>
          <button className="text-button" onClick={onRefresh}>
            <RefreshCw size={14} />
            Retry
          </button>
        </div>
      )}
      <div className="dashboard-grid">
        <section className="dashboard-card dashboard-chat" aria-labelledby="dashboard-chat-title">
          <div className="dashboard-chat-content">
            <h2 id="dashboard-chat-title">Talk to {mainName}</h2>
            {composer}
            <button className="dashboard-chat-link" onClick={onConversation}>
              <MessageCircle size={14} />
              {hasConversation ? 'Open conversation' : 'Open chat'}
              <ChevronRight size={14} />
            </button>
          </div>
        </section>
        <section
          className="dashboard-card dashboard-repository"
          aria-labelledby="repo-activity-title"
        >
          <div className="dashboard-card-heading">
            <div>
              <Activity size={16} />
              <h2 id="repo-activity-title">Repository activity</h2>
            </div>
            <div className="dashboard-card-actions">
              <button
                className="icon-button"
                aria-label="Refresh repository activity"
                title="Refresh repository activity"
                onClick={() => void poll()}
                disabled={!pollingEnabled || polling}
              >
                <RefreshCw size={14} className={polling ? 'spin' : ''} />
              </button>
              <button
                className="icon-button"
                aria-label="Repository activity settings"
                title="Repository activity settings"
                onClick={() => setSettingsOpen('activity')}
                disabled={!activity}
              >
                <Settings2 size={15} />
              </button>
            </div>
          </div>
          <div className="dashboard-feeds">
            {repositorySections.map((section) => {
              const entries =
                activity?.events.filter((event) => section.kinds.includes(event.kind)) || [];
              const count =
                section.kinds
                  .map((kind) => activity?.counts[kind])
                  .find((value) => typeof value === 'number') ?? entries.length;
              return (
                <div className="dashboard-feed" key={section.label}>
                  <h3>
                    <span data-tone={section.tone}>
                      <section.icon size={15} />
                    </span>
                    {section.label}
                    {(count > 0 || activity?.settings.last_poll_at) && <small>{count}</small>}
                  </h3>
                  {entries.length ? (
                    entries.slice(0, 2).map((event) => <FeedItem key={event.id} event={event} />)
                  ) : (
                    <p>{activity ? repoEmpty : '—'}</p>
                  )}
                </div>
              );
            })}
          </div>
          <div className="dashboard-card-footer">
            {activity ? (
              <>
                <i className={pollingEnabled ? 'ready' : ''} />
                {feedStatus}
                {activity.settings.catching_up
                  ? ' · Catching up'
                  : activity.settings.last_poll_at &&
                    ` · Checked ${relativeDate(activity.settings.last_poll_at).toLowerCase()}`}
              </>
            ) : error ? (
              'Activity unavailable'
            ) : (
              'Loading activity…'
            )}
          </div>
          {activity?.settings.last_error && (
            <p className="dashboard-activity-error">{activity.settings.last_error}</p>
          )}
        </section>
        <section
          className="dashboard-card dashboard-attention"
          aria-labelledby="needs-attention-title"
        >
          <div className="dashboard-card-heading">
            <div>
              <Bell size={16} />
              <h2 id="needs-attention-title">Needs attention</h2>
            </div>
            {data && (
              <span className="dashboard-attention-count">
                {suggestions.length + attention.length}
              </span>
            )}
          </div>
          {suggestions.length > 0 || attention.length > 0 ? (
            <div className="dashboard-attention-list">
              {suggestions.map((item) => (
                <button
                  key={item.id}
                  className="dashboard-attention-item"
                  onClick={() => setSuggestion(item)}
                >
                  <span>
                    <small>Suggestion</small>
                    <strong>{item.title}</strong>
                  </span>
                  <ChevronRight size={14} />
                </button>
              ))}
              {attention.map((run) => (
                <button
                  key={run.id}
                  className="dashboard-attention-item"
                  onClick={() => onOpenRun(run.id)}
                >
                  <span>
                    <small>
                      {run.status === 'waiting' ? 'Waiting for input' : label(run.status)}
                    </small>
                    <strong>{run.title}</strong>
                  </span>
                  <ChevronRight size={14} />
                </button>
              ))}
            </div>
          ) : (
            <div className="dashboard-attention-empty">
              <p>
                {data
                  ? 'You’re all caught up'
                  : error
                    ? 'Attention unavailable'
                    : 'Checking for updates…'}
              </p>
            </div>
          )}
        </section>
        <section className="dashboard-card dashboard-workflows" aria-labelledby="workflows-title">
          <div className="dashboard-card-heading">
            <div>
              <WorkflowIcon size={16} />
              <h2 id="workflows-title">Workflows</h2>
            </div>
            <div className="dashboard-card-actions">
              <button
                className="icon-button"
                aria-label="Workflow limits"
                title="Workflow limits"
                disabled={!data}
                onClick={() => setSettingsOpen('workflow')}
              >
                <Settings2 size={15} />
              </button>
              <button className="dashboard-plain-button" onClick={() => setNewWorkflow(true)}>
                <Plus size={16} />
                New workflow
              </button>
            </div>
          </div>
          <div className="dashboard-workflow-filters" role="group" aria-label="Filter workflows">
            <button
              className={filter === 'current' ? 'selected' : ''}
              aria-pressed={filter === 'current'}
              onClick={() => setTab('current')}
            >
              In progress{data && <span>{current.length}</span>}
            </button>
            <button
              className={filter === 'history' ? 'selected' : ''}
              aria-pressed={filter === 'history'}
              onClick={() => setTab('history')}
            >
              History{data && <span>{history.length}</span>}
            </button>
          </div>
          {selectedList.length ? (
            <div>
              {selectedList.map((workflow) => (
                <WorkflowRow
                  key={workflow.id}
                  workflow={workflow}
                  selected={workflow.id === selectedRunId}
                  busy={pending.includes(workflow.id)}
                  onOpen={onOpenRun}
                  onAction={(id, action) => void changeWorkflow(id, action)}
                />
              ))}
            </div>
          ) : (
            <div className="dashboard-workflows-empty">
              {data
                ? filter === 'current'
                  ? 'No workflows in progress'
                  : 'No past workflows'
                : error
                  ? 'Workflows unavailable'
                  : 'Loading workflows…'}
            </div>
          )}
        </section>
      </div>
      {settingsOpen === 'activity' && activity && (
        <ActivitySettingsDialog
          initial={activity.settings}
          projectId={project.id}
          onClose={() => setSettingsOpen(null)}
          onSaved={onRefresh}
        />
      )}
      {settingsOpen === 'workflow' && data && (
        <WorkflowLimitsDialog
          initial={data.settings}
          projectId={project.id}
          onClose={() => setSettingsOpen(null)}
          onSaved={onRefresh}
        />
      )}
      {(newWorkflow || suggestion) && (
        <StartWorkflowDialog
          projectId={project.id}
          suggestion={suggestion}
          onClose={() => {
            setNewWorkflow(false);
            setSuggestion(null);
          }}
          onStarted={workflowStarted}
        />
      )}
    </div>
  );
}

export function DashboardRail({
  data,
  onDashboard,
  onOpenRun,
}: {
  data: ProjectDashboard | null;
  onDashboard: () => void;
  onOpenRun: (id: string) => void;
}) {
  const current = data?.workflows.filter(inProgress) || [];
  const suggestions =
    data?.activity.suggestions.filter((item) => item.status === 'pending').length || 0;
  const attention = (data?.workflows.filter(needsAttention).length || 0) + suggestions;
  return (
    <aside className="dashboard-rail" aria-label="Project dashboard summary">
      <button className="dashboard-rail-home" onClick={onDashboard}>
        <LayoutDashboard size={16} />
        Dashboard
        <ChevronRight size={15} />
      </button>
      {data && (
        <div className="dashboard-rail-details">
          <div className="dashboard-rail-summary">
            <div>
              <WorkflowIcon size={14} />
              <span>In progress</span>
              <strong>{current.length}</strong>
            </div>
            <div>
              <Bell size={14} />
              <span>Needs attention</span>
              <strong>{attention}</strong>
            </div>
          </div>
          {!!current.length && (
            <div className="dashboard-rail-workflows">
              {current.slice(0, 4).map((run) => (
                <button key={run.id} onClick={() => onOpenRun(run.id)}>
                  <Status status={run.status} compact />
                  <span>
                    <strong>{run.title}</strong>
                    <small>{label(run.phase || run.status)}</small>
                  </span>
                  <ChevronRight size={13} />
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </aside>
  );
}
