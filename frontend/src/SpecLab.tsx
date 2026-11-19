import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  FileText,
  GitCompare,
  LoaderCircle,
  Play,
  RotateCcw,
  ScrollText,
  ShieldCheck,
  X,
} from 'lucide-react';
import { api, errorText, post } from './api';
import { MarkdownText, Modal, Status, relativeDate } from './components';
import type { Project } from './types';

type Props = { project: Project; notify: (text: string, error?: boolean) => void };
type Tab = 'run' | 'compare' | 'govern';
type Backend = 'recorded' | 'fixture' | 'aicommons' | 'ollama' | 'evo';
type Case = {
  id: string;
  repo: string;
  why: string;
  title: string;
  must_stay_blocked: boolean;
};
type ToolCall = { role?: string; tool: string; query: string };
type Row = {
  case: string;
  harness: string;
  model: string;
  completed: boolean;
  score: number;
  checks: Record<string, boolean>;
  exit_status: string;
  error?: string | null;
  failed_role?: string | null;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  elapsed_seconds?: number;
  format_errors?: number;
  tool_calls?: ToolCall[];
  markdown?: string;
  trajectory_messages?: { role: string; content: string }[];
  events?: { role: string; status: string; message: string }[];
  agents?: { role: string; calls: number; status: string }[];
};
type Summary = {
  runs: number;
  completed: number;
  mean_score: number;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  seconds: number;
};
type Replay = {
  cases: {
    case: string;
    baseline: number;
    candidate: number;
    baseline_status: string;
    candidate_status: string;
  }[];
  regressions: string[];
  unblocked: string[];
  baseline_mean: number;
  candidate_mean: number;
  harness: string;
  model: string;
  recorded: boolean;
};
type Job = {
  id: string;
  kind: 'spec' | 'benchmark' | 'cycle' | 'replay';
  status: string;
  title: string;
  backend: Backend;
  model: string;
  harness?: string;
  case_id?: string;
  cycle_id?: string;
  progress: string[];
  error?: string | null;
  started_at: string;
  seconds?: number;
  result?: Row;
  rows?: Row[];
  summary?: Record<string, Summary>;
  replay?: Replay;
};
type Version = {
  id: string;
  prompt: string;
  status: string;
  rationale?: string;
  created_at: string;
  run_id?: string;
};
type Gate = { id: string; name: string; rule: string; passed: boolean; detail: string };
type CycleRow = {
  id: string;
  title: string;
  status: string;
  mode: string;
  created_at: string;
  candidate_version?: string;
  baseline_version?: string;
  error?: string;
  case_id?: string;
  critic?: boolean;
  replay?: string;
};
type Audit = {
  action: string;
  at: string;
  run_id?: string;
  version?: string;
  reviewer?: string;
  hash?: string;
};
type Overview = {
  cases: Case[];
  harnesses: { id: string; description: string }[];
  backends: {
    recorded: {
      model: string;
      recorded_at: string;
      demo_cycle: string | null;
      coverage: Record<string, string[]>;
    } | null;
    aicommons: {
      models: string[];
      default: string;
      job_token_cap: number;
      usage: { limit: number; remaining: number; reset_at: string } | null;
      error: string | null;
    } | null;
    ollama: string[] | null;
    evo: { model: string; configured: boolean };
  };
  jobs: Job[];
  recorded: { name: string; models: string[]; cases: number; summary: Record<string, Summary> }[];
  governance: {
    active_version: string;
    versions: Version[];
    cycles: CycleRow[];
    feedback: { id: string; text: string; reviewer: string; at: string }[];
    audit: Audit[];
    ledger_ok: boolean;
    gates: { id: string; name: string; rule: string }[];
  };
};
type CycleDetail = {
  run: {
    id: string;
    status: string;
    mode: string;
    error?: string;
    evaluation?: {
      results: { scenario_id: string; baseline: string; candidate: string; rationale: string }[];
      baseline_passed: number;
      candidate_passed: number;
      total: number;
    };
    scenarios?: { id: string; category: string; title: string }[];
    tool_calls?: ToolCall[];
    agents?: { role: string; calls: number; status: string }[];
    markdown?: string;
    benchmark_replay?: Replay;
  };
  candidate?: Version;
  baseline?: Version;
  gates: Gate[];
};

const HARNESS_LABEL: Record<string, string> = {
  mini: 'mini-swe-agent',
  'prime-rlm': 'Prime-RLM',
  'prime-schema': 'Prime-RLM (schema)',
};
const HARNESS_NOTE: Record<string, string> = {
  mini: 'One agent, one linear history, one shell-style action per turn. Validators answer like a terminal.',
  'prime-rlm':
    'Six specialists spawned and collected in parallel: research, prompt, scenarios, two writers, critic.',
  'prime-schema':
    'The same orchestrator, with schema-constrained decoding where the model allows it.',
};
const pct = (n?: number) => (n == null ? '—' : `${Math.round(n * 100)}%`);
const fmt = (n?: number) =>
  n == null ? '—' : n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);
