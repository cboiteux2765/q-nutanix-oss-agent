import { useRef, useState } from 'react';
import {
  AlertCircle,
  ChevronRight,
  ClipboardList,
  Code2,
  FlaskConical,
  GitBranch,
  History,
  LoaderCircle,
  MessageSquareText,
  RefreshCw,
  Search,
  ShieldCheck,
  Workflow as WorkflowIcon,
} from 'lucide-react';
import { Status } from './components';
import type { Project, Workflow } from './types';
import './AgentHistory.css';

const roles = new Map([
  ['spec', { label: 'Spec', icon: ClipboardList }],
  ['code', { label: 'Code', icon: Code2 }],
  ['test', { label: 'Test', icon: FlaskConical }],
  ['qa', { label: 'QA', icon: ShieldCheck }],
]);

const workflowDate = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  year: 'numeric',
});

type Props = {
  project: Project;
  role: string;
  workflows: Workflow[];
  loading: boolean;
  error: string;
  selectedAgentId: string | null;
  onOpen: (runId: string, agentId: string) => void;
  onOpenWorkflow: (runId: string) => void;
  onRefresh: () => void;
};

export default function AgentHistory({
  project,
  role,
  workflows,
  loading,
  error,
  selectedAgentId,
  onOpen,
  onOpenWorkflow,
  onRefresh,
}: Props) {
  const [search, setSearch] = useState('');
  const searchInput = useRef<HTMLInputElement>(null);
  const query = search.trim().toLowerCase();
  const { label, icon: RoleIcon } = roles.get(role) ?? { label: 'Phase', icon: History };
  const matches = (value?: string) => value?.toLowerCase().includes(query);
  const groups = workflows
    .filter((workflow) => workflow.project_id === project.id)
    .flatMap((workflow) => {
      const agents = (workflow.agents ?? []).filter((agent) => agent.role === role);
      const workflowMatches = !query || matches(workflow.title) || matches(workflow.branch);
      const attempts = agents
        .map((agent, index) => ({
          agent,
          name: agent.name || label,
          attempt: agents.length > 1 ? `Attempt ${index + 1}` : '',
        }))
        .filter(
          ({ agent, name, attempt }) =>
            workflowMatches || matches(`${name} ${attempt}`) || matches(agent.task),
        )
        .reverse();
      const createdAt = Date.parse(workflow.created_at);
      return attempts.length ? [{ workflow, attempts, createdAt }] : [];
    })
    .sort((left, right) => (right.createdAt || 0) - (left.createdAt || 0));
  const threadCount = groups.reduce((total, group) => total + group.attempts.length, 0);

  return (
    <section className="agent-history" aria-labelledby="agent-history-title">
      <header className="agent-history-heading">
        <h1 id="agent-history-title">
          <RoleIcon size={24} aria-hidden="true" />
          {label} agent history
        </h1>
        <div className="agent-history-tools">
          <label className="agent-history-search">
            <Search size={16} aria-hidden="true" />
            <span className="sr-only">Search {label} agent history</span>
            <input
              ref={searchInput}
              type="search"
              placeholder="Search workflows and tasks"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <button
            type="button"
            className="icon-button"
            aria-label="Refresh agent history"
            title="Refresh agent history"
            disabled={loading}
            onClick={onRefresh}
          >
            <RefreshCw size={16} className={loading ? 'spin' : undefined} aria-hidden="true" />
          </button>
        </div>
      </header>

      {error && (
        <div className="agent-history-error" role="alert">
          <AlertCircle size={16} aria-hidden="true" />
          <span>Couldn’t load agent history. {error}</span>
          <button type="button" disabled={loading} onClick={onRefresh}>
            Try again
          </button>
        </div>
      )}

      {groups.length ? (
        <>
          <p className="agent-history-summary" role="status">
            {threadCount} {threadCount === 1 ? 'thread' : 'threads'} across {groups.length}{' '}
            {groups.length === 1 ? 'workflow' : 'workflows'}
          </p>
          {groups.map(({ workflow, attempts, createdAt }) => (
            <section key={workflow.id} className="agent-history-group">
              <div className="agent-history-workflow">
                <WorkflowIcon size={18} aria-hidden="true" />
                <div>
                  <h2>
                    <button
                      type="button"
                      onClick={() => onOpenWorkflow(workflow.id)}
                      aria-label={`Open workflow ${workflow.title}`}
                      title={workflow.title}
                    >
                      <span>{workflow.title}</span>
                      <ChevronRight size={14} aria-hidden="true" />
                    </button>
                  </h2>
                  <div className="agent-history-meta">
                    <Status status={workflow.status} />
                    {Number.isFinite(createdAt) && (
                      <time dateTime={workflow.created_at}>{workflowDate.format(createdAt)}</time>
                    )}
                    {workflow.branch && (
                      <span className="agent-history-branch" title={workflow.branch}>
                        <GitBranch size={13} aria-hidden="true" />
                        <span>{workflow.branch}</span>
                      </span>
                    )}
                  </div>
                </div>
              </div>
              <ul className="agent-history-attempts" role="list">
                {attempts.map(({ agent, name, attempt }) => (
                  <li key={agent.id} data-selected={agent.id === selectedAgentId}>
                    <div className="agent-history-attempt">
                      <div>
                        <strong title={name}>{name}</strong>
                        {attempt && <small>{attempt}</small>}
                        <Status status={agent.status} />
                      </div>
                      {agent.task && <p title={agent.task}>{agent.task}</p>}
                    </div>
                    <button
                      type="button"
                      className="agent-history-thread"
                      aria-label={`View thread for ${name}${attempt ? `, ${attempt}` : ''} in ${workflow.title}`}
                      aria-current={agent.id === selectedAgentId || undefined}
                      onClick={() => onOpen(workflow.id, agent.id)}
                    >
                      <MessageSquareText size={15} aria-hidden="true" />
                      View thread
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </>
      ) : loading ? (
        <div className="agent-history-empty" role="status">
          <LoaderCircle size={20} className="spin" aria-hidden="true" />
          <p>Loading agent history…</p>
        </div>
      ) : !error ? (
        <div className="agent-history-empty">
          <History size={24} aria-hidden="true" />
          <h2 role="status">{query ? 'No matching threads' : `No ${label} agents yet`}</h2>
          {query ? (
            <button
              type="button"
              onClick={() => {
                setSearch('');
                searchInput.current?.focus();
              }}
            >
              Clear search
            </button>
          ) : (
            <p>Saved threads will appear here when a workflow runs a {label} agent.</p>
          )}
        </div>
      ) : null}
    </section>
  );
}
