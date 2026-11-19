import { useCallback, useEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import type { View } from './Sidebar';

type WorkspaceRoute = {
  projectId: string | null;
  view: View;
  runId: string | null;
  agentId: string | null;
  trace: boolean;
};

const views = new Set<View>(['workspace', 'conversation', 'memory', 'skills', 'evolution', 'spec']);
const composerViews = new Set<View>(['workspace', 'conversation']);

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
  const activeTransition = useRef<ViewTransition | null>(null);

  const updateRoute = useCallback((next: WorkspaceRoute, animate = true) => {
    const previous = current.current;
    if (
      previous.view === next.view &&
      previous.projectId === next.projectId &&
      previous.runId === next.runId &&
      previous.agentId === next.agentId &&
      previous.trace === next.trace
    )
      return;

    current.current = next;
    const running = activeTransition.current;
    if (running) {
      running.skipTransition();
      setRoute(next);
      return;
    }

    const shareComposer =
      animate &&
      !!next.projectId &&
      previous.projectId === next.projectId &&
      !previous.runId &&
      !next.runId &&
      previous.view !== next.view &&
      composerViews.has(previous.view) &&
      composerViews.has(next.view);

    if (
      !shareComposer ||
      !document.startViewTransition ||
      window.matchMedia('(prefers-reduced-motion: reduce)').matches ||
      !document.querySelector('.workspace-body .composer')
    ) {
      setRoute(next);
      return;
    }

    const root = document.documentElement;
    root.classList.add('workspace-transition');
    const transition = document.startViewTransition(() => {
      // The browser captures the destination as soon as this callback returns.
      if (current.current === next) flushSync(() => setRoute(next));
    });
    activeTransition.current = transition;
    const finish = () => {
      activeTransition.current = null;
      root.classList.remove('workspace-transition');
    };
    void transition.ready.catch(() => {});
    void transition.finished.then(finish, finish);
  }, []);

  useEffect(() => {
    const restore = () => updateRoute(readRoute());
    window.addEventListener('popstate', restore);
    window.addEventListener('hashchange', restore);
    return () => {
      window.removeEventListener('popstate', restore);
      window.removeEventListener('hashchange', restore);
      activeTransition.current?.skipTransition();
    };
  }, [updateRoute]);
  const navigate = useCallback(
    (change: Partial<WorkspaceRoute>, { replace = false, animate = true } = {}) => {
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
      updateRoute(next, animate && !replace);
    },
    [updateRoute],
  );
  return { route, navigate };
}
