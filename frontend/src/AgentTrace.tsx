import { memo, useDeferredValue, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Activity,
  Bot,
  ChevronRight,
  Download,
  FileText,
  GitBranch,
  LoaderCircle,
  MessagesSquare,
  RefreshCw,
  Search,
  Terminal,
  X,
} from 'lucide-react';
import { api, errorText, mergeBySeq } from './api';
import { EventIcon, eventLabel, MarkdownText, Status } from './components';
import type { Agent, Artifact, Event, Run } from './types';
import './AgentTrace.css';

type Reasoning = { kind: 'content' | 'summary'; text: string };
type ToolCall = { id: string; function: { name: string; arguments: unknown } };
type TranscriptEntry = {
  seq: number;
  event_id: string | null;
  created_at: string | null;
  message: { role: string; content: unknown; tool_calls?: ToolCall[]; tool_call_id?: string };
  reasoning: Reasoning[];
  source: string;
};
type Page<T> = {
  entries: T[];
  next_cursor: number | null;
  completeness?: 'complete' | 'legacy_partial';
};
type ToolSpan = { key: string; name: string; parent?: string; events: Event[] };

const PAGE_SIZE = 100;
const POLL_INTERVAL = 3000;
const EXPORT_LIMIT = 64 * 1024 * 1024;
const ROLE_LABELS: Record<string, string> = {
  system: 'System instructions',
  user: 'Input',
  assistant: 'Assistant',
  tool: 'Tool result',
};
const SOURCE_LABELS: Record<string, string> = {
  legacy_context: 'Recovered',
  legacy_event: 'Recovered',
  quick_responder: 'Quick responder',
  supervisor_context: 'Supervisor context',
};

function tracePage<T extends { seq: number }>(path: string, after: number, signal: AbortSignal) {
  return api<Page<T>>(`${path}?after=${after}&limit=${PAGE_SIZE}`, { signal });
}

function cursorAfter<T extends { seq: number }>(page: Page<T>, after: number) {
  const cursor = page.next_cursor ?? page.entries.at(-1)?.seq ?? after;
  if (page.next_cursor !== null && cursor <= after)
    throw new Error('The trace cursor did not advance.');
  return Math.max(cursor, after);
}

