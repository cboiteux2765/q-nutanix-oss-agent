import type { RefObject } from 'react';
import {
  AlertCircle,
  ArrowRight,
  ArrowUp,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Flag,
  FolderGit2,
  LoaderCircle,
  PanelRightOpen,
  Plus,
  RefreshCw,
  Square,
} from 'lucide-react';
import { MarkdownText, modelLabel, Status, timeLabel } from './components';
import {
  ACTIVE_STATES,
  RESUMABLE_STATES,
  type Agent,
  type Event,
  type Project,
  type Run,
  type Settings,
} from './types';

const STARTERS = [
  {
    label: 'Explore the repo',
    text: 'Explore this repository and explain its architecture and key modules. Do not change any files yet.',
  },
  {
    label: 'Plan a feature',
    text: 'Help me plan a new feature. Ask me what I want to build before making changes.',
  },
  {
    label: 'Work through a bug',
    text: 'Help me investigate a bug. Ask me for the issue and reproduction details first.',
  },
];

/** The empty conversation: a greeting and suggested first messages. */
export function NewConversation({
  project,
  onStarter,
  onSetup,
}: {
  project: Project | null;
  onStarter: (text: string) => void;
  onSetup: () => void;
}) {
  return (
    <div className="new-conversation">
      <div className="setup-message">
        <span className="bubble-sender">EVO</span>
        <div className="chat-bubble">
          {project
            ? 'A feature, a stubborn bug, or an idea you’re still figuring out. Where should we start?'
            : 'When you’re ready, I’ll help you connect a repository and choose our first task.'}
        </div>
      </div>
      <div className="quick-replies">
        {project ? (
          STARTERS.map((starter) => (
            <button key={starter.label} className="reply" onClick={() => onStarter(starter.text)}>
              {starter.label}
            </button>
          ))
        ) : (
          <button className="reply primary-reply" onClick={onSetup}>
            Set up a project
            <ArrowRight size={15} />
          </button>
        )}
      </div>
    </div>
  );
}

function phaseLabel(phase?: string) {
  if (phase === 'ready') return 'Ready for review';
  if (phase === 'qa') return 'QA';
  return (phase || 'triage').replace(/_/g, ' ');
}

/** The central conversation with the main agent, and the open run's state below it. */
export function MessageList({
  run,
  agents,
  messages,
  hasMore,
  onEarlier,
  streaming,
  draft,
  delivered,
  active,
  mainName,
  onShowWork,
  onResume,
}: {
  run: Run | null;
  agents: Agent[];
  messages: Event[];
  hasMore: boolean;
  onEarlier: () => void;
  streaming: string;
  draft: string;
  delivered: Set<string>;
  active: boolean;
  mainName: string;
  onShowWork: () => void;
  onResume: () => void;
}) {
  const lastUserSeq = Math.max(
    0,
    ...messages.filter((m) => m.data.role === 'user').map((m) => m.seq),
  );
  const teamWorking = agents.some(
    (a) => a.role !== 'orchestrator' && ACTIVE_STATES.includes(a.status),
  );
  return (
    <div className="message-list">
      <div className="chat-date">
        {new Date(run?.created_at || messages[0]?.created_at || Date.now()).toLocaleDateString([], {
          weekday: 'short',
          month: 'short',
          day: 'numeric',
        })}
      </div>
      {hasMore && (
        <button className="text-button earlier-messages" onClick={onEarlier}>
          Earlier messages
        </button>
      )}
      {messages.map((e) => {
        const text = String(e.data.text || e.data.content || '');
        if (e.data.channel === 'update') {
          const notice = String(e.data.notice || 'progress');
          return (
            <div className={`update-line ${notice}`} key={e.seq}>
              {notice === 'question' ? (
                <CircleHelp size={13} />
              ) : notice === 'blocker' ? (
                <AlertCircle size={13} />
              ) : (
                <Flag size={13} />
              )}
              <MarkdownText>{text}</MarkdownText>
              <time>{timeLabel(e.created_at)}</time>
            </div>
          );
        }
        const user = e.data.role === 'user';
        const status =
          user && run && e.run_id === run.id && e.seq === lastUserSeq
            ? delivered.has(e.id)
              ? `Read by ${mainName}`
              : active
                ? `Received · ${mainName} will read it at its next step`
                : ''
            : '';
        return (
          <article className={`message ${user ? 'user-message' : 'assistant-message'}`} key={e.seq}>
            <div className="message-body">
              <div className="message-heading">
                <strong>{user ? 'You' : mainName}</strong>
                {Boolean(e.data.responder) && (
                  <span
                    className="message-tag"
                    title="Answered from the recorded state while the work continues"
                  >
                    while working
                  </span>
                )}
                <time>{timeLabel(e.created_at)}</time>
              </div>
              <div className="message-bubble">
                <MarkdownText>{text}</MarkdownText>
              </div>
              {status && <div className="delivery-status">{status}</div>}
            </div>
          </article>
        );
      })}
      {streaming && (
        <article className="message assistant-message">
          <div className="message-body">
            <div className="message-heading">
              <strong>{mainName}</strong>
            </div>
            <div className="message-bubble">
              <MarkdownText>{streaming}</MarkdownText>
              <span className="stream-cursor" />
            </div>
          </div>
        </article>
      )}
      {draft && !streaming && (
        <div
          className="draft-preview"
          aria-live="off"
          title="Working text; it becomes a message only if it is the reply"
        >
          <MarkdownText>{draft}</MarkdownText>
        </div>
      )}
      {active && !streaming && !draft && (
        <div className="thinking-row" role="status">
          <span className="thinking-dots">
            <i />
            <i />
            <i />
          </span>
          <span>{teamWorking ? 'Working with the team' : `${mainName} is thinking`}</span>
        </div>
      )}
      {run && agents.length > 0 && (
        <button className="inline-work" onClick={onShowWork}>
          <span className="work-card-icon">
            <PanelRightOpen size={18} />
          </span>
          <span>
            <strong>
              {active
                ? 'Working on it'
                : run.status === 'completed'
                  ? 'Work completed'
                  : 'View the work'}
            </strong>
            <small>
              {phaseLabel(run.phase)} · {agents.length} {agents.length === 1 ? 'agent' : 'agents'}
            </small>
          </span>
          <Status status={run.status} compact />
          <ChevronRight size={15} />
        </button>
      )}
      {run?.error && (
        <div className="run-error">
          <AlertCircle size={16} />
          <div>
            <strong>This run needs attention.</strong>
            <p>{run.error}</p>
          </div>
        </div>
      )}
      {run && RESUMABLE_STATES.includes(run.status) && (
        <div className="resume-row">
          <span>Everything so far is saved.</span>
          <button className="button secondary small" onClick={onResume}>
            <RefreshCw size={13} />
            Resume work
          </button>
        </div>
      )}
    </div>
  );
}

