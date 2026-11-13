import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  FlaskConical,
  LoaderCircle,
  Play,
  RotateCcw,
  Settings2,
  Sparkles,
  Square,
  X,
} from 'lucide-react';
import { api, arrayOf, errorText, post, put } from './api';
import { MarkdownText, Modal, Status, relativeDate } from './components';
import type { Project, Prompt, Run } from './types';

type Props = { project: Project; notify: (text: string, error?: boolean) => void };
type Split = 'train' | 'validation' | 'heldout' | 'skip';
type Task = {
  id: string;
  title: string;
  split: Split;
  issue: number;
  built: boolean;
  error?: string;
};
type Limits = Record<string, number>;
type EvolutionSettings = {
  mode: 'manual' | 'auto';
  auto_activate: boolean;
  min_queued_runs: number;
  max_auto_per_day: number;
  role: string;
  limits: Limits;
};
type QueueItem = { id: string; run_id: string; reason: string; title?: string; created_at: string };
type Gate = {
  passed: boolean;
  improved: boolean;
  reasons: string[];
  regressions: string[];
  // Absent when no candidate beat the baseline on validation, so nothing was evaluated held-out.
  heldout?: Record<string, number | null>;
  paired?: { both: number; baseline_only: number; candidate_only: number; neither: number } | null;
};
type Rollout = {
  id: string;
  task_id: string;
  task_title?: string;
  phase: string;
  candidate_label: string;
  outcome: string;
  score: number;
  resolved: boolean;
  tokens: number;
  cost_usd: number;
  latency_s: number;
  steps?: number;
  fail_to_pass?: [number, number];
  pass_to_pass?: [number, number];
  diff?: string;
  commands?: string[];
  final_message?: string;
  error?: string;
  leakage?: unknown[];
  reused_from?: string;
};
type Experiment = {
  id: string;
  role: string;
  kind: string;
  origin: string;
  objective: string;
  status: string;
  stage: string;
  created_at: string;
  source_run_ids: string[];
  splits: Record<string, string[]>;
  limits: Limits;
  spent: {
    tokens: number;
    cost_usd: number;
    rollouts: number;
    reflection_calls: number;
    search_tokens?: number;
    heldout_tokens?: number;
    unresolved_tokens?: number;
    unresolved_cost_usd?: number;
  };
  budget?: {
    total_tokens: number;
    search_tokens: number;
    heldout_reserve_tokens: number;
    per_rollout_tokens: number;
    total_usd?: number | null;
    not_covered: string;
  };
  optimizer: string;
  model: Record<string, string>;
  baseline_version?: number;
  error?: string;
  note?: string;
  log?: string[];
  proposals?: { at: string; text: string }[];
  gepa?: {
    candidates: {
      index: number;
      validation_score: number;
      prompt_id?: string;
      parents: number[];
    }[];
    best_index: number;
  };
  summary?: {
    gate: Gate;
    candidate_prompt_id: string | null;
    metrics: Record<string, Record<string, Record<string, unknown>>>;
  };
  rollouts?: Rollout[];
  candidates?: (Prompt & Record<string, unknown>)[];
};
type Overview = {
  settings: EvolutionSettings;
  queue: QueueItem[];
  experiments: Experiment[];
  tasks: Task[];
  environment: { replay_data: boolean; task_python: boolean };
  exposure: Record<string, { optimization: string[]; heldout: string[] }>;
};
type PromptRecord = Prompt & {
  activation_label?: string;
  gate?: Gate;
  experiment_id?: string;
  validation_score?: number;
  source_run_ids?: string[];
  source_event_ids?: string[];
  parent_prompt_ids?: (string | null)[];
};

const RUNNING = ['queued', 'running'];
const LIMIT_FIELDS: [string, string, number][] = [
  ['max_metric_calls', 'Search rollouts', 1],
  ['max_iterations', 'GEPA proposals', 1],
  ['reflection_minibatch_size', 'Reflection minibatch', 1],
  ['rollout_tokens', 'Tokens per rollout', 1],
  ['max_steps', 'Agent steps per rollout', 1],
  ['experiment_tokens', 'Total token budget', 1],
  ['rollout_seconds', 'Minutes per rollout', 60],
  ['wall_seconds', 'Hours in total', 3600],
  ['max_cost_usd', 'Spending limit (USD)', 1],
  ['rollout_cost_usd', 'Reserve per held-out rollout (USD)', 1],
];
const SPLIT_LABEL: Record<Split, string> = {
  train: 'Reflection',
  validation: 'Validation',
  heldout: 'Held-out',
  skip: 'Skip',
};

