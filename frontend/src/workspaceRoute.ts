import { useCallback, useEffect, useRef, useState } from 'react';
import type { View } from './Sidebar';

type WorkspaceRoute = {
  projectId: string | null;
  view: View;
  runId: string | null;
  agentId: string | null;
  trace: boolean;
};

const views = new Set<View>(['workspace', 'conversation', 'memory', 'skills', 'evolution', 'spec']);

function readRoute(): WorkspaceRoute {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const view = params.get('view') as View;
  const runId = params.get('run');
  const agentId = runId ? params.get('agent') : null;
  return {
    view: views.has(view) ? view : 'workspace',
    projectId: params.get('project'),
    runId,
    agentId,
    trace: !!agentId && params.get('trace') === '1',
  };
}

export function useWorkspaceRoute() {
  const [route, setRoute] = useState(readRoute);
  const current = useRef(route);
  useEffect(() => {
    const restore = () => {
      current.current = readRoute();
      setRoute(current.current);
    };
    window.addEventListener('popstate', restore);
    window.addEventListener('hashchange', restore);
    return () => {
      window.removeEventListener('popstate', restore);
      window.removeEventListener('hashchange', restore);
    };
  }, []);
  const navigate = useCallback((change: Partial<WorkspaceRoute>, replace = false) => {
    const next = { ...current.current, ...change };
    const params = new URLSearchParams();
    if (next.projectId) params.set('project', next.projectId);
    params.set('view', next.view);
    if (next.runId) params.set('run', next.runId);
    if (next.runId && next.agentId) {
      params.set('agent', next.agentId);
      if (next.trace) params.set('trace', '1');
    }
    const url = new URL(window.location.href);
    url.hash = params.toString();
    if (url.href === window.location.href) return;
    if (replace) window.history.replaceState(null, '', url);
    else window.history.pushState(null, '', url);
    current.current = next;
    setRoute(next);
  }, []);
  return { route, navigate };
}