/** The message box, with the repository, model and assignment controls above it. */
export function Composer({
  inputRef,
  project,
  settings,
  run,
  runs,
  active,
  mainName,
  draft,
  setDraft,
  submitting,
  disabled,
  newAssignment,
  setNewAssignment,
  onSend,
  onStop,
  onRepository,
  onModel,
  onViewRun,
}: {
  inputRef: RefObject<HTMLTextAreaElement | null>;
  project: Project | null;
  settings: Settings | null;
  run: Run | null;
  runs: Run[];
  active: boolean;
  mainName: string;
  draft: string;
  setDraft: (text: string) => void;
  submitting: boolean;
  disabled: boolean;
  newAssignment: boolean;
  setNewAssignment: (value: boolean) => void;
  onSend: () => void;
  onStop: () => void;
  onRepository: () => void;
  onModel: () => void;
  onViewRun: (id: string) => void;
}) {
  return (
    <div className="composer-area">
      <div className="composer-context-row">
        <button
          className="composer-context"
          title={project?.path || 'Connect a repository'}
          aria-label={project ? 'Change repository' : 'Connect a repository'}
          onClick={onRepository}
        >
          <FolderGit2 size={14} />
          <span>{project?.name || 'Connect a repository'}</span>
          <ChevronDown size={11} />
        </button>
        {settings && (
          <button className="model-choice" onClick={onModel}>
            <span>{modelLabel(project?.main_agent?.model || settings.model)}</span>
            <ChevronDown size={12} />
          </button>
        )}
      </div>
      {run && !active && (
        <div className="assignment-options">
          <button
            className={newAssignment ? 'selected' : ''}
            onClick={() => setNewAssignment(!newAssignment)}
            title="Start a fresh assignment from the repository's current files, keeping this conversation"
          >
            <Plus size={12} />
            {newAssignment ? 'New assignment selected' : 'New assignment'}
          </button>
          {runs.length > 1 && (
            <select
              aria-label="View assignment"
              value={run.id}
              onChange={(e) => onViewRun(e.target.value)}
            >
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.title}
                </option>
              ))}
            </select>
          )}
        </div>
      )}
      <div className={`composer ${submitting ? 'sending' : ''}`}>
        <textarea
          ref={inputRef}
          rows={1}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            e.target.style.height = 'auto';
            e.target.style.height = `${Math.min(e.target.scrollHeight, 180)}px`;
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              onSend();
            }
          }}
          placeholder={
            active
              ? 'Add a thought or steer the work…'
              : project
                ? `Message ${mainName}…`
                : 'Set up a project to start chatting…'
          }
          aria-label={`Message ${mainName}`}
          title="Enter to send · Shift + Enter for a new line"
          disabled={disabled}
        />
        <div className="composer-controls">
          {active && (
            <button
              className="stop-button"
              onClick={onStop}
              title="Stop work"
              aria-label="Stop work"
            >
              <Square size={13} fill="currentColor" />
            </button>
          )}
          <button
            className="send-button"
            onClick={onSend}
            disabled={!project || !draft.trim() || submitting}
            title="Send message"
            aria-label="Send message"
          >
            {submitting ? <LoaderCircle size={18} className="spin" /> : <ArrowUp size={20} />}
          </button>
        </div>
      </div>
    </div>
  );
}
