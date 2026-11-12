import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorText, mergeBySeq } from './api';
import type { Event, RunDetail } from './types';

// Events that change what the run detail shows (statuses, agents, phase, usage).
const DETAIL_EVENTS = new Set([
  'agent.created',
  'agent.status',
  'run.status',
  'workflow',
  'usage',
  'run.completed',
  'run.failed',
  'message',
]);

/**
 * The open run: its detail, its event log (kept live over the event stream) and the main
 * agent's streaming reply.
 */
export function useRun(notify: (text: string, error?: boolean) => void) {
  const [detail, setDetail] = useState<RunDetail | null>(null),
    [events, setEvents] = useState<Event[]>([]),
    [streaming, setStreaming] = useState('');
  const currentRun = useRef<string | null>(null),
    requestCounter = useRef(0);

  /** Whether this run is still the one on screen (check after an await). */
  const isOpen = useCallback((id: string) => currentRun.current === id, []);

  /** Forget the open run and ignore any load still in flight. */
  const clearRun = useCallback(() => {
    requestCounter.current++;
    currentRun.current = null;
    setDetail(null);
    setEvents([]);
    setStreaming('');
  }, []);

  /** Open a run. Resolves true once it is shown, false if it failed or was superseded. */
  const loadRun = useCallback(
    async (id: string) => {
      const token = ++requestCounter.current;
      try {
        const result = await api<RunDetail>(`/runs/${id}`);
        if (token !== requestCounter.current) return false;
        const sameRun = currentRun.current === id;
        currentRun.current = id;
        setDetail(result);
        setEvents((old) => (sameRun ? mergeBySeq(old, result.events || []) : result.events || []));
        setStreaming('');
        return true;
      } catch (e) {
        // A superseded load (the user moved on) fails silently, like a superseded success.
        if (token === requestCounter.current) notify(errorText(e), true);
        return false;
      }
    },
    [notify],
  );

  const run = detail?.run || null;
  const runId = run?.id;
  const rootAgentId = run?.root_agent_id;
  useEffect(() => {
    if (!runId) return;
    const id = runId;
    let closed = false;
    let refresh: ReturnType<typeof setTimeout> | undefined;
    const last = Math.max(0, ...events.map((e) => e.seq));
    const source = new EventSource(`/api/runs/${id}/events?after=${last}`);
    // New events arrive on the stream, so the refresh only needs the run and its agents.
    const refreshDetail = () => {
      clearTimeout(refresh);
      refresh = setTimeout(() => {
        api<RunDetail>(`/runs/${id}?events=false`)
          .then((d) => {
            if (!closed && currentRun.current === id) setDetail(d);
          })
          .catch(() => {});
      }, 400);
    };
    source.onmessage = (e) => {
      if (closed || currentRun.current !== id) return;
      try {
        const event = JSON.parse(e.data) as Event;
        if (!event.seq) return;
        setEvents((old) => (old.some((x) => x.seq === event.seq) ? old : [...old, event]));
        const fromRoot = !event.agent_id || event.agent_id === rootAgentId;
        if (event.kind === 'agent.delta' && fromRoot)
          setStreaming((old) => old + String(event.data.text || ''));
        if (
          (event.kind === 'message' &&
            event.data.role === 'assistant' &&
            event.data.visible !== false) ||
          (event.kind === 'agent.response' && event.agent_id === rootAgentId)
        )
          setStreaming('');
        if (DETAIL_EVENTS.has(event.kind) || event.kind.startsWith('workflow.')) refreshDetail();
      } catch {
        /* server heartbeats do not contain events */
      }
    };
    const poll = setInterval(() => {
      if (currentRun.current === id) refreshDetail();
    }, 5000);
    return () => {
      closed = true;
      source.close();
      clearTimeout(refresh);
      clearInterval(poll);
    };
    // Restart the event stream only when the open run changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  return { detail, setDetail, run, events, streaming, loadRun, clearRun, isOpen };
}