/** A small line diff (longest common subsequence), good enough for prompt versions. */
function lineDiff(before: string, after: string) {
  const a = before.split('\n'),
    b = after.split('\n');
  const table = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      table[i][j] =
        a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
  const out: { kind: ' ' | '+' | '-'; text: string }[] = [];
  let i = 0,
    j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ kind: ' ', text: a[i] });
      i++;
      j++;
    } else if (table[i + 1][j] >= table[i][j + 1]) out.push({ kind: '-', text: a[i++] });
    else out.push({ kind: '+', text: b[j++] });
  }
  while (i < a.length) out.push({ kind: '-', text: a[i++] });
  while (j < b.length) out.push({ kind: '+', text: b[j++] });
  return out;
}

function Diff({ before, after }: { before: string; after: string }) {
  return (
    <pre className="prompt-diff">
      {lineDiff(before, after).map((line, index) => (
        <div
          key={index}
          className={line.kind === '+' ? 'added' : line.kind === '-' ? 'removed' : ''}
        >
          {line.kind} {line.text}
        </div>
      ))}
    </pre>
  );
}

const fmt = (n?: number | null) => (n == null ? '—' : n.toLocaleString());
const pct = (n?: number | null) => (n == null ? '—' : `${Math.round(n * 100)}%`);

