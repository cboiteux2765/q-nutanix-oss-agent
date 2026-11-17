import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  X,
  LoaderCircle,
  Check,
  AlertCircle,
  Circle,
  Pause,
  Terminal,
  FileText,
  MessageSquare,
  GitBranch,
  Bot,
  Layers3,
} from 'lucide-react';
import type { Event } from './types';

export function modelLabel(model: string) {
  if (model === 'gpt-6.1-terra') return 'GPT 6.1 Terra';
  if (model === 'gpt-6.1-sol') return 'GPT 6.1 Sol';
  return model;
}

export function Logo({ small = false }: { small?: boolean }) {
  return (
    <span className={`logo ${small ? 'small' : ''}`} aria-hidden="true">
      <svg viewBox="0 0 32 32">
        <path d="M8 7h17v4H13v4h10v4H13v4h12v4H8z" />
        <circle cx="25" cy="6" r="3" />
      </svg>
    </span>
  );
}
export function MarkdownText({ children }: { children: string }) {
  return (
    <div className="markdown">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {children}
      </Markdown>
    </div>
  );
}
export function Modal({
  title,
  subtitle,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    const element = ref.current;
    if (!element?.contains(document.activeElement))
      (element?.querySelector<HTMLElement>('input, textarea, select') || element)?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close.current();
      if (e.key === 'Tab') {
        const nodes = element?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input, select, textarea, [tabindex="0"]',
        );
        if (!nodes?.length) return;
        const first = nodes[0],
          last = nodes[nodes.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('keydown', key);
      before?.focus();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
        tabIndex={-1}
        className={`modal ${wide ? 'wide' : ''}`}
      >
        <div className="modal-heading">
          <div>
            <h2 id="modal-title">{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close dialog">
            <X size={19} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
export function Status({ status, compact = false }: { status: string; compact?: boolean }) {
  const state = status?.toLowerCase() || 'idle';
  const active = ['running', 'working', 'thinking', 'starting', 'queued', 'compacting'].includes(
    state,
  );
  const done = ['completed', 'complete', 'ready', 'done', 'accepted', 'active'].includes(state);
  const bad = ['failed', 'error', 'blocked'].includes(state);
  const paused = ['cancelled', 'canceled', 'interrupted', 'paused'].includes(state);
  const Icon = active ? LoaderCircle : done ? Check : bad ? AlertCircle : paused ? Pause : Circle;
  return (
    <span
      className={`status ${active ? 'working' : done ? 'success' : bad ? 'danger' : ''} ${compact ? 'compact' : ''}`}
    >
      <Icon size={compact ? 11 : 12} className={active ? 'spin' : ''} />
      {!compact && state.replaceAll('_', ' ')}
    </span>
  );
}
export function Empty({
  icon,
  title,
  children,
}: {
  icon: ReactNode;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon">{icon}</div>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}
export function timeLabel(date?: string) {
  if (!date) return '';
  const d = new Date(date);
  return Number.isNaN(d.getTime())
    ? ''
    : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}
export function relativeDate(date?: string) {
  if (!date) return '';
  const delta = Date.now() - new Date(date).getTime();
  if (delta < 60000) return 'Just now';
  if (delta < 3600000) return `${Math.floor(delta / 60000)}m ago`;
  if (delta < 86400000) return `${Math.floor(delta / 3600000)}h ago`;
  return new Date(date).toLocaleDateString([], { month: 'short', day: 'numeric' });
}
export function eventLabel(event: Event) {
  const d = event.data;
  if (event.kind === 'message')
    return String(d.role === 'user' ? 'You sent a message' : d.text || 'Agent replied').slice(
      0,
      110,
    );
  if (event.kind === 'tool.started') return `Using ${d.name || d.tool || 'Python'}`;
  if (event.kind === 'tool.finished') return `${d.name || d.tool || 'Tool'} completed`;
  if (event.kind === 'agent.created') return `${d.name || d.role || 'Agent'} joined`;
  if (event.kind === 'agent.status') return `${d.status || 'Status updated'}`;
  if (event.kind === 'usage') return 'Model response received';
  if (event.kind === 'compaction') return 'Context compacted · originals retained';
  return String(d.message || d.text || d.summary || event.kind.replaceAll('.', ' · ')).slice(
    0,
    130,
  );
}
export function EventIcon({ kind }: { kind: string }) {
  const Icon = kind.startsWith('tool')
    ? Terminal
    : kind.startsWith('agent')
      ? Bot
      : kind.startsWith('workflow')
        ? GitBranch
        : kind.startsWith('artifact')
          ? FileText
          : kind.startsWith('compaction')
            ? Layers3
            : MessageSquare;
  return <Icon size={14} />;
}
export function jsonText(value: unknown) {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}
export type ToastMessage = { text: string; error: boolean };
export function Toast({ toast, onDismiss }: { toast: ToastMessage; onDismiss: () => void }) {
  return (
    <div className={`toast ${toast.error ? 'error' : ''}`} role={toast.error ? 'alert' : 'status'}>
      {toast.error ? <AlertCircle size={17} /> : <Check size={17} />}
      <span>{toast.text}</span>
      <button className="icon-button" aria-label="Dismiss notification" onClick={onDismiss}>
        <X size={14} />
      </button>
    </div>
  );
}