const isRunning = (s: string) => s === 'running' || s === 'queued';

function BackendPicker({
  overview,
  backend,
  model,
  onChange,
}: {
  overview: Overview;
  backend: Backend;
  model: string;
  onChange: (backend: Backend, model: string) => void;
}) {
  const ollama = overview.backends.ollama;
  const commons = overview.backends.aicommons;
  return (
    <div className="field-group">
      <span className="field-title">
        Model <small>same model for every harness</small>
      </span>
      <select
        value={backend}
        onChange={(e) => {
          const next = e.target.value as Backend;
          onChange(
            next,
            next === 'ollama'
              ? ollama?.[0] || ''
              : next === 'aicommons'
                ? commons?.default || ''
                : '',
          );
        }}
        aria-label="Model backend"
      >
        <option value="recorded" disabled={!overview.backends.recorded}>
          {overview.backends.recorded
            ? `Recorded · ${overview.backends.recorded.model} (no model needed)`
            : 'Recorded (no recording found)'}
        </option>
        <option value="fixture">Scripted fixture (no model)</option>
        <option value="aicommons" disabled={!commons || !!commons.error}>
          UCSB AI Commons{commons ? (commons.error ? ' (unavailable)' : '') : ' (set key in .env)'}
        </option>
        <option value="ollama" disabled={!ollama?.length}>
          Ollama{ollama ? (ollama.length ? '' : ' (no models installed)') : ' (not running)'}
        </option>
        <option value="evo" disabled={!overview.backends.evo.configured}>
          EVO model · {overview.backends.evo.model}
          {overview.backends.evo.configured ? '' : ' (set a key in Settings)'}
        </option>
      </select>
      {backend === 'aicommons' && commons && (
        <>
          <select
            value={model}
            onChange={(e) => onChange(backend, e.target.value)}
            aria-label="AI Commons model"
          >
            {commons.models.map((m) => (
              <option key={m}>{m}</option>
            ))}
          </select>
          <p className="field-help">
            {commons.usage
              ? `${commons.usage.remaining.toLocaleString()} of ${commons.usage.limit.toLocaleString()} tokens left this month. `
              : ''}
            Each job stops at {commons.job_token_cap.toLocaleString()} tokens.
          </p>
        </>
      )}
      {backend === 'ollama' && ollama && (
        <select
          value={model}
          onChange={(e) => onChange(backend, e.target.value)}
          aria-label="Ollama model"
        >
          {ollama.map((m) => (
            <option key={m}>{m}</option>
          ))}
        </select>
      )}
      {backend === 'recorded' && overview.backends.recorded && (
        <p className="field-help">
          Replays real {overview.backends.recorded.model} replies recorded on the synthetic
          benchmark cases ({relativeDate(overview.backends.recorded.recorded_at)}). The harness
          loop, tools and validators run for real; only the model is replaced. Unrecorded
          combinations are listed as unavailable.
        </p>
      )}
      {backend === 'fixture' && (
        <p className="field-help">
          A fixed script on the bundled fixture: the smallest offline smoke test of the harness
          loop, tools and validators.
        </p>
      )}
    </div>
  );
}

function Checks({ checks }: { checks: Record<string, boolean> }) {
  return (
    <div className="spec-checks">
      {Object.entries(checks).map(([name, ok]) => (
        <span key={name} className={ok ? 'ok' : 'bad'}>
          {ok ? <Check size={11} /> : <X size={11} />} {name.replace(/_/g, ' ')}
        </span>
      ))}
    </div>
  );
}