export function EvolutionView({ project, notify }: Props) {
  const [overview, setOverview] = useState<Overview | null>(null),
    [runs, setRuns] = useState<Run[]>([]),
    [prompts, setPrompts] = useState<PromptRecord[]>([]),
    [selectedRuns, setSelectedRuns] = useState<string[]>([]),
    [objective, setObjective] = useState(''),
    [splits, setSplits] = useState<Record<string, Split>>({}),
    [limits, setLimits] = useState<Limits>({}),
    [showLimits, setShowLimits] = useState(false),
    [busy, setBusy] = useState(false),
    [openExperiment, setOpenExperiment] = useState<Experiment | null>(null),
    [openPrompt, setOpenPrompt] = useState<PromptRecord | null>(null),
    [settingsOpen, setSettingsOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const [data, runList, promptList] = await Promise.all([
        api<Overview>(`/projects/${project.id}/evolution`),
        api<unknown>(`/projects/${project.id}/runs`),
        api<unknown>(`/projects/${project.id}/prompts`),
      ]);
      setOverview(data);
      setRuns(arrayOf<Run>(runList, 'runs'));
      setPrompts(arrayOf<PromptRecord>(promptList, 'prompts'));
      setSplits((old) =>
        Object.keys(old).length ? old : Object.fromEntries(data.tasks.map((t) => [t.id, t.split])),
      );
      setLimits((old) => (Object.keys(old).length ? old : data.settings.limits));
    } catch (e) {
      notify(errorText(e), true);
    }
  }, [project.id, notify]);
  useEffect(() => {
    setOverview(null);
    setSplits({});
    setLimits({});
    setSelectedRuns([]);
    setOpenExperiment(null);
    void load();
  }, [load]);

  const running = overview?.experiments.some((e) => RUNNING.includes(e.status));
  const openId = openExperiment?.id;
  useEffect(() => {
    if (!running && !(openExperiment && RUNNING.includes(openExperiment.status))) return;
    const timer = setInterval(async () => {
      void load();
      if (openId)
        try {
          setOpenExperiment(await api<Experiment>(`/projects/${project.id}/experiments/${openId}`));
        } catch {
          /* keep the last view */
        }
    }, 4000);
    return () => clearInterval(timer);
  }, [running, openId, openExperiment, project.id, load]);

  const pick = (split: Split) =>
    Object.entries(splits)
      .filter(([, s]) => s === split)
      .map(([id]) => id);
  const ready = overview?.environment.replay_data && overview.environment.task_python;
  const canStart =
    ready &&
    selectedRuns.length >= 1 &&
    selectedRuns.length <= 8 &&
    pick('train').length &&
    pick('validation').length &&
    pick('heldout').length;
  // Mirrors budget_plan on the server: the paired held-out evaluation is reserved up front.
  const heldoutReserve = 2 * pick('heldout').length * (limits.rollout_tokens || 0);
  const searchTokens = Math.max(0, (limits.experiment_tokens || 0) - heldoutReserve);
  const heldoutReserveUsd = 2 * pick('heldout').length * (limits.rollout_cost_usd || 0);
  const searchUsd = Math.max(0, (limits.max_cost_usd || 0) - heldoutReserveUsd);
  const searchRollouts = limits.rollout_tokens
    ? Math.floor(searchTokens / limits.rollout_tokens)
    : 0;

  async function start(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const experiment = await post<Experiment>(`/projects/${project.id}/experiments`, {
        role: 'code',
        objective,
        run_ids: selectedRuns,
        train: pick('train'),
        validation: pick('validation'),
        heldout: pick('heldout'),
        limits,
      });
      notify('Experiment started. It runs in the background.');
      setOpenExperiment(experiment);
      void load();
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setBusy(false);
    }
  }
  async function quickDraft() {
    setBusy(true);
    try {
      const prompt = await post<PromptRecord>(`/projects/${project.id}/evolve`, {
        role: 'code',
        run_ids: selectedRuns,
        feedback: objective,
      });
      notify('Draft ready. It has not been evaluated.');
      setOpenPrompt(prompt);
      void load();
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setBusy(false);
    }
  }
  async function experimentAction(id: string, action: 'cancel' | 'resume') {
    try {
      setOpenExperiment(
        await post<Experiment>(`/projects/${project.id}/experiments/${id}/${action}`),
      );
      void load();
    } catch (e) {
      notify(errorText(e), true);
    }
  }
  async function openDetail(id: string) {
    try {
      setOpenExperiment(await api<Experiment>(`/projects/${project.id}/experiments/${id}`));
    } catch (e) {
      notify(errorText(e), true);
    }
  }
  async function activate(prompt: PromptRecord, force = false) {
    try {
      const result = await post<PromptRecord>(
        `/projects/${project.id}/prompts/${prompt.id}/activate`,
        {
          force,
        },
      );
      notify(
        `Version ${result.version} is active for new assignments (${result.activation_label}).`,
      );
      setOpenPrompt(null);
      void load();
    } catch (e) {
      notify(errorText(e), true);
    }
  }
  async function rollback(role: string, promptId: string | null) {
    try {
      await post(`/projects/${project.id}/prompts/rollback`, { role, prompt_id: promptId });
      notify(
        promptId
          ? 'Rolled back. New assignments use the earlier version.'
          : 'New assignments use the built-in instructions.',
      );
      setOpenPrompt(null);
      void load();
    } catch (e) {
      notify(errorText(e), true);
    }
  }
  const byId = useMemo(() => new Map(prompts.map((p) => [p.id, p])), [prompts]);
  const activeCode = prompts.find((p) => p.role === 'code' && p.status === 'active');

  if (!overview)
    return (
      <div className="knowledge-view">
        <LoaderCircle size={18} className="spin" />
      </div>
    );
  return (
    <div className="knowledge-view">
      <div className="page-heading">
        <div>
          <h1>Evolution</h1>
          <p>Test new instructions on real past issues before any agent uses them.</p>
        </div>
        <button className="subtle-tag as-button" onClick={() => setSettingsOpen(true)}>
          <Settings2 size={13} />
          {overview.settings.mode === 'auto'
            ? `Automatic${overview.settings.auto_activate ? ' · may activate' : ' · review'}`
            : 'Manual'}
        </button>
      </div>
      {!ready && (
        <div className="evo-warning">
          <AlertTriangle size={15} />
          Experiments need the pytest replay data and a Python with pytest's dependencies. Set
          EVO_REPLAY_DATA and EVO_REPLAY_PYTHON, then reopen this page.
        </div>
      )}
      <div className="evolution-layout">
        <form className="evolution-form" onSubmit={start}>
          <div className="eyebrow">NEW EXPERIMENT · CODE ROLE</div>
          <label>
            Objective
            <textarea
              rows={3}
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
              placeholder="For example: reproduce the bug with a failing test before editing."
            />
          </label>
          <div className="field-group">
            <span className="field-title">
              Source runs <small>{selectedRuns.length}/8 · evidence for reflection</small>
            </span>
            <div className="run-picker">
              {runs.length ? (
                runs
                  .slice()
                  .reverse()
                  .map((r) => (
                    <label key={r.id} className="check-row">
                      <input
                        type="checkbox"
                        checked={selectedRuns.includes(r.id)}
                        disabled={!selectedRuns.includes(r.id) && selectedRuns.length >= 8}
                        onChange={(e) =>
                          setSelectedRuns((old) =>
                            e.target.checked ? [...old, r.id] : old.filter((id) => id !== r.id),
                          )
                        }
                      />
                      <span>{r.title}</span>
                      <small>{relativeDate(r.created_at)}</small>
                    </label>
                  ))
              ) : (
                <small className="quiet-note">
                  Finish an assignment first; its record is the evidence.
                </small>
              )}
            </div>
            {overview.queue.length > 0 && (
              <button
                type="button"
                className="text-button"
                onClick={() =>
                  setSelectedRuns((old) =>
                    [...new Set([...old, ...overview.queue.map((q) => q.run_id)])].slice(0, 8),
                  )
                }
              >
                Add {overview.queue.length} queued {overview.queue.length === 1 ? 'run' : 'runs'}
              </button>
            )}
          </div>
          <div className="field-group">
            <span className="field-title">
              Evaluation tasks <small>real pytest issues, scored by hidden tests</small>
            </span>
            <div className="task-table">
              {overview.tasks.map((t) => (
                <div key={t.id} className="task-row" title={t.error || ''}>
                  <span>
                    #{t.issue} {t.title}
                    {overview.exposure[t.id]?.heldout.length ? (
                      <small className="task-exposure"> · reserved held-out</small>
                    ) : overview.exposure[t.id]?.optimization.length ? (
                      <small className="task-exposure"> · seen by the search</small>
                    ) : null}
                    {t.error && <AlertTriangle size={12} />}
                  </span>
                  <select
                    value={splits[t.id] || 'skip'}
                    onChange={(e) => setSplits({ ...splits, [t.id]: e.target.value as Split })}
                    aria-label={`Use of task ${t.issue}`}
                  >
                    {(Object.keys(SPLIT_LABEL) as Split[]).map((s) => (
                      <option key={s} value={s}>
                        {SPLIT_LABEL[s]}
                      </option>
                    ))}
                  </select>
                </div>
              ))}
            </div>
            <p className="field-help">
              Reflection tasks feed GEPA's feedback. Validation scores choose candidates. Held-out
              tasks run once at the end, for the baseline and the best candidate only. A task held
              out once stays held out; a task the search has seen can't be held out later.
            </p>
          </div>
          <button type="button" className="text-button" onClick={() => setShowLimits(!showLimits)}>
            {showLimits ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Limits
          </button>
          {showLimits && (
            <div className="limit-grid">
              {LIMIT_FIELDS.map(([key, label, scale]) => (
                <label key={key}>
                  {label}
                  <input
                    type="number"
                    min={0}
                    step={key.endsWith('_usd') ? 0.01 : scale === 1 ? 1 : 'any'}
                    value={(limits[key] ?? 0) / scale}
                    onChange={(e) =>
                      setLimits({ ...limits, [key]: Number(e.target.value) * scale })
                    }
                  />
                </label>
              ))}
            </div>
          )}
          <p className="field-help">
            Each rollout is a full Code session on a real model, capped at{' '}
            {fmt(limits.rollout_tokens)} tokens. Of {fmt(limits.experiment_tokens)} tokens,{' '}
            {fmt(heldoutReserve)} are reserved for {2 * pick('heldout').length} held-out rollouts
            (baseline and candidate on each task). The search gets {fmt(searchTokens)}, enough for{' '}
            {searchRollouts} rollouts at the full cap (more if they finish under it), plus
            reflection calls. Every model call is checked against the remaining budget before it
            starts.
          </p>
          <p className="field-help">
            Spending limit ${(limits.max_cost_usd || 0).toFixed(2)}: ${heldoutReserveUsd.toFixed(2)}{' '}
            reserved for held-out rollouts, ${searchUsd.toFixed(2)} for the search. Each call is
            admitted at its worst-case cost (no cache discount assumed), so actual spending is
            usually far lower. Applies when prices are set in Settings.
          </p>
          <div className="button-row">
            <button className="button primary" disabled={busy || !canStart}>
              {busy ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}
              Start experiment
            </button>
            <button
              type="button"
              className="button secondary"
              disabled={busy || !selectedRuns.length}
              onClick={quickDraft}
              title="One model call over the selected runs. Produces an unevaluated draft."
            >
              <Sparkles size={15} />
              Draft only
            </button>
          </div>
        </form>
        <div className="prompt-list">
          <div className="section-label">
            EXPERIMENTS <span>{overview.experiments.length}</span>
          </div>
          {overview.experiments.length ? (
            overview.experiments.map((e) => (
              <button key={e.id} className="prompt-row" onClick={() => openDetail(e.id)}>
                <FlaskConical size={14} />
                <div>
                  <strong>
                    {e.kind === 'skill' ? 'Skill A/B' : 'Code instructions'}
                    <span>{e.origin === 'auto' ? 'automatic' : relativeDate(e.created_at)}</span>
                  </strong>
                  <small>
                    {RUNNING.includes(e.status)
                      ? e.stage
                      : e.summary?.gate
                        ? e.summary.gate.passed
                          ? `Passed gate · held-out ${pct(e.summary.gate.heldout?.baseline_score as number)} → ${pct(e.summary.gate.heldout?.candidate_score as number)}`
                          : e.summary.gate.reasons[0]
                        : e.error || e.note || e.stage}{' '}
                    · {fmt(e.spent.tokens)} tokens
                  </small>
                </div>
                <Status status={e.status} />
              </button>
            ))
          ) : (
            <div className="quiet-note">No experiments yet.</div>
          )}
          <div className="section-label spaced">
            CODE INSTRUCTIONS{' '}
            <span>{activeCode ? `v${activeCode.version} active` : 'built-in'}</span>
          </div>
          {prompts
            .slice()
            .reverse()
            .map((p) => (
              <button key={p.id} className="prompt-row" onClick={() => setOpenPrompt(p)}>
                <span className="role-dot" data-role={p.role} />
                <div>
                  <strong>
                    {p.role} <span>v{p.version || 1}</span>
                  </strong>
                  <small>
                    {p.status === 'active'
                      ? p.activation_label || 'active'
                      : p.gate
                        ? p.gate.passed
                          ? 'passed held-out evaluation'
                          : 'did not pass held-out evaluation'
                        : p.experiment_id
                          ? `validation ${pct(p.validation_score)}`
                          : 'not evaluated'}
                  </small>
                </div>
                <Status status={p.status === 'candidate' ? 'proposed' : p.status} />
              </button>
            ))}
          {activeCode && (
            <button className="text-button" onClick={() => rollback('code', null)}>
              <RotateCcw size={13} /> Use built-in Code instructions
            </button>
          )}
        </div>
      </div>
      {openExperiment && (
        <ExperimentDetail
          experiment={openExperiment}
          prompts={byId}
          onClose={() => setOpenExperiment(null)}
          onAction={experimentAction}
          onPrompt={(p) => {
            setOpenExperiment(null);
            setOpenPrompt(p);
          }}
        />
      )}
      {openPrompt && (
        <PromptDetail
          project={project}
          prompt={openPrompt}
          prompts={prompts}
          notify={notify}
          onClose={() => setOpenPrompt(null)}
          onActivate={activate}
          onRollback={rollback}
        />
      )}
      {settingsOpen && (
        <SettingsDialog
          project={project}
          settings={overview.settings}
          notify={notify}
          onClose={() => setSettingsOpen(false)}
          onSaved={() => {
            setSettingsOpen(false);
            void load();
          }}
        />
      )}
    </div>
  );
}