function useTracePages<T extends { seq: number }>(path: string) {
  const [entries, setEntries] = useState<T[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [completeness, setCompleteness] = useState<Page<T>['completeness']>();
  const position = useRef({ after: 0, hasMore: false });
  const request = useRef<(loadMore?: boolean) => void>(() => undefined);

  useEffect(() => {
    const controller = new AbortController();
    let firstPage = true;
    let pending = false;
    async function load(loadMore = false) {
      if (pending || controller.signal.aborted || (position.current.hasMore && !loadMore)) return;
      pending = true;
      if (firstPage || loadMore) setLoading(true);
      const { after } = position.current;
      try {
        const page = await tracePage<T>(path, after, controller.signal);
        if (controller.signal.aborted) return;
        const more = page.next_cursor !== null;
        position.current = { after: cursorAfter(page, after), hasMore: more };
        firstPage = false;
        setHasMore(more);
        setCompleteness(page.completeness);
        if (page.entries.length) setEntries((previous) => mergeBySeq(previous, page.entries));
        setError('');
      } catch (e) {
        if (!controller.signal.aborted) setError(errorText(e));
      } finally {
        pending = false;
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    request.current = (loadMore) => void load(loadMore);
    void load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load();
    }, POLL_INTERVAL);
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [path]);

  return { entries, hasMore, loading, error, completeness, request };
}

function pretty(value: unknown) {
  if (typeof value === 'string') {
    try {
      const parsed: unknown = JSON.parse(value);
      if (parsed && typeof parsed === 'object') return JSON.stringify(parsed, null, 2);
    } catch {
      // Plain text is valid output.
    }
    return value;
  }
  return JSON.stringify(value, null, 2) ?? '';
}

function time(date: string | null) {
  if (!date) return '';
  const value = new Date(date);
  return Number.isNaN(value.getTime())
    ? ''
    : value.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

function objectOutput(value: unknown): Record<string, unknown> {
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return {};
    }
  }
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function artifactIds(data: Record<string, unknown>) {
  const output = objectOutput(data.result);
  return [
    data.artifact_id,
    data.main_artifact_id,
    output.artifact_id,
    output.main_artifact_id,
    data.artifact_ids,
    output.artifact_ids,
  ]
    .flat()
    .filter((id): id is string => typeof id === 'string' && id.length > 0);
}

function indexActivity(events: Event[]) {
  const spans = new Map<string, ToolSpan>();
  const activity: (ToolSpan | Event)[] = [];
  for (const event of events) {
    if (!['tool.started', 'tool.finished', 'tool.output'].includes(event.kind)) {
      activity.push(event);
      continue;
    }
    const { span_id, call_id, parent_span_id, name } = event.data;
    const key =
      typeof span_id === 'string'
        ? `span:${span_id}`
        : typeof call_id === 'string'
          ? `call:${call_id}`
          : `event:${event.id}`;
    let span = spans.get(key);
    if (!span) {
      span = { key, name: typeof name === 'string' ? name : 'Tool', events: [] };
      spans.set(key, span);
      activity.push(span);
    }
    if (typeof parent_span_id === 'string') span.parent = `span:${parent_span_id}`;
    span.events.push(event);
  }
  return { spans, activity };
}

function CodeBlock({ value }: { value: unknown }) {
  return <pre className="trace-code">{pretty(value)}</pre>;
}

function Disclosure({
  summary,
  children,
  event = false,
}: {
  summary: ReactNode;
  children: ReactNode;
  event?: boolean;
}) {
  const [visited, setVisited] = useState(false);
  return (
    <details
      className={event ? 'trace-event' : 'trace-disclosure'}
      onToggle={(e) => {
        if (e.currentTarget.open && !visited) setVisited(true);
      }}
    >
      <summary>{summary}</summary>
      {visited && (event ? children : <div className="trace-detail-body">{children}</div>)}
    </details>
  );
}

function TraceArtifact({ id }: { id: string }) {
  const [opened, setOpened] = useState(false);
  const [artifact, setArtifact] = useState<Artifact>();
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!opened) return;
    const controller = new AbortController();
    api<Artifact>(`/artifacts/${encodeURIComponent(id)}`, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setArtifact(value);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(errorText(e));
      });
    return () => controller.abort();
  }, [opened, id, attempt]);

  return (
    <details
      className="trace-disclosure"
      onToggle={(e) => {
        if (e.currentTarget.open) setOpened(true);
      }}
    >
      <summary>
        <FileText size={14} /> {artifact?.name || `Full output · ${id.slice(0, 8)}`}
      </summary>
      <div className="trace-detail-body">
        {artifact ? (
          <CodeBlock value={artifact.content} />
        ) : error ? (
          <div className="trace-inline-status">
            <span role="alert">{error}</span>
            <button
              className="text-button"
              onClick={() => {
                setError('');
                setAttempt((value) => value + 1);
              }}
            >
              Retry
            </button>
          </div>
        ) : (
          <span className="trace-inline-status" role="status">
            Loading artifact…
          </span>
        )}
      </div>
    </details>
  );
}

function ToolDetails({ span }: { span: ToolSpan }) {
  const start = span.events.find((event) => event.kind === 'tool.started');
  const finish = span.events.find((event) => event.kind === 'tool.finished');
  const artifacts = [...new Set(span.events.flatMap((event) => artifactIds(event.data)))];
  return (
    <>
      {start?.data.arguments != null && (
        <Disclosure summary="Arguments">
          <CodeBlock value={start.data.arguments} />
        </Disclosure>
      )}
      {finish?.data.result !== undefined && (
        <Disclosure summary="Result">
          <CodeBlock value={finish.data.result} />
        </Disclosure>
      )}
      {artifacts.map((id) => (
        <TraceArtifact key={id} id={id} />
      ))}
      <Disclosure
        summary={
          <>
            Recorded events <span className="trace-count">{span.events.length}</span>
          </>
        }
      >
        <CodeBlock value={span.events} />
      </Disclosure>
    </>
  );
}

function TraceTime({ value }: { value: string | null }) {
  return value ? <time dateTime={value}>{time(value)}</time> : null;
}

