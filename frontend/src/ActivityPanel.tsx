import { useEffect, useState } from 'react';
import {
  Bot,
  Activity,
  Files,
  ChevronRight,
  Layers3,
  FileText,
  X,
  ArrowUpRight,
  GitBranch,
  LoaderCircle,
  Check,
} from 'lucide-react';
import { api, arrayOf, errorText, post } from './api';
import {
  Empty,
  EventIcon,
  eventLabel,
  jsonText,
  MarkdownText,
  Modal,
  Status,
  timeLabel,
} from './components';
import type { Agent, Artifact, Event, Run } from './types';

// Capitalize only the first letter so agent names like "EVO · pytest" keep their casing.
const displayName = (name: string) => name.charAt(0).toUpperCase() + name.slice(1);

export default function ActivityPanel({
  run,
  agents,
  events,
  onClose,
  notify,
  selectedAgent,
}: {
  run: Run | null;
  agents: Agent[];
  events: Event[];
  onClose: () => void;
  notify: (s: string, error?: boolean) => void;
  selectedAgent?: string | null;
}) {
  const [tab, setTab] = useState('agents'),
    [expanded, setExpanded] = useState<string | null>(null),
    [artifacts, setArtifacts] = useState<Artifact[]>([]),
    [artifact, setArtifact] = useState<Artifact | null>(null),
    [selected, setSelected] = useState<Event | null>(null),
    [compacting, setCompacting] = useState<string | null>(null);
  useEffect(() => {
    if (selectedAgent) {
      setTab('agents');
      setExpanded(selectedAgent);
    }
  }, [selectedAgent]);
  useEffect(() => {
    if (!run) {
      setArtifacts([]);
      return;
    }
    if (tab === 'artifacts')
      api(`/runs/${run.id}/artifacts`)
        .then((v) => setArtifacts(arrayOf(v, 'artifacts')))
        .catch((e) => notify(errorText(e), true));
  }, [run?.id, tab, events.length, notify]);
  async function compact(id: string) {
    setCompacting(id);
    try {
      await post(`/agents/${id}/compact`);
      notify('Context compacted.');
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setCompacting(null);
    }
  }
  async function open(a: Artifact) {
    try {
      setArtifact({ ...a, ...(await api<Artifact>(`/artifacts/${a.id}`)) });
    } catch (e) {
      notify(errorText(e), true);
    }
  }
  const timeline = events
    .filter(
      (e) =>
        !['agent.delta', 'usage'].includes(e.kind) &&
        (!selectedAgent || e.agent_id === selectedAgent),
    )
    .slice(-150)
    .reverse();
  const roles = ['spec', 'code', 'test', 'qa'];
  const phase = run?.phase || 'triage';
  return (
    <aside className="activity-panel">
      <div className="activity-heading">
        <span>
          <Activity size={15} />
          Work
        </span>
        <button className="icon-button" aria-label="Close activity panel" onClick={onClose}>
          <X size={16} />
        </button>
      </div>
      <div className="activity-tabs">
        {[
          { id: 'agents', label: 'Agents', icon: Bot },
          { id: 'timeline', label: 'Timeline', icon: Activity },
          { id: 'artifacts', label: 'Artifacts', icon: Files },
        ].map((t) => (
          <button className={tab === t.id ? 'active' : ''} onClick={() => setTab(t.id)} key={t.id}>
            <t.icon size={14} />
            {t.label}
            {t.id === 'agents' && agents.length > 0 && <span>{agents.length}</span>}
          </button>
        ))}
      </div>
      {tab === 'agents' ? (
        <div className="panel-scroll">
          <div className="workflow-card">
            <div className="section-label">
              WORKFLOW <span>{phase === 'qa' ? 'QA' : phase}</span>
            </div>
            <div className="workflow-track">
              {roles.map((r, i) => {
                const past = roles.indexOf(phase) > i || phase === 'ready';
                return (
                  <div
                    key={r}
                    className={`workflow-step ${r === phase ? 'current' : ''} ${past ? 'past' : ''}`}
                  >
                    <span>{past ? <Check size={12} strokeWidth={2.5} /> : i + 1}</span>
                    <small>{r === 'qa' ? 'QA' : r[0].toUpperCase() + r.slice(1)}</small>
                  </div>
                );
              })}
            </div>
          </div>
          <div className="section-label padded">
            {selectedAgent ? 'AGENT DETAIL' : 'YOUR TEAM'}{' '}
            {!selectedAgent && (
              <span>{agents.filter((a) => a.status === 'running').length} working</span>
            )}
          </div>
          {agents.length ? (
            agents
              .filter((a) => !selectedAgent || a.id === selectedAgent)
              .map((a) => (
                <div className={`agent-card ${expanded === a.id ? 'expanded' : ''}`} key={a.id}>
                  <button
                    className="agent-card-heading"
                    onClick={() => setExpanded(expanded === a.id ? null : a.id)}
                  >
                    <span className="agent-avatar" data-role={a.role}>
                      {a.role === 'orchestrator' ? (
                        <span className="mini-logo">e</span>
                      ) : a.role === 'qa' ? (
                        'Q'
                      ) : (
                        a.role[0]?.toUpperCase()
                      )}
                    </span>
                    <span className="agent-identity">
                      <strong>{displayName(a.name || a.role)}</strong>
                      <small>
                        {a.role === 'orchestrator'
                          ? 'Keeping the whole picture'
                          : a.task?.slice(0, 62) || 'Ready for a task'}
                      </small>
                    </span>
                    <Status status={a.status} compact />
                    <ChevronRight size={13} className={expanded === a.id ? 'rotate-quarter' : ''} />
                  </button>
                  {expanded === a.id && (
                    <div className="agent-expanded">
                      <div className="agent-state">
                        <Status status={a.status} />
                        <code>{a.id.slice(0, 8)}</code>
                      </div>
                      <details className="agent-assignment">
                        <summary>Assignment</summary>
                        <p>{a.task}</p>
                      </details>
                      {a.result != null && (
                        <div className="agent-result">
                          <MarkdownText>{jsonText(a.result)}</MarkdownText>
                        </div>
                      )}
                      {a.error && <div className="form-error">{a.error}</div>}
                      <div className="agent-live-trail">
                        <div className="section-label">RECENT ACTIVITY</div>
                        {timeline
                          .filter((e) => e.agent_id === a.id)
                          .slice(0, 8)
                          .map((e) => (
                            <button
                              key={e.seq}
                              className="agent-trail-event"
                              onClick={() => setSelected(e)}
                            >
                              <EventIcon kind={e.kind} />
                              <span>{eventLabel(e)}</span>
                              <time>{timeLabel(e.created_at)}</time>
                            </button>
                          ))}
                      </div>
                      <button
                        className="text-button"
                        disabled={compacting !== null || a.status === 'running'}
                        onClick={() => compact(a.id)}
                      >
                        {compacting === a.id ? (
                          <LoaderCircle size={13} className="spin" />
                        ) : (
                          <Layers3 size={13} />
                        )}
                        Compact context
                      </button>
                    </div>
                  )}
                </div>
              ))
          ) : (
            <Empty icon={<Bot size={25} />} title="Your team assembles here.">
              Start a conversation. EVO will bring in the right agents as the work takes shape.
            </Empty>
          )}
          {run?.branch && (
            <div className="branch-note">
              <GitBranch size={14} />
              <div>
                <span>Working branch</span>
                <code>{run.branch}</code>
              </div>
            </div>
          )}
        </div>
      ) : tab === 'timeline' ? (
        <div className="panel-scroll timeline">
          {timeline.length ? (
            timeline.map((e) => (
              <button className="timeline-event" key={e.seq} onClick={() => setSelected(e)}>
                <span className="timeline-icon">
                  <EventIcon kind={e.kind} />
                </span>
                <span>
                  <strong>{eventLabel(e)}</strong>
                  <small>
                    {agents.find((a) => a.id === e.agent_id)?.name || 'Workspace'} ·{' '}
                    {timeLabel(e.created_at)}
                  </small>
                </span>
                <ChevronRight size={12} />
              </button>
            ))
          ) : (
            <Empty icon={<Activity size={25} />} title="A clear trail of the work.">
              Messages, decisions, and tool activity will appear here as your agents get started.
            </Empty>
          )}
        </div>
      ) : (
        <div className="panel-scroll artifacts-list">
          {artifacts.length ? (
            artifacts.map((a) => (
              <button className="artifact-row" key={a.id} onClick={() => open(a)}>
                <FileText size={19} />
                <span>
                  <strong>{a.name || a.title || a.path || a.id}</strong>
                  <small>
                    {a.kind || 'Artifact'} · {timeLabel(a.created_at)}
                  </small>
                </span>
                <ArrowUpRight size={13} />
              </button>
            ))
          ) : (
            <Empty icon={<Files size={25} />} title="The work, made tangible.">
              Specs, implementation notes, and verification reports will collect here.
            </Empty>
          )}
        </div>
      )}
      {run?.token_usage && (
        <div className="panel-footer">
          <span>
            {((run.token_usage.input || 0) + (run.token_usage.output || 0)).toLocaleString()} tokens
            {(run.token_usage.cached || 0) > 0 &&
              ` · ${(run.token_usage.cached || 0).toLocaleString()} cached`}
          </span>
        </div>
      )}
      {selected && (
        <Modal
          title={eventLabel(selected)}
          subtitle={`${selected.kind} · event ${selected.seq}`}
          onClose={() => setSelected(null)}
          wide
        >
          <div className="event-detail">
            <pre>{JSON.stringify(selected.data, null, 2)}</pre>
          </div>
        </Modal>
      )}
      {artifact && (
        <Modal
          title={artifact.name || artifact.title || 'Artifact'}
          subtitle={artifact.path || artifact.id}
          onClose={() => setArtifact(null)}
          wide
        >
          <div className="skill-detail">
            <MarkdownText>
              {typeof artifact.content === 'string'
                ? artifact.content
                : JSON.stringify(artifact, null, 2)}
            </MarkdownText>
          </div>
        </Modal>
      )}
    </aside>
  );
}