function ExperimentDetail({
  experiment: e,
  prompts,
  onClose,
  onAction,
  onPrompt,
}: {
  experiment: Experiment;
  prompts: Map<string, PromptRecord>;
  onClose: () => void;
  onAction: (id: string, action: 'cancel' | 'resume') => void;
  onPrompt: (p: PromptRecord) => void;
}) {
  const [openRollout, setOpenRollout] = useState<string | null>(null);
  const gate = e.summary?.gate;
  const candidateId = e.summary?.candidate_prompt_id;
  const heldout = (e.rollouts || []).filter((r) => r.phase === 'heldout');
  const heldoutTasks = [...new Set(heldout.map((r) => r.task_id))];
  return (
    <Modal
      title={e.kind === 'skill' ? 'Skill comparison' : 'Code instructions experiment'}
      subtitle={`${e.optimizer} · ${e.model.model} (${e.model.api_style})`}
      onClose={onClose}
      wide
    >
      <div className="skill-detail experiment-detail">
        <div className="experiment-status">
          <Status status={e.status} />
          <span>{e.stage}</span>
          <span className="spacer" />
          <span>
            {fmt(e.spent.tokens)}
            {e.budget ? ` of ${fmt(e.budget.total_tokens)}` : ''} tokens
            {e.budget && e.spent.search_tokens !== undefined
              ? ` (search ${fmt(e.spent.search_tokens)} of ${fmt(e.budget.search_tokens)}, held-out ${fmt(
                  e.spent.heldout_tokens || 0,
                )} of ${fmt(e.budget.heldout_reserve_tokens)})`
              : ''}{' '}
            · {e.spent.rollouts} rollouts · {e.spent.reflection_calls} reflections
            {e.spent.unresolved_tokens
              ? ` · ${fmt(e.spent.unresolved_tokens)} charged for requests with unknown usage`
              : ''}
            {e.spent.cost_usd || e.budget?.total_usd
              ? ` · $${(e.spent.cost_usd || 0).toFixed(2)}${
                  e.budget?.total_usd ? ` of $${e.budget.total_usd.toFixed(2)}` : ''
                }`
              : ''}
          </span>
        </div>
        {(e.error || e.note) && <div className="evo-warning">{e.error || e.note}</div>}
        {e.objective && <p className="field-help">Objective: {e.objective}</p>}
        {gate && (
          <div className={`gate ${gate.passed ? 'passed' : 'failed'}`}>
            <strong>
              {gate.passed
                ? 'Passed the evidence gate'
                : gate.heldout
                  ? 'Did not pass the evidence gate'
                  : 'No candidate reached the held-out evaluation'}
            </strong>
            {gate.heldout && (
              <span>
                Held-out ({gate.heldout.tasks} tasks): baseline {pct(gate.heldout.baseline_score)} (
                {gate.heldout.baseline_resolved} resolved, {fmt(gate.heldout.baseline_tokens)}{' '}
                tokens avg) → candidate {pct(gate.heldout.candidate_score)} (
                {gate.heldout.candidate_resolved} resolved, {fmt(gate.heldout.candidate_tokens)}{' '}
                tokens avg)
              </span>
            )}
            {gate.paired && (
              <span>
                Paired: {gate.paired.both} solved by both, {gate.paired.candidate_only} only by the
                candidate, {gate.paired.baseline_only} only by the baseline, {gate.paired.neither} by
                neither.
              </span>
            )}
            {gate.reasons.map((r) => (
              <span key={r}>· {r}</span>
            ))}
            {gate.heldout && (
              <small>
                Small samples: {gate.heldout.tasks} held-out tasks cannot show a reliable
                improvement, only the absence of obvious regressions.
              </small>
            )}
          </div>
        )}
        {heldoutTasks.length > 0 && (
          <table className="evo-table">
            <thead>
              <tr>
                <th>Held-out task</th>
                <th>Baseline</th>
                <th>Candidate</th>
              </tr>
            </thead>
            <tbody>
              {heldoutTasks.map((task) => {
                const cell = (baseline: boolean) => {
                  const r = heldout.find(
                    (x) =>
                      x.task_id === task && x.candidate_label.startsWith('baseline') === baseline,
                  );
                  return r
                    ? `${r.resolved ? 'resolved' : r.outcome} · ${pct(r.score)} · ${fmt(r.tokens)} tok`
                    : '—';
                };
                return (
                  <tr key={task}>
                    <td>{task}</td>
                    <td>{cell(true)}</td>
                    <td>{cell(false)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {e.gepa && (
          <div className="field-group">
            <span className="field-title">GEPA candidates (validation score)</span>
            {e.gepa.candidates.map((c) => {
              const prompt = c.prompt_id ? prompts.get(c.prompt_id) : undefined;
              return (
                <button
                  key={c.index}
                  className="candidate-row"
                  disabled={!prompt}
                  onClick={() => prompt && onPrompt(prompt)}
                >
                  <span>
                    {c.index === 0 ? 'Baseline' : `Candidate ${c.index}`}
                    {c.index === e.gepa!.best_index && ' · best'}
                    {c.index > 0 &&
                      ` · from ${c.parents.map((p) => (p === 0 ? 'baseline' : `#${p}`)).join(', ')}`}
                  </span>
                  <span>{pct(c.validation_score)}</span>
                </button>
              );
            })}
            {candidateId && prompts.get(candidateId) && (
              <button
                className="button secondary small"
                onClick={() => onPrompt(prompts.get(candidateId)!)}
              >
                Review the best candidate
              </button>
            )}
          </div>
        )}
        <div className="field-group">
          <span className="field-title">Rollouts</span>
          {(e.rollouts || []).map((r) => (
            <div key={r.id} className="rollout">
              <button
                className="rollout-row"
                onClick={() => setOpenRollout(openRollout === r.id ? null : r.id)}
              >
                <span className={`phase-tag ${r.phase}`}>{r.phase}</span>
                <span>{r.task_id}</span>
                <span>{r.candidate_label}</span>
                <span className="spacer" />
                <span>
                  {r.resolved ? 'resolved' : r.outcome}
                  {r.fail_to_pass && ` · ${r.fail_to_pass[0]}/${r.fail_to_pass[1]} target`}
                  {r.pass_to_pass && ` · ${r.pass_to_pass[0]}/${r.pass_to_pass[1]} kept`}
                </span>
                <span>{fmt(r.tokens)} tok</span>
                <span>{Math.round(r.latency_s || 0)}s</span>
                {r.reused_from && (
                  <span title="Reused a finished rollout with the same inputs">reused</span>
                )}
                {Boolean(r.leakage?.length) && <AlertTriangle size={12} />}
              </button>
              {openRollout === r.id && (
                <div className="rollout-body">
                  {r.error && <p>Error: {r.error}</p>}
                  {Boolean(r.leakage?.length) && (
                    <p>Possible evaluator access flagged: {JSON.stringify(r.leakage)}</p>
                  )}
                  {r.final_message && <MarkdownText>{r.final_message}</MarkdownText>}
                  {r.commands?.length ? <pre>{r.commands.join('\n')}</pre> : null}
                  <pre className="prompt-diff">{r.diff || '(no changes)'}</pre>
                </div>
              )}
            </div>
          ))}
        </div>
        {e.proposals?.length ? (
          <details>
            <summary>Reflection proposals ({e.proposals.length}, including rejected ones)</summary>
            {e.proposals.map((p, i) => (
              <pre key={i} className="prompt-diff">
                {p.text}
              </pre>
            ))}
          </details>
        ) : null}
        {e.log?.length ? (
          <details>
            <summary>Optimizer log</summary>
            <pre>{e.log.join('\n')}</pre>
          </details>
        ) : null}
        <p className="field-help">
          Source runs: {e.source_run_ids.map((id) => id.slice(0, 8)).join(', ') || 'none'} ·
          Reflection {e.splits.train.join(', ') || '—'} · Validation{' '}
          {e.splits.validation.join(', ')} · Held-out {e.splits.heldout.join(', ')}
        </p>
      </div>
      <div className="modal-footer">
        {RUNNING.includes(e.status) ? (
          <button className="button secondary" onClick={() => onAction(e.id, 'cancel')}>
            <Square size={13} /> Cancel
          </button>
        ) : ['interrupted', 'cancelled', 'failed', 'stopped'].includes(e.status) ? (
          <button className="button secondary" onClick={() => onAction(e.id, 'resume')}>
            <Play size={13} /> Resume (finished rollouts are reused)
          </button>
        ) : null}
      </div>
    </Modal>
  );
}

function PromptDetail({
  project,
  prompt,
  prompts,
  notify,
  onClose,
  onActivate,
  onRollback,
}: {
  project: Project;
  prompt: PromptRecord;
  prompts: PromptRecord[];
  notify: (text: string, error?: boolean) => void;
  onClose: () => void;
  onActivate: (p: PromptRecord, force?: boolean) => void;
  onRollback: (role: string, id: string | null) => void;
}) {
  const [confirm, setConfirm] = useState(false),
    [textEval, setTextEval] = useState(false);
  const active = prompts.find((p) => p.role === prompt.role && p.status === 'active');
  const compareTo = active && active.id !== prompt.id ? active : null;
  const passed = prompt.gate?.passed;
  return (
    <Modal
      title={`${prompt.role} · version ${prompt.version || 1}`}
      subtitle={
        prompt.status === 'active'
          ? `Active for new assignments · ${prompt.activation_label || 'activated'}`
          : passed
            ? 'Passed held-out evaluation. Review it before activating.'
            : prompt.gate
              ? 'Did not pass held-out evaluation.'
              : 'Not evaluated with executable checks.'
      }
      onClose={onClose}
      wide
    >
      <div className="skill-detail">
        <div className="field-title">
          Changes compared with{' '}
          {compareTo ? `active v${compareTo.version}` : 'the built-in instructions'}
        </div>
        <Diff before={compareTo?.content || ''} after={prompt.content || ''} />
        {prompt.gate && (
          <div className={`gate ${passed ? 'passed' : 'failed'}`}>
            <span>
              Held-out: baseline {pct(prompt.gate.heldout?.baseline_score)} → this version{' '}
              {pct(prompt.gate.heldout?.candidate_score)}
            </span>
            {prompt.gate.reasons.map((r) => (
              <span key={r}>· {r}</span>
            ))}
          </div>
        )}
        <p className="field-help">
          Evidence: source runs{' '}
          {(prompt.source_run_ids || []).map((id) => id.slice(0, 8)).join(', ') || '—'}
          {prompt.source_event_ids?.length
            ? ` · ${prompt.source_event_ids.length} cited events`
            : ''}
          {prompt.experiment_id ? ` · experiment ${prompt.experiment_id.slice(0, 8)}` : ''}
          {prompt.parent_prompt_ids?.length
            ? ` · derived from ${prompt.parent_prompt_ids.map((p) => (p ? `v${prompts.find((x) => x.id === p)?.version ?? '?'}` : 'built-in')).join(', ')}`
            : ''}
        </p>
        <button className="text-button" onClick={() => setTextEval(!textEval)}>
          {textEval ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Text-response check
          (model-judged, no code is run)
        </button>
        {textEval && <TextEvaluator project={project} prompt={prompt} notify={notify} />}
      </div>
      <div className="modal-footer">
        {prompt.status === 'active' ? (
          <button className="button secondary" onClick={() => onRollback(prompt.role, null)}>
            <RotateCcw size={14} /> Roll back to built-in
          </button>
        ) : prompt.status === 'superseded' || prompt.status === 'rolled_back' ? (
          <button className="button secondary" onClick={() => onRollback(prompt.role, prompt.id)}>
            <RotateCcw size={14} /> Restore this version
          </button>
        ) : passed ? (
          <button className="button primary" onClick={() => onActivate(prompt)}>
            <Check size={15} /> Activate for new assignments
          </button>
        ) : confirm ? (
          <>
            <span className="field-help">
              It will be labelled as activated without passing evaluation.
            </span>
            <button className="button secondary" onClick={() => setConfirm(false)}>
              <X size={14} /> Keep reviewing
            </button>
            <button className="button primary" onClick={() => onActivate(prompt, true)}>
              Activate anyway
            </button>
          </>
        ) : (
          <button className="button secondary" onClick={() => setConfirm(true)}>
            Activate without passing evaluation…
          </button>
        )}
      </div>
    </Modal>
  );
}

function TextEvaluator({
  project,
  prompt,
  notify,
}: {
  project: Project;
  prompt: PromptRecord;
  notify: Props['notify'];
}) {
  const [cases, setCases] = useState(
      '[\n  {\n    "input": "Describe a scenario",\n    "expected": "What a good answer includes",\n    "split": "heldout"\n  }\n]',
    ),
    [result, setResult] = useState<unknown>(null),
    [busy, setBusy] = useState(false);
  async function run() {
    setBusy(true);
    try {
      const parsed = JSON.parse(cases);
      if (!Array.isArray(parsed)) throw new Error('Cases must be a JSON array.');
      setResult(
        await post(`/projects/${project.id}/prompts/${prompt.id}/evaluations`, { cases: parsed }),
      );
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="evaluation-panel">
      <p className="field-help">
        Compares written answers from the baseline and this version, scored by a model against your
        criteria. It does not run tools or code, so it cannot show that code changes work.
      </p>
      <textarea
        className="code-input"
        rows={8}
        value={cases}
        onChange={(e) => setCases(e.target.value)}
        spellCheck={false}
      />
      <button className="button secondary" disabled={busy} onClick={run}>
        {busy ? <LoaderCircle size={14} className="spin" /> : <FlaskConical size={14} />} Run text
        check
      </button>
      {result != null && <pre className="evaluation-result">{JSON.stringify(result, null, 2)}</pre>}
    </div>
  );
}

function SettingsDialog({
  project,
  settings,
  notify,
  onClose,
  onSaved,
}: {
  project: Project;
  settings: EvolutionSettings;
  notify: Props['notify'];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState(settings),
    [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      await put(`/projects/${project.id}/evolution/settings`, draft);
      notify('Evolution settings saved.');
      onSaved();
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title="Background evolution"
      subtitle="Finished and failed assignments are queued as evidence. Nothing runs at startup."
      onClose={onClose}
    >
      <div className="settings-body">
        <label className="check-row">
          <input
            type="radio"
            checked={draft.mode === 'manual'}
            onChange={() => setDraft({ ...draft, mode: 'manual', auto_activate: false })}
          />
          <span>Manual: the queue only collects runs. You start experiments.</span>
        </label>
        <label className="check-row">
          <input
            type="radio"
            checked={draft.mode === 'auto'}
            onChange={() => setDraft({ ...draft, mode: 'auto' })}
          />
          <span>Automatic: start one bounded experiment when enough runs are queued.</span>
        </label>
        {draft.mode === 'auto' && (
          <>
            <label>
              Queued runs needed
              <input
                type="number"
                min={1}
                max={20}
                value={draft.min_queued_runs}
                onChange={(e) => setDraft({ ...draft, min_queued_runs: Number(e.target.value) })}
              />
            </label>
            <label>
              Automatic experiments per day
              <input
                type="number"
                min={1}
                max={20}
                value={draft.max_auto_per_day}
                onChange={(e) => setDraft({ ...draft, max_auto_per_day: Number(e.target.value) })}
              />
            </label>
            <label>
              Token budget per automatic experiment
              <input
                type="number"
                min={1}
                value={draft.limits.experiment_tokens}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    limits: { ...draft.limits, experiment_tokens: Number(e.target.value) },
                  })
                }
              />
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={draft.auto_activate}
                onChange={(e) => setDraft({ ...draft, auto_activate: e.target.checked })}
              />
              <span>
                Activate a candidate automatically when it passes every gate and improves held-out
                results. Otherwise it waits for your review. Skills are never activated
                automatically.
              </span>
            </label>
          </>
        )}
      </div>
      <div className="modal-footer">
        <button className="button primary" disabled={busy} onClick={save}>
          {busy && <LoaderCircle size={15} className="spin" />}Save
        </button>
      </div>
    </Modal>
  );
}
