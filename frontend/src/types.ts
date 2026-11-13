export type Project = {
  id: string;
  name: string;
  path: string;
  description?: string;
  created_at?: string;
  main_agent: { id: string; name: string; instructions: string; model: string };
};
export type Conversation = { run_id: string | null; messages: Event[]; has_more: boolean };
export type Run = {
  id: string;
  project_id: string;
  title: string;
  status: string;
  phase?: string;
  root_agent_id?: string;
  branch?: string;
  workdir?: string;
  created_at: string;
  updated_at?: string;
  token_usage?: { input?: number; output?: number; cached?: number };
  error?: string;
};
export type Agent = {
  id: string;
  run_id: string;
  parent_id?: string;
  role: string;
  name: string;
  status: string;
  task: string;
  result?: unknown;
  error?: string;
};
export type Event = {
  id: string;
  seq: number;
  run_id: string;
  agent_id?: string;
  kind: string;
  data: Record<string, unknown>;
  created_at: string;
};
// `events` is omitted by the lightweight refresh (`/runs/{id}?events=false`).
export type RunDetail = { run: Run; agents: Agent[]; events?: Event[] };
// Mirrors evo.common.ACTIVE: work is queued or in progress.
export const ACTIVE_STATES = ['queued', 'running'];
// Stopped runs that 'Resume work' can continue. Waiting runs continue with a new message.
export const RESUMABLE_STATES = ['interrupted', 'failed', 'cancelled'];
export type Settings = {
  api_style: string;
  base_url: string;
  model: string;
  api_key_env: string;
  api_key_configured: boolean;
  reasoning_effort: string;
  runtime_preset: 'auto' | 'custom';
  temperature: number | null;
  max_output_tokens: number;
  context_tokens: number;
  compact_at: number;
  max_steps: number;
  max_agents: number;
  max_depth: number;
  max_run_tokens: number;
  command_timeout: number;
  repl_timeout: number;
  sandbox: string;
  input_price: number;
  cached_input_price: number;
  cache_write_price: number;
  output_price: number;
  [key: string]: unknown;
};
export type RuntimeRecommendations = {
  values: Pick<
    Settings,
    | 'context_tokens'
    | 'compact_at'
    | 'max_output_tokens'
    | 'max_steps'
    | 'max_agents'
    | 'max_depth'
    | 'max_run_tokens'
  >;
  model_info: {
    model: string;
    context_window: number;
    max_output_tokens: number;
    standard_input_limit: number | null;
    source_url: string;
    verified_on: string;
  } | null;
};
export type Page = {
  path: string;
  title: string;
  content?: string;
  revision?: string;
  updated_at?: string;
};
export type Citation = {
  kind: 'observed' | 'inferred';
  event_id?: string;
  artifact_id?: string;
  commit?: string;
  url?: string;
};
export type Skill = {
  id: string;
  name: string;
  description?: string;
  content?: string;
  status: string;
  version?: number;
  role?: string;
  evidence?: Citation[];
  evaluation?: { experiment_id: string; passed: boolean; reasons: string[] };
};
export type Prompt = {
  id: string;
  role: string;
  content: string;
  status: string;
  version?: number;
  rationale?: string;
  created_at?: string;
};
export type Artifact = {
  id: string;
  name?: string;
  path?: string;
  title?: string;
  kind?: string;
  content?: string;
  agent_id?: string;
  created_at?: string;
};