const ActivityRow = memo(function ActivityRow({
  item,
  parent,
}: {
  item: ToolSpan | Event;
  parent?: ToolSpan;
}) {
  if ('events' in item) {
    const first = item.events[0];
    return (
      <div className="trace-row">
        <span className="trace-row-icon">
          <Terminal size={16} />
        </span>
        <div className="trace-row-content">
          <Disclosure
            event
            summary={
              <>
                <strong>{item.name}</strong>
                <span className="trace-row-meta">
                  <TraceTime value={first.created_at} />
                  <ChevronRight size={14} />
                </span>
              </>
            }
          >
            <ToolDetails span={item} />
            {item.parent && (
              <Disclosure
                summary={
                  <>
                    <GitBranch size={14} /> Parent span · {parent?.name || item.parent.slice(5)}
                  </>
                }
              >
                {parent ? (
                  <ToolDetails span={parent} />
                ) : (
                  <p className="trace-inline-status">
                    The parent event is outside the loaded activity.
                  </p>
                )}
              </Disclosure>
            )}
          </Disclosure>
        </div>
      </div>
    );
  }
  const ids = artifactIds(item.data);
  return (
    <div className="trace-row">
      <span className="trace-row-icon">
        <EventIcon kind={item.kind} />
      </span>
      <div className="trace-row-content">
        <Disclosure
          event
          summary={
            <>
              <span className="trace-event-label">
                <strong>{eventLabel(item)}</strong>
                <small>{item.kind}</small>
              </span>
              <span className="trace-row-meta">
                <TraceTime value={item.created_at} />
                <ChevronRight size={14} />
              </span>
            </>
          }
        >
          <CodeBlock value={item.data} />
          {ids.map((id) => (
            <TraceArtifact key={id} id={id} />
          ))}
        </Disclosure>
      </div>
    </div>
  );
});

const TranscriptRow = memo(function TranscriptRow({ entry }: { entry: TranscriptEntry }) {
  const { message, reasoning } = entry;
  const { role, content, tool_calls } = message;
  const label = ROLE_LABELS[role] || role;
  const Icon =
    role === 'assistant'
      ? Bot
      : role === 'tool'
        ? Terminal
        : role === 'system'
          ? FileText
          : MessagesSquare;
  const availableReasoning = reasoning.filter(
    ({ kind, text }) => (kind === 'content' || kind === 'summary') && text.trim(),
  );
  return (
    <div className="trace-row" data-role={role}>
      <span className="trace-row-icon">
        <Icon size={16} />
      </span>
      <div className="trace-row-content">
        <div className="trace-row-heading">
          <strong>{label}</strong>
          <span className="trace-row-meta">
            {SOURCE_LABELS[entry.source] && <span>{SOURCE_LABELS[entry.source]}</span>}
            <TraceTime value={entry.created_at} />
          </span>
        </div>
        {content != null &&
          content !== '' &&
          (role === 'system' || role === 'tool' ? (
            <Disclosure
              summary={
                <>
                  {role === 'system' ? 'View instructions' : 'View result'}
                  {message.tool_call_id && <code>{message.tool_call_id}</code>}
                </>
              }
            >
              <CodeBlock value={content} />
            </Disclosure>
          ) : typeof content === 'string' ? (
            <MarkdownText>{content}</MarkdownText>
          ) : (
            <CodeBlock value={content} />
          ))}
        {role === 'tool' &&
          [...new Set(artifactIds(objectOutput(content)))].map((id) => (
            <TraceArtifact key={id} id={id} />
          ))}
        {(tool_calls || []).map((call) => (
          <Disclosure
            key={call.id}
            summary={
              <>
                <Terminal size={14} /> {call.function.name} <code>{call.id}</code>
              </>
            }
          >
            <CodeBlock value={call.function.arguments} />
          </Disclosure>
        ))}
        {availableReasoning.map((item, index) => (
          <Disclosure
            key={index}
            summary={item.kind === 'summary' ? 'Provider reasoning summary' : 'Provider reasoning'}
          >
            <MarkdownText>{item.text}</MarkdownText>
          </Disclosure>
        ))}
        {role === 'assistant' && !availableReasoning.length && (
          <small className="trace-no-reasoning">
            {entry.source.startsWith('legacy_')
              ? 'Provider reasoning unavailable in recovered history'
              : 'No provider reasoning recorded'}
          </small>
        )}
      </div>
    </div>
  );
});

