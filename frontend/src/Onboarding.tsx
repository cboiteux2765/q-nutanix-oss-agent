import { useEffect, useRef, useState } from 'react';
import {
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronRight,
  FolderGit2,
  KeyRound,
  LoaderCircle,
} from 'lucide-react';
import { errorText, post } from './api';
import { modelLabel } from './components';
import type { Project, Settings } from './types';

type Step = 'invite' | 'repository' | 'model' | 'task';
const steps: Step[] = ['invite', 'repository', 'model', 'task'];
const starters = [
  {
    label: 'Understand the repo',
    text: 'Explore this repository and explain its architecture, key modules, and where to start. Do not change any files yet.',
  },
  {
    label: 'Build a feature',
    text: 'Help me plan a new feature. First, ask me what I want to build and clarify the requirements before making changes.',
  },
  {
    label: 'Fix a bug',
    text: 'Help me investigate a bug. First, ask me for the issue and reproduction details before making changes.',
  },
];

export default function Onboarding({
  project,
  settings,
  onProject,
  onSettings,
  onComplete,
  onLater,
}: {
  project: Project | null;
  settings: Settings | null;
  onProject: (project: Project) => void;
  onSettings: () => void;
  onComplete: (message: string) => void;
  onLater: () => void;
}) {
  const [step, setStep] = useState<Step>('invite');
  const [newRepo, setNewRepo] = useState(false);
  const [path, setPath] = useState('');
  const [name, setName] = useState('');
  const [task, setTask] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const current = steps.indexOf(step);
  const latest = useRef<HTMLDivElement>(null);
  const connectedHere = useRef<string | null>(null);
  useEffect(() => {
    if (step !== 'invite') latest.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [step]);
  // A project can also be changed from the sidebar while setup is open.
  useEffect(() => {
    if (current > 1 && project?.id !== connectedHere.current) setStep('repository');
    setNewRepo(false);
    connectedHere.current = null;
  }, [project?.id]);

  async function connect(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const cleanPath = path.trim().replace(/^["']|["']$/g, '');
      const next = await post<Project>('/projects', {
        name:
          name.trim() ||
          cleanPath
            .replace(/[\\/]+$/, '')
            .split(/[\\/]/)
            .pop() ||
          'My project',
        path: cleanPath,
        description: '',
      });
      connectedHere.current = next.id;
      onProject(next);
      setNewRepo(false);
      setStep('model');
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="setup-chat">
      <div className="setup-message">
        <span className="bubble-sender">EVO</span>
        <div className="chat-bubble">
          Hey! I’m EVO, your project partner. Tell me what you want to build, and I’ll coordinate
          the planning, coding, and review.
        </div>
        <div className="chat-bubble">Ready to set up a project together?</div>
        {step === 'invite' && (
          <div className="quick-replies">
            <button className="reply primary-reply" onClick={() => setStep('repository')}>
              Let’s get started <ArrowRight size={15} />
            </button>
            <button className="reply" onClick={onLater}>
              I’ll look around first
            </button>
          </div>
        )}
      </div>
      {current > 0 && (
        <>
          <div className="setup-reply">
            <span>Let’s get started.</span>
          </div>
          <div className="setup-message">
            <div className="chat-bubble">
              Which repository should we work on? Choose a folder on the machine running EVO.
            </div>
            {current > 1 && project && (
              <div className="setup-answer">
                <FolderGit2 size={16} />
                <span>
                  <strong>{project.name}</strong>
                  <small>{project.path}</small>
                </span>
                <Check size={15} />
              </div>
            )}
          </div>
        </>
      )}
      {current > 1 && (
        <div className="setup-message">
          <div className="chat-bubble">
            Next, let’s choose the model I’ll use. You can change this any time in Settings.
          </div>
          {current > 2 && settings && (
            <div className="setup-answer">
              <Check size={16} />
              <span>{modelLabel(settings.model)}</span>
            </div>
          )}
        </div>
      )}
      {current > 2 && (
        <div className="setup-message">
          <div className="chat-bubble">
            All set. What would you like to start with? Pick an idea below or write your own.
          </div>
        </div>
      )}

      {current > 0 && (
        <div ref={latest} className="setup-interaction" key={step}>
          <div className="setup-step">
            <span>
              {current} of 3 ·{' '}
              {step === 'repository'
                ? 'Your repository'
                : step === 'model'
                  ? 'Your model'
                  : 'Your first message'}
            </span>
            <button
              onClick={() => {
                setStep(steps[current - 1]);
                setError('');
              }}
              disabled={busy}
            >
              <ArrowLeft size={13} /> Back
            </button>
          </div>
          {step === 'repository' && (
            <>
              {project && !newRepo ? (
                <>
                  <button className="repo-choice" onClick={() => setStep('model')}>
                    <span className="repo-choice-icon">
                      <FolderGit2 size={20} />
                    </span>
                    <span>
                      <strong>{project.name}</strong>
                      <small>{project.path}</small>
                    </span>
                    <ChevronRight size={17} />
                  </button>
                  <button className="text-button" onClick={() => setNewRepo(true)}>
                    Connect a different repository
                  </button>
                </>
              ) : (
                <form className="setup-form" onSubmit={connect}>
                  <label>
                    Repository path
                    <input
                      autoFocus
                      required
                      placeholder="D:\projects\my-project"
                      value={path}
                      onChange={(e) => setPath(e.target.value)}
                      spellCheck={false}
                      disabled={busy}
                    />
                  </label>
                  <label>
                    Project name <span className="optional">optional</span>
                    <input
                      placeholder="Use the folder name"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      disabled={busy}
                    />
                  </label>
                  <p className="field-help">
                    Already cloned locally? Paste its full folder path. Each task gets its own
                    working copy.
                  </p>
                  {error && (
                    <div className="form-error" role="alert">
                      {error}
                    </div>
                  )}
                  <div className="setup-actions">
                    {project && (
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => setNewRepo(false)}
                      >
                        Use existing project
                      </button>
                    )}
                    <button
                      type="submit"
                      className="button primary"
                      disabled={busy || !path.trim()}
                    >
                      {busy ? (
                        <LoaderCircle size={15} className="spin" />
                      ) : (
                        <ArrowRight size={15} />
                      )}
                      Connect repository
                    </button>
                  </div>
                </form>
              )}
            </>
          )}
          {step === 'model' && (
            <>
              <button className="repo-choice" onClick={onSettings}>
                <span className="repo-choice-icon">
                  <KeyRound size={20} />
                </span>
                <span>
                  <strong>{modelLabel(settings?.model || 'gpt-6.1-terra')}</strong>
                  <small>
                    {settings?.api_style === 'chat_completions'
                      ? 'Chat Completions'
                      : 'Responses API'}{' '}
                    ·{' '}
                    {settings?.api_key_configured
                      ? 'API key available'
                      : 'Add an API key to continue'}
                  </small>
                </span>
                <span className="choice-action">Change</span>
              </button>
              <p className="field-help setup-hint">
                {settings?.api_key_configured
                  ? 'Model requests start when you send your first message.'
                  : 'Open Settings to add your key. OpenAI and compatible providers are supported.'}
              </p>
              <div className="setup-actions">
                <button
                  className="button primary"
                  onClick={() => (settings?.api_key_configured ? setStep('task') : onSettings())}
                >
                  {settings?.api_key_configured ? 'Continue' : 'Set up model'}
                  <ArrowRight size={15} />
                </button>
              </div>
            </>
          )}
          {step === 'task' && (
            <form
              className="setup-form"
              onSubmit={(e) => {
                e.preventDefault();
                if (task.trim() && project && settings?.api_key_configured) onComplete(task.trim());
              }}
            >
              <div className="task-options">
                {starters.map((s) => (
                  <button
                    type="button"
                    key={s.label}
                    className={`reply ${task === s.text ? 'chosen' : ''}`}
                    onClick={() => setTask(s.text)}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
              <label className="sr-only" htmlFor="first-task">
                Your first message
              </label>
              <textarea
                id="first-task"
                autoFocus
                rows={4}
                value={task}
                onChange={(e) => setTask(e.target.value)}
                placeholder="I’d like to…"
              />
              <p className="field-help">
                I’ll put this in the chat. You can edit it before you send.
              </p>
              <div className="setup-actions">
                <button
                  type="submit"
                  className="button primary"
                  disabled={!task.trim() || !project || !settings?.api_key_configured}
                >
                  Continue to chat
                  <ArrowRight size={15} />
                </button>
              </div>
            </form>
          )}
        </div>
      )}
    </div>
  );
}