function SummaryTable({ summary }: { summary: Record<string, Summary> }) {
  return (
    <table className="evo-table">
      <thead>
        <tr>
          <th>Harness</th>
          <th>Drafts</th>
          <th>Mean score</th>
          <th>Model calls</th>
          <th>Tokens in / out</th>
          <th>Time</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(summary).map(([h, s]) => (
          <tr key={h}>
            <td>{HARNESS_LABEL[h] || h}</td>
            <td>
              {s.completed}/{s.runs}
            </td>
            <td>{pct(s.mean_score)}</td>
            <td>{s.model_calls}</td>
            <td>
              {fmt(s.input_tokens)} / {fmt(s.output_tokens)}
            </td>
            <td>{s.seconds}s</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RowDetail({ row }: { row: Row }) {
  const [open, setOpen] = useState<string | null>(null);
  const toggle = (k: string) => setOpen(open === k ? null : k);
  return (
    <div className="experiment-detail">
      <div className="experiment-status">
        <Status status={row.completed ? 'completed' : 'failed'} />
        <span>
          {HARNESS_LABEL[row.harness] || row.harness} · {row.case} · {row.model}
        </span>
        <span className="spacer" />
        <span>
          {row.model_calls} calls · {fmt(row.input_tokens)}/{fmt(row.output_tokens)} tokens
          {row.elapsed_seconds != null && ` · ${row.elapsed_seconds}s`}
        </span>
      </div>
      <div className={`gate ${row.completed ? 'passed' : 'failed'}`}>
        <strong>
          {row.exit_status} · score {pct(row.score)}
        </strong>
        {row.error && <small>{row.error}</small>}
        {row.failed_role && <small>Failed specialist: {row.failed_role}</small>}
        {Object.keys(row.checks).length > 0 && <Checks checks={row.checks} />}
      </div>
      {row.tool_calls && row.tool_calls.length > 0 && (
        <div className="rollout">
          <button className="rollout-row" onClick={() => toggle('tools')}>
            {open === 'tools' ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            Tool calls <span className="phase-tag">{row.tool_calls.length}</span>
          </button>
          {open === 'tools' && (
            <div className="rollout-body">
              <table className="evo-table">
                <tbody>
                  {row.tool_calls.map((t, i) => (
                    <tr key={i}>
                      <td>{t.role || 'mini'}</td>
                      <td>{t.tool}</td>
                      <td>{t.query || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
      {row.trajectory_messages && row.trajectory_messages.length > 0 && (
        <div className="rollout">
          <button className="rollout-row" onClick={() => toggle('traj')}>
            {open === 'traj' ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            Trajectory <span className="phase-tag">{row.trajectory_messages.length} messages</span>
          </button>
          {open === 'traj' && (
            <div className="rollout-body">
              {row.trajectory_messages.slice(2).map((m, i) => (
                <div key={i} className={`spec-turn ${m.role}`}>
                  <small>
                    {m.role === 'assistant' ? 'agent' : m.role === 'user' ? 'environment' : m.role}
                  </small>
                  <pre>
                    {m.content.length > 2400 ? m.content.slice(0, 2400) + '\n…' : m.content}
                  </pre>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {row.events && row.events.length > 0 && (
        <div className="rollout">
          <button className="rollout-row" onClick={() => toggle('events')}>
            {open === 'events' ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            Specialist events <span className="phase-tag">{row.events.length}</span>
          </button>
          {open === 'events' && (
            <div className="rollout-body">
              <table className="evo-table">
                <tbody>
                  {row.events.map((e, i) => (
                    <tr key={i}>
                      <td>{e.role}</td>
                      <td>{e.status}</td>
                      <td>{e.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
      {row.markdown && (
        <div className="rollout">
          <button className="rollout-row" onClick={() => toggle('spec')}>
            {open === 'spec' ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            Product specification
          </button>
          {open === 'spec' && (
            <div className="rollout-body spec-markdown">
              <MarkdownText>{row.markdown}</MarkdownText>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RowsTable({ rows, onOpen }: { rows: Row[]; onOpen: (row: Row) => void }) {
  return (
    <table className="evo-table">
      <thead>
        <tr>
          <th>Case</th>
          <th>Harness</th>
          <th>Outcome</th>
          <th>Score</th>
          <th>Calls</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} className="spec-clickable" onClick={() => onOpen(r)}>
            <td>{r.case}</td>
            <td>{HARNESS_LABEL[r.harness] || r.harness}</td>
            <td title={r.error || ''}>{r.exit_status}</td>
            <td>{pct(r.score)}</td>
            <td>{r.model_calls}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function JobDetail({ job, onClose }: { job: Job; onClose: () => void }) {
  const [row, setRow] = useState<Row | null>(null);
  return (
    <Modal
      title={row ? `${row.case} · ${HARNESS_LABEL[row.harness] || row.harness}` : job.title}
      subtitle={`${job.kind} · ${job.model} · ${relativeDate(job.started_at)}`}
      onClose={row ? () => setRow(null) : onClose}
      wide
    >
      <div className="skill-detail experiment-detail">
        {row ? (
          <>
            <button className="text-button" onClick={() => setRow(null)}>
              ← Back to the comparison
            </button>
            <RowDetail row={row} />
          </>
        ) : (
          <>
            <div className="experiment-status">
              <Status status={job.status} />
              <span>
                {isRunning(job.status)
                  ? job.progress.at(-1) || 'Starting'
                  : job.error || 'Finished'}
              </span>
              <span className="spacer" />
              {job.seconds != null && <span>{job.seconds}s</span>}
            </div>
            {job.result && <RowDetail row={job.result} />}
            {job.summary && <SummaryTable summary={job.summary} />}
            {job.rows && <RowsTable rows={job.rows} onOpen={setRow} />}
            {job.replay && <ReplayTable replay={job.replay} />}
            {isRunning(job.status) && job.progress.length > 0 && (
              <pre className="evaluation-result">{job.progress.slice(-8).join('\n')}</pre>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}

function ReplayTable({ replay }: { replay: Replay }) {
  return (
    <div className="field-group">
      <span className="field-title">
        Frozen benchmark replay{' '}
        <small>
          {HARNESS_LABEL[replay.harness] || replay.harness} · {replay.model}
          {replay.recorded && ' · recorded'}
        </small>
      </span>
      <table className="evo-table">
        <thead>
          <tr>
            <th>Case</th>
            <th>Current prompt</th>
            <th>Candidate</th>
          </tr>
        </thead>
        <tbody>
          {replay.cases.map((c) => (
            <tr
              key={c.case}
              className={
                replay.regressions.includes(c.case) || replay.unblocked.includes(c.case)
                  ? 'spec-regression'
                  : ''
              }
            >
              <td>{c.case}</td>
              <td>
                {pct(c.baseline)} <small>{c.baseline_status}</small>
              </td>
              <td>
                {pct(c.candidate)} <small>{c.candidate_status}</small>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PromptDiff({ before, after }: { before: string; after: string }) {
  const old = new Set(before.split('\n').map((l) => l.trim()));
  const now = new Set(after.split('\n').map((l) => l.trim()));
  return (
    <div className="prompt-diff">
      {before
        .split('\n')
        .filter((l) => !now.has(l.trim()))
        .map((l, i) => (
          <div key={`r${i}`} className="removed">
            {l}
          </div>
        ))}
      {after.split('\n').map((l, i) => (
        <div key={i} className={old.has(l.trim()) ? '' : 'added'}>
          {l || ' '}
        </div>
      ))}
    </div>
  );
}

function CycleDetailModal({
  base,
  cycleId,
  overview,
  busy,
  onClose,
  onChanged,
  notify,
}: {
  base: string;
  cycleId: string;
  overview: Overview;
  busy: boolean;
  onClose: () => void;
  onChanged: () => void;
  notify: Props['notify'];
}) {
  const [detail, setDetail] = useState<CycleDetail | null>(null),
    [reviewer, setReviewer] = useState(''),
    [feedback, setFeedback] = useState(''),
    [backend, setBackend] = useState<Backend>('recorded'),
    [model, setModel] = useState(''),
    [working, setWorking] = useState(false);
  const load = useCallback(async () => {
    try {
      setDetail(
        await api<CycleDetail>(
          `${base}/cycles/${cycleId}?reviewer=${encodeURIComponent(reviewer)}`,
        ),
      );
    } catch (e) {
      notify(errorText(e), true);
    }
  }, [base, cycleId, reviewer, notify]);
  useEffect(() => {
    void load();
  }, [load, busy]);

  async function act(path: string, body: unknown, message: string) {
    setWorking(true);
    try {
      await post(`${base}${path}`, body);
      notify(message);
      onChanged();
      await load();
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setWorking(false);
    }
  }
  if (!detail)
    return (
      <Modal title="Evolution cycle" onClose={onClose} wide>
        <LoaderCircle size={18} className="spin" />
      </Modal>
    );
  const { run, candidate, baseline, gates } = detail;
  const waiting = run.status === 'needs_review';
  const blocking = gates.filter((g) => !g.passed && g.id !== 'approval');
  return (
    <Modal
      title={`Candidate ${candidate?.id || ''} vs ${baseline?.id || ''}`}
      subtitle={`${run.mode} · ${run.status}`}
      onClose={onClose}
      wide
    >
      <div className="skill-detail experiment-detail">
        {run.error && (
          <div className="evo-warning">
            <AlertTriangle size={15} /> {run.error}
          </div>
        )}
        {candidate && baseline && (
          <div className="field-group">
            <span className="field-title">
              Prompt change <small>{candidate.rationale}</small>
            </span>
            <PromptDiff before={baseline.prompt} after={candidate.prompt} />
          </div>
        )}
        {run.evaluation && (
          <div className="field-group">
            <span className="field-title">
              Critic on generated scenarios{' '}
              <small>
                current {run.evaluation.baseline_passed}/{run.evaluation.total} · candidate{' '}
                {run.evaluation.candidate_passed}/{run.evaluation.total} · model judgment, not
                executed
              </small>
            </span>
            <table className="evo-table">
              <tbody>
                {run.evaluation.results.map((r) => {
                  const s = run.scenarios?.find((x) => x.id === r.scenario_id);
                  return (
                    <tr key={r.scenario_id}>
                      <td>
                        {s?.category?.replace(/_/g, ' ')} <small>{s?.title}</small>
                      </td>
                      <td>{r.baseline}</td>
                      <td>{r.candidate}</td>
                      <td>
                        <small>{r.rationale}</small>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {run.benchmark_replay && <ReplayTable replay={run.benchmark_replay} />}
        {gates.length > 0 && (
          <div className="field-group">
            <span className="field-title">
              Governance gates <small>deterministic; no model decides promotion</small>
            </span>
            {gates.map((g) => (
              <div key={g.id} className={`gate ${g.passed ? 'passed' : 'failed'}`}>
                <strong>
                  {g.passed ? <Check size={12} /> : <X size={12} />} {g.name}
                </strong>
                <small>{g.detail}</small>
              </div>
            ))}
          </div>
        )}
        {waiting && (
          <>
            <div className="field-group">
              <span className="field-title">
                Gate 5 · replay the frozen benchmark{' '}
                <small>mini-swe-agent, current prompt vs candidate, same snapshots</small>
              </span>
              <BackendPicker
                overview={overview}
                backend={backend}
                model={model}
                onChange={(b, m) => {
                  setBackend(b);
                  setModel(m);
                }}
              />
              <button
                className="button secondary"
                disabled={busy || working}
                onClick={() =>
                  act(
                    `/cycles/${run.id}/replay`,
                    { backend, model, harness: 'mini' },
                    'Benchmark replay started.',
                  )
                }
              >
                {busy ? <LoaderCircle size={15} className="spin" /> : <GitCompare size={15} />}
                Replay benchmark
              </button>
            </div>
            <div className="field-group">
              <span className="field-title">
                Feedback <small>stored as unverified memory; feeds the next prompt revision</small>
              </span>
              <textarea
                rows={2}
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                placeholder="For example: ask the product owner before changing whitespace behavior."
              />
            </div>
            <label className="field-group">
              <span className="field-title">Reviewer</span>
              <input
                value={reviewer}
                onChange={(e) => setReviewer(e.target.value)}
                placeholder="Your name"
              />
            </label>
            <div className="button-row">
              <button
                className="button primary"
                disabled={working || busy || !reviewer.trim() || blocking.length > 0}
                title={blocking.length ? `Blocked: ${blocking.map((g) => g.name).join(', ')}` : ''}
                onClick={() =>
                  act(
                    `/cycles/${run.id}/decision`,
                    { action: 'promote', reviewer },
                    `${candidate?.id} is now active.`,
                  )
                }
              >
                <ShieldCheck size={15} /> Promote
              </button>
              <button
                className="button secondary"
                disabled={working || !reviewer.trim()}
                onClick={() =>
                  act(
                    `/cycles/${run.id}/decision`,
                    { action: 'reject', reviewer },
                    'Candidate rejected.',
                  )
                }
              >
                Reject
              </button>
              <button
                className="button secondary"
                disabled={working || !reviewer.trim() || !feedback.trim()}
                onClick={async () => {
                  await act(
                    `/cycles/${run.id}/feedback`,
                    { text: feedback, reviewer },
                    'Feedback saved.',
                  );
                  setFeedback('');
                }}
              >
                Save feedback
              </button>
            </div>
          </>
        )}
        {run.markdown && (
          <details>
            <summary className="field-title">Candidate's product specification</summary>
            <div className="spec-markdown">
              <MarkdownText>{run.markdown}</MarkdownText>
            </div>
          </details>
        )}
      </div>
    </Modal>
  );
}

export function SpecLabView({ project, notify }: Props) {
  const base = `/projects/${project.id}/spec`;
  const [overview, setOverview] = useState<Overview | null>(null),
    [tab, setTab] = useState<Tab>('run'),
    [harness, setHarness] = useState('mini'),
    [caseId, setCaseId] = useState('bundled-fixture'),
    [evidence, setEvidence] = useState(''),
    [compareHarnesses, setCompareHarnesses] = useState(['mini', 'prime-rlm']),
    [compareCases, setCompareCases] = useState<string[]>(['bundled-fixture']),
    [backend, setBackend] = useState<Backend>('recorded'),
    [model, setModel] = useState(''),
    [busy, setBusy] = useState(false),
    [openJob, setOpenJob] = useState<Job | null>(null),
    [openCycle, setOpenCycle] = useState<string | null>(null),
    [rollbackBy, setRollbackBy] = useState('');

  const load = useCallback(async () => {
    try {
      const data = await api<Overview>(base);
      setOverview((old) => {
        if (!old && !data.backends.recorded) setBackend('fixture');
        return data;
      });
    } catch (e) {
      notify(errorText(e), true);
    }
  }, [base, notify]);
  useEffect(() => {
    setOverview(null);
    setOpenJob(null);
    setOpenCycle(null);
    void load();
  }, [load]);

  const running = overview?.jobs.some((j) => isRunning(j.status));
  const openJobId = openJob?.id;
  useEffect(() => {
    if (!running && !(openJob && isRunning(openJob.status))) return;
    const timer = setInterval(async () => {
      void load();
      if (openJobId)
        try {
          setOpenJob(await api<Job>(`${base}/jobs/${openJobId}`));
        } catch {
          /* keep the last view */
        }
    }, 2000);
    return () => clearInterval(timer);
  }, [running, openJobId, openJob, base, load]);

  async function start(path: string, body: unknown, message: string) {
    setBusy(true);
    try {
      const job = await post<Job>(`${base}${path}`, body);
      notify(message);
      setOpenJob(job);
      void load();
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setBusy(false);
    }
  }
  async function showJob(id: string) {
    try {
      setOpenJob(await api<Job>(`${base}/jobs/${id}`));
    } catch (e) {
      notify(errorText(e), true);
    }
  }
  async function showRecorded(name: string) {
    try {
      const data = await api<{ rows: Row[]; summary: Record<string, Summary> }>(
        `${base}/recorded/${name}`,
      );
      setOpenJob({
        id: name,
        kind: 'benchmark',
        status: 'completed',
        title: `Recorded comparison · ${name}`,
        backend: 'ollama',
        model: data.rows[0]?.model || '',
        progress: [],
        started_at: '',
        ...data,
      });
    } catch (e) {
      notify(errorText(e), true);
    }
  }
  async function rollback(version: string) {
    try {
      await post(`${base}/versions/${version}/rollback`, { reviewer: rollbackBy });
      notify(`${version} is active again.`);
      void load();
    } catch (e) {
      notify(errorText(e), true);
    }
  }

  if (!overview)
    return (
      <div className="knowledge-view">
        <LoaderCircle size={18} className="spin" />
      </div>
    );
  const gov = overview.governance;
  const offline = backend === 'recorded' || backend === 'fixture';
  const recording = overview.backends.recorded;
  /** Whether every listed harness can run this case with the chosen backend. */
  const covered = (id: string, harnesses: string[]) =>
    backend === 'fixture'
      ? id === 'bundled-fixture'
      : backend === 'recorded'
        ? harnesses.every((h) => recording?.coverage[h]?.includes(id))
        : true;
  const usableCases = overview.cases.filter((c) => covered(c.id, [harness]));
  const cycleCases = overview.cases.filter((c) => covered(c.id, ['prime-rlm']));
  const runCase = usableCases.some((c) => c.id === caseId) ? caseId : usableCases[0]?.id || '';
  const cycleCase = cycleCases.some((c) => c.id === caseId)
    ? caseId
    : cycleCases.find((c) => c.id === recording?.demo_cycle)?.id || cycleCases[0]?.id || '';
  const compareUsable = compareCases.filter((id) => covered(id, compareHarnesses));
  const picker = (
    <BackendPicker
      overview={overview}
      backend={backend}
      model={model}
      onChange={(b, m) => {
        setBackend(b);
        setModel(m);
        if (b === 'fixture') setCompareCases(['bundled-fixture']);
      }}
    />
  );
  const jobsOf = (kinds: Job['kind'][]) => overview.jobs.filter((j) => kinds.includes(j.kind));

  return (
    <div className="knowledge-view">
      <div className="page-heading">
        <div>
          <h1>Spec Lab</h1>
        </div>
        <span className="subtle-tag" title="Active supplemental prompt for both harnesses">
          <ScrollText size={13} /> Prompt {gov.active_version}
        </span>
      </div>
      <div className="spec-tabs" role="tablist">
        {(
          [
            ['run', 'Run a spec', FileText],
            ['compare', 'Compare harnesses', GitCompare],
            ['govern', 'Governed evolution', ShieldCheck],
          ] as const
        ).map(([id, label, Icon]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            className={tab === id ? 'active' : ''}
            onClick={() => setTab(id)}
          >
            <Icon size={14} /> {label}
          </button>
        ))}
      </div>

      {tab === 'run' && (
        <div className="evolution-layout">
          <form
            className="evolution-form"
            onSubmit={(e) => {
              e.preventDefault();
              let parsed: unknown = undefined;
              if (evidence.trim())
                try {
                  parsed = JSON.parse(evidence);
                } catch {
                  notify('The evidence snapshot is not valid JSON.', true);
                  return;
                }
              void start(
                '/runs',
                {
                  harness,
                  case_id: parsed ? null : runCase,
                  backend,
                  model,
                  evidence: parsed ?? null,
                },
                'Spec run started.',
              );
            }}
          >
            <div className="field-group">
              <span className="field-title">Harness</span>
              {overview.harnesses.map((h) => (
                <label key={h.id} className="check-row spec-option">
                  <input
                    type="radio"
                    checked={harness === h.id}
                    onChange={() => setHarness(h.id)}
                  />
                  <span>
                    {HARNESS_LABEL[h.id]}
                    <small>{HARNESS_NOTE[h.id]}</small>
                  </span>
                </label>
              ))}
            </div>
            {picker}
            <label className="field-group">
              <span className="field-title">
                Issue snapshot <small>pinned evidence from the frozen benchmark</small>
              </span>
              <select
                value={runCase}
                onChange={(e) => setCaseId(e.target.value)}
                disabled={!!evidence.trim()}
              >
                {usableCases.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.id} — {c.title}
                  </option>
                ))}
              </select>
              <small className="field-help">
                {overview.cases.find((c) => c.id === runCase)?.why}
              </small>
            </label>
            {!offline && (
              <label className="field-group">
                <span className="field-title">
                  Or paste an evidence snapshot{' '}
                  <small>EVIDENCE_COLLECTED JSON from agent.evidence</small>
                </span>
                <textarea
                  rows={3}
                  className="code-input"
                  value={evidence}
                  onChange={(e) => setEvidence(e.target.value)}
                />
              </label>
            )}
            <button className="button primary" disabled={busy || running}>
              {busy ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}
              Write spec
            </button>
            {running && <small className="field-help">Another Spec Lab job is running.</small>}
          </form>
          <JobList title="SPEC RUNS" jobs={jobsOf(['spec'])} onOpen={showJob} />
        </div>
      )}

      {tab === 'compare' && (
        <div className="evolution-layout">
          <form
            className="evolution-form"
            onSubmit={(e) => {
              e.preventDefault();
              void start(
                '/benchmarks',
                { harnesses: compareHarnesses, case_ids: compareUsable, backend, model },
                'Comparison started. It runs in the background.',
              );
            }}
          >
            <div className="eyebrow">NEW COMPARISON · FROZEN BENCHMARK</div>
            <div className="field-group">
              <span className="field-title">Harnesses</span>
              {overview.harnesses.map((h) => (
                <label key={h.id} className="check-row">
                  <input
                    type="checkbox"
                    checked={compareHarnesses.includes(h.id)}
                    onChange={(e) =>
                      setCompareHarnesses((old) =>
                        e.target.checked ? [...old, h.id] : old.filter((x) => x !== h.id),
                      )
                    }
                  />
                  <span>{HARNESS_LABEL[h.id]}</span>
                </label>
              ))}
            </div>
            {picker}
            <div className="field-group">
              <span className="field-title">
                Cases <small>scored by deterministic rubrics</small>
              </span>
              <div className="run-picker">
                {overview.cases.map((c) => (
                  <label key={c.id} className="check-row" title={c.why}>
                    <input
                      type="checkbox"
                      disabled={!covered(c.id, compareHarnesses)}
                      checked={compareCases.includes(c.id)}
                      onChange={(e) =>
                        setCompareCases((old) =>
                          e.target.checked ? [...old, c.id] : old.filter((x) => x !== c.id),
                        )
                      }
                    />
                    <span>{c.id}</span>
                    {c.must_stay_blocked && <small>must stay blocked</small>}
                  </label>
                ))}
              </div>
              <button
                type="button"
                className="text-button"
                onClick={() =>
                  setCompareCases(
                    overview.cases
                      .filter((c) => c.id !== 'bundled-fixture' || backend === 'fixture')
                      .filter((c) => covered(c.id, compareHarnesses))
                      .map((c) => c.id),
                  )
                }
              >
                Select every available case
              </button>
            </div>
            {!offline && (
              <p className="field-help">
                {`Worst case about ${compareUsable.length * compareHarnesses.length * 15} model calls. Live results are also saved to evals/results.`}
              </p>
            )}
            <button
              className="button primary"
              disabled={busy || running || !compareHarnesses.length || !compareUsable.length}
            >
              {busy ? <LoaderCircle size={15} className="spin" /> : <GitCompare size={15} />}
              Compare
            </button>
          </form>
          <div className="prompt-list">
            <JobList title="COMPARISONS" jobs={jobsOf(['benchmark'])} onOpen={showJob} />
            <div className="section-label spaced">
              RECORDED RESULTS <span>{overview.recorded.length}</span>
            </div>
            {overview.recorded.length ? (
              overview.recorded.map((r) => (
                <button key={r.name} className="prompt-row" onClick={() => showRecorded(r.name)}>
                  <GitCompare size={14} />
                  <div>
                    <strong>
                      {r.models.join(', ')} <span>{r.cases} cases</span>
                    </strong>
                    <small>
                      {Object.entries(r.summary)
                        .map(
                          ([h, s]) =>
                            `${HARNESS_LABEL[h] || h} ${s.completed}/${s.runs} · ${pct(s.mean_score)}`,
                        )
                        .join('  ·  ')}
                    </small>
                  </div>
                </button>
              ))
            ) : (
              <div className="quiet-note">No recorded comparisons yet.</div>
            )}
            <p className="field-help">The qualitative write-up is in docs/HARNESS_COMPARISON.md.</p>
          </div>
        </div>
      )}

      {tab === 'govern' && (
        <div className="evolution-layout">
          <form
            className="evolution-form"
            onSubmit={(e) => {
              e.preventDefault();
              void start(
                '/cycles',
                { case_id: cycleCase, backend, model, harness: 'prime-rlm' },
                'Evolution cycle started.',
              );
            }}
          >
            <div className="eyebrow">NEW CYCLE · PROPOSE A PROMPT REVISION</div>
            <p className="field-help">
              The Prime-RLM orchestrator researches the issue, proposes a revision of the active
              prompt from reviewer feedback, writes specs with both prompts and has a critic judge
              them. A candidate becomes active after it clears the gates and a named reviewer
              promotes it.
            </p>
            {picker}
            <label className="field-group">
              <span className="field-title">Issue snapshot</span>
              <select value={cycleCase} onChange={(e) => setCaseId(e.target.value)}>
                {cycleCases.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.id}
                    {c.id === recording?.demo_cycle && backend === 'recorded'
                      ? ' (recorded demo cycle)'
                      : ''}
                  </option>
                ))}
              </select>
              {backend === 'recorded' && (
                <small className="field-help">
                  Offline, Gate 5 can replay the benchmark for the recorded demo cycle's candidate.
                </small>
              )}
            </label>
            <button className="button primary" disabled={busy || running}>
              {busy ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}
              Start cycle
            </button>
            <div className="field-group">
              <span className="field-title">
                Gates <small>docs/GOVERNED_EVOLUTION.md</small>
              </span>
              <ol className="spec-gates">
                {gov.gates.map((g) => (
                  <li key={g.id}>
                    <strong>{g.name}.</strong> {g.rule}
                  </li>
                ))}
              </ol>
            </div>
          </form>
          <div className="prompt-list">
            <div className="section-label">
              CYCLES <span>{gov.cycles.length}</span>
            </div>
            {gov.cycles.length ? (
              gov.cycles.map((c) => (
                <button key={c.id} className="prompt-row" onClick={() => setOpenCycle(c.id)}>
                  <ShieldCheck size={14} />
                  <div>
                    <strong>
                      {c.candidate_version
                        ? `${c.candidate_version} vs ${c.baseline_version}`
                        : 'Cycle'}
                      <span>
                        {c.case_id} · {relativeDate(c.created_at)}
                      </span>
                    </strong>
                    <small>
                      {c.error ||
                        `critic ${c.critic ? 'passed' : c.critic === false ? 'failed' : '—'} · replay ${c.replay || 'not run'} · ${c.mode}`}
                    </small>
                  </div>
                  <Status status={c.status === 'needs_review' ? 'waiting' : c.status} />
                </button>
              ))
            ) : (
              <div className="quiet-note">No cycles yet.</div>
            )}
            <div className="section-label spaced">
              PROMPT VERSIONS <span>{gov.active_version} active</span>
            </div>
            {gov.versions.map((v) => (
              <div key={v.id} className="prompt-row">
                <span className="role-dot" data-role="spec" />
                <div>
                  <strong>
                    {v.id} <span>{v.status}</span>
                  </strong>
                  <small title={v.prompt}>{v.rationale || v.prompt}</small>
                </div>
                {v.status === 'retired' && (
                  <button
                    className="text-button"
                    disabled={!rollbackBy.trim()}
                    title={rollbackBy.trim() ? '' : 'Enter a reviewer name below'}
                    onClick={() => rollback(v.id)}
                  >
                    <RotateCcw size={13} /> Restore
                  </button>
                )}
              </div>
            ))}
            <input
              className="spec-reviewer"
              value={rollbackBy}
              onChange={(e) => setRollbackBy(e.target.value)}
              placeholder="Reviewer name for rollbacks"
            />
            <div className="section-label spaced">
              LEDGER{' '}
              <span className={gov.ledger_ok ? '' : 'spec-bad'}>
                {gov.ledger_ok ? 'hash chain verified' : 'hash chain broken'}
              </span>
            </div>
            {gov.audit.length ? (
              <table className="evo-table">
                <tbody>
                  {gov.audit.map((a, i) => (
                    <tr key={i}>
                      <td>{a.action.replace('_', ' ')}</td>
                      <td>{a.version || a.run_id?.slice(0, 8) || ''}</td>
                      <td>{a.reviewer || ''}</td>
                      <td>
                        <small>{relativeDate(a.at)}</small>
                      </td>
                      <td>
                        <code>{a.hash?.slice(0, 8)}</code>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="quiet-note">Nothing recorded yet.</div>
            )}
            <JobList title="REPLAYS" jobs={jobsOf(['replay', 'cycle'])} onOpen={showJob} spaced />
          </div>
        </div>
      )}

      {openJob && <JobDetail job={openJob} onClose={() => setOpenJob(null)} />}
      {openCycle && (
        <CycleDetailModal
          base={base}
          cycleId={openCycle}
          overview={overview}
          busy={!!running}
          onClose={() => setOpenCycle(null)}
          onChanged={load}
          notify={notify}
        />
      )}
    </div>
  );
}

function JobList({
  title,
  jobs,
  onOpen,
  spaced = false,
}: {
  title: string;
  jobs: Job[];
  onOpen: (id: string) => void;
  spaced?: boolean;
}) {
  return (
    <div className="prompt-list">
      <div className={`section-label ${spaced ? 'spaced' : ''}`}>
        {title} <span>{jobs.length}</span>
      </div>
      {jobs.length ? (
        jobs.map((j) => (
          <button key={j.id} className="prompt-row" onClick={() => onOpen(j.id)}>
            {j.kind === 'benchmark' || j.kind === 'replay' ? (
              <GitCompare size={14} />
            ) : (
              <FileText size={14} />
            )}
            <div>
              <strong>
                {j.harness ? HARNESS_LABEL[j.harness] || j.harness : j.kind}
                <span>
                  {j.model} · {relativeDate(j.started_at)}
                </span>
              </strong>
              <small>
                {isRunning(j.status) ? j.progress.at(-1) || 'Starting' : j.error || j.title}
              </small>
            </div>
            <Status status={j.status} />
          </button>
        ))
      ) : (
        <div className="quiet-note">Nothing yet.</div>
      )}
    </div>
  );
}