function downloadJson(filename: string, data: string) {
  const blob = new Blob([data], { type: 'application/json' });
  if (blob.size > EXPORT_LIMIT)
    throw new Error('The trace exceeds the 64 MiB browser export limit.');
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

type TraceProps = { run: Run; agent: Agent; onClose: () => void };

export default function AgentTrace(props: TraceProps) {
  return (
    <TraceDialog key={`${props.run.project_id}/${props.run.id}/${props.agent.id}`} {...props} />
  );
}

function TraceDialog({ run, agent, onClose }: TraceProps) {
  const path = `/projects/${encodeURIComponent(run.project_id)}/runs/${encodeURIComponent(run.id)}/agents/${encodeURIComponent(agent.id)}/trace`;
  const transcript = useTracePages<TranscriptEntry>(`${path}/transcript`);
  const events = useTracePages<Event>(`${path}/events`);
  const [view, setView] = useState<'transcript' | 'events'>('transcript');
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [exporting, setExporting] = useState(false);
  const [exportMessage, setExportMessage] = useState('');
  const [exportError, setExportError] = useState(false);
  const exportRequest = useRef<AbortController | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const query = useDeferredValue(search.trim().toLowerCase());
  const { spans, activity } = useMemo(() => indexActivity(events.entries), [events.entries]);

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => {
      element?.close();
      exportRequest.current?.abort();
      exportRequest.current = null;
    };
  }, []);

  const visibleMessages = useMemo(
    () =>
      transcript.entries.filter(
        (entry) =>
          (filter === 'all' || filter === entry.message.role) &&
          (!query ||
            ROLE_LABELS[entry.message.role]?.toLowerCase().includes(query) ||
            SOURCE_LABELS[entry.source]?.toLowerCase().includes(query) ||
            JSON.stringify(entry).toLowerCase().includes(query)),
      ),
    [transcript.entries, filter, query],
  );
  const visibleActivity = useMemo(
    () =>
      activity.filter((item) => {
        const tool = 'events' in item;
        return (
          (filter === 'all' || filter === (tool ? 'tool' : item.kind.split('.')[0])) &&
          (!query ||
            (tool ? item.name : eventLabel(item)).toLowerCase().includes(query) ||
            JSON.stringify(item).toLowerCase().includes(query))
        );
      }),
    [activity, filter, query],
  );
  const viewData = view === 'transcript' ? transcript : events;
  const roles = [...new Set(transcript.entries.map((entry) => entry.message.role))];
  const kinds = [
    ...new Set(activity.map((item) => ('events' in item ? 'tool' : item.kind.split('.')[0]))),
  ].sort();
  const matches = view === 'transcript' ? visibleMessages.length : visibleActivity.length;

  async function exportTrace() {
    if (exportRequest.current) return;
    const controller = new AbortController();
    exportRequest.current = controller;
    setExporting(true);
    setExportError(false);
    setExportMessage('');
    let characters = 0;
    function account(value: unknown) {
      characters += JSON.stringify(value).length;
      if (characters > EXPORT_LIMIT)
        throw new Error('The trace exceeds the 64 MiB browser export limit.');
    }
    async function readAll<T extends { seq: number }>(kind: string) {
      let after = 0;
      let page: Page<T>;
      const entries: T[] = [];
      do {
        page = await tracePage<T>(`${path}/${kind}`, after, controller.signal);
        controller.signal.throwIfAborted();
        account(page.entries);
        entries.push(...page.entries);
        after = cursorAfter(page, after);
      } while (page.next_cursor !== null);
      return { entries, completeness: page.completeness };
    }
    try {
      const [messages, activity] = await Promise.all([
        readAll<TranscriptEntry>('transcript'),
        readAll<Event>('events'),
      ]);
      const artifacts: Artifact[] = [];
      const missingArtifacts: { id: string; error: string }[] = [];
      const ids = new Set([
        ...activity.entries.flatMap((event) => artifactIds(event.data)),
        ...messages.entries.flatMap(({ message }) =>
          message.role === 'tool' ? artifactIds(objectOutput(message.content)) : [],
        ),
      ]);
      for (const id of ids) {
        let artifact: Artifact;
        try {
          artifact = await api<Artifact>(`/artifacts/${encodeURIComponent(id)}`, {
            signal: controller.signal,
          });
        } catch (e) {
          controller.signal.throwIfAborted();
          missingArtifacts.push({ id, error: errorText(e) });
          continue;
        }
        account(artifact);
        artifacts.push(artifact);
      }
      controller.signal.throwIfAborted();
      const safeId = agent.id.replace(/[^a-zA-Z0-9_-]/g, '_');
      downloadJson(
        `evo-trace-${safeId}.json`,
        JSON.stringify(
          {
            project_id: run.project_id,
            run_id: run.id,
            agent_id: agent.id,
            exported_at: new Date().toISOString(),
            completeness: messages.completeness,
            transcript: messages.entries,
            events: activity.entries,
            artifacts,
            missing_artifacts: missingArtifacts,
          },
          null,
          2,
        ),
      );
      setExportMessage(
        missingArtifacts.length
          ? `Trace downloaded with ${missingArtifacts.length} unavailable ${missingArtifacts.length === 1 ? 'artifact' : 'artifacts'}.`
          : 'Trace downloaded.',
      );
    } catch (e) {
      if (!controller.signal.aborted) {
        setExportError(true);
        setExportMessage(errorText(e));
      }
    } finally {
      if (exportRequest.current === controller) {
        exportRequest.current = null;
        controller.abort();
        setExporting(false);
      }
    }
  }

  function cancelExport() {
    exportRequest.current?.abort();
    setExportMessage('Download canceled.');
  }

  return (
    <dialog
      ref={dialog}
      className="agent-trace-dialog"
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <header className="trace-header">
        <div className="trace-identity">
          <span className="trace-agent-icon">
            <Bot size={22} />
          </span>
          <div>
            <h2 id={titleId}>{agent.name || agent.role}</h2>
            <p>
              {agent.role === 'qa' ? 'QA' : agent.role} <span>·</span> {run.title}
            </p>
          </div>
          <Status status={agent.status} />
        </div>
        <div className="trace-header-actions">
          {exporting ? (
            <button className="button trace-download" onClick={cancelExport}>
              <LoaderCircle size={15} className="spin" /> Cancel download
            </button>
          ) : (
            <button className="button trace-download" onClick={() => void exportTrace()}>
              <Download size={15} /> Download trace
            </button>
          )}
          <button
            autoFocus
            className="icon-button"
            aria-label="Close agent trace"
            onClick={onClose}
          >
            <X size={20} />
          </button>
        </div>
      </header>
      <div className="trace-toolbar">
        <nav className="trace-views" aria-label="Trace view">
          <button
            aria-pressed={view === 'transcript'}
            onClick={() => {
              setView('transcript');
              setFilter('all');
              scroll.current?.scrollTo(0, 0);
            }}
          >
            <MessagesSquare size={15} /> Transcript
          </button>
          <button
            aria-pressed={view === 'events'}
            onClick={() => {
              setView('events');
              setFilter('all');
              scroll.current?.scrollTo(0, 0);
            }}
          >
            <Activity size={15} /> Activity
          </button>
        </nav>
        <div className="trace-controls">
          <label className="trace-search">
            <Search size={15} aria-hidden="true" />
            <input
              type="search"
              aria-label="Search loaded history"
              placeholder="Search loaded history"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <select
            aria-label={view === 'transcript' ? 'Filter messages' : 'Filter activity'}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            <option value="all">{view === 'transcript' ? 'All messages' : 'All activity'}</option>
            {view === 'transcript'
              ? roles.map((role) => (
                  <option key={role} value={role}>
                    {ROLE_LABELS[role] || role}
                  </option>
                ))
              : kinds.map((kind) => (
                  <option key={kind} value={kind}>
                    {kind === 'tool' ? 'Tool calls' : kind}
                  </option>
                ))}
          </select>
          <button
            className="icon-button"
            aria-label="Refresh trace"
            title="Refresh trace"
            disabled={viewData.loading}
            onClick={() => viewData.request.current(true)}
          >
            <RefreshCw size={15} />
          </button>
        </div>
      </div>
      {transcript.completeness === 'legacy_partial' && (
        <p className="trace-notice">
          This older run has a partial history. Some messages and provider reasoning may be missing.
        </p>
      )}
      {exportMessage && (
        <p className="trace-notice" role={exportError ? 'alert' : 'status'}>
          {exportMessage}
        </p>
      )}
      <div ref={scroll} className="trace-scroll">
        {view === 'transcript'
          ? visibleMessages.map((entry) => <TranscriptRow key={entry.seq} entry={entry} />)
          : visibleActivity.map((item) =>
              'events' in item ? (
                <ActivityRow
                  key={item.key}
                  item={item}
                  parent={item.parent ? spans.get(item.parent) : undefined}
                />
              ) : (
                <ActivityRow key={item.id} item={item} />
              ),
            )}
        {!matches && !viewData.error && (
          <div className="trace-empty" role="status">
            {viewData.loading
              ? 'Loading agent history…'
              : query || filter !== 'all'
                ? 'No matches in the loaded history.'
                : 'No entries recorded yet.'}
          </div>
        )}
        {viewData.error && (
          <div className="trace-load-error" role="alert">
            <span>{viewData.error}</span>
            <button
              className="button"
              onClick={() => viewData.request.current(true)}
              disabled={viewData.loading}
            >
              Retry
            </button>
          </div>
        )}
        {viewData.hasMore && (
          <div className="trace-pagination">
            <button
              className="button"
              disabled={viewData.loading}
              onClick={() => viewData.request.current(true)}
            >
              {viewData.loading ? 'Loading…' : 'Load more history'}
            </button>
          </div>
        )}
      </div>
      <footer className="trace-footer">
        <span>
          {viewData.entries.length.toLocaleString()} {view === 'transcript' ? 'messages' : 'events'}{' '}
          loaded{viewData.hasMore && ' · More available'}
        </span>
        {(query || filter !== 'all') && (
          <span>
            {matches.toLocaleString()} {matches === 1 ? 'match' : 'matches'}
          </span>
        )}
      </footer>
    </dialog>
  );
}
