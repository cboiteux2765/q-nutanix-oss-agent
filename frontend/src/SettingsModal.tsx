import { useEffect, useState } from 'react';
import { Check, KeyRound, LoaderCircle, ArrowUpRight, ChevronDown } from 'lucide-react';
import { Modal } from './components';
import { api, put, post, errorText } from './api';
import type { RuntimeRecommendations, Settings } from './types';

const runtimeFields = new Set([
  'context_tokens',
  'compact_at',
  'max_output_tokens',
  'max_steps',
  'max_agents',
  'max_depth',
  'max_run_tokens',
]);

export default function SettingsModal({
  settings,
  onClose,
  onSave,
}: {
  settings: Settings;
  onClose: () => void;
  onSave: (s: Settings) => void;
}) {
  const [form, setForm] = useState(settings),
    [key, setKey] = useState(''),
    [busy, setBusy] = useState(false),
    [checking, setChecking] = useState(false),
    [error, setError] = useState(''),
    [checked, setChecked] = useState(''),
    [advanced, setAdvanced] = useState(false),
    [recommendations, setRecommendations] = useState<RuntimeRecommendations | null>(null),
    [recommending, setRecommending] = useState(true);
  const field = (name: string, value: unknown) =>
    setForm((old) => ({
      ...old,
      [name]: value,
      ...(runtimeFields.has(name) ? { runtime_preset: 'custom' as const } : {}),
    }));
  useEffect(() => {
    let closed = false;
    setRecommending(true);
    const timer = setTimeout(() => {
      const query = new URLSearchParams({ model: form.model, base_url: form.base_url });
      api<RuntimeRecommendations>(`/settings/recommendations?${query}`)
        .then((value) => {
          if (closed) return;
          setRecommendations(value);
          setForm((old) => (old.runtime_preset === 'auto' ? { ...old, ...value.values } : old));
        })
        .catch((e) => {
          if (!closed) {
            setRecommendations(null);
            setError(errorText(e));
          }
        })
        .finally(() => {
          if (!closed) setRecommending(false);
        });
    }, 200);
    return () => {
      closed = true;
      clearTimeout(timer);
    };
  }, [form.model, form.base_url]);
  const modelInfo = recommendations?.model_info;
  const compactAt = Math.floor(form.context_tokens * form.compact_at);
  async function save(check = false) {
    setError('');
    setChecked('');
    check ? setChecking(true) : setBusy(true);
    try {
      const writable = [
        'api_style',
        'base_url',
        'model',
        'api_key_env',
        'reasoning_effort',
        'runtime_preset',
        'temperature',
        'max_output_tokens',
        'context_tokens',
        'compact_at',
        'max_steps',
        'max_agents',
        'max_depth',
        'max_run_tokens',
        'command_timeout',
        'repl_timeout',
        'sandbox',
        'input_price',
        'cached_input_price',
        'cache_write_price',
        'output_price',
      ];
      const payload = Object.fromEntries(
        writable.filter((k) => k in form).map((k) => [k, form[k]]),
      );
      const saved = await put<Settings>('/settings', {
        ...payload,
        ...(key ? { api_key: key } : {}),
      });
      onSave(saved);
      setKey('');
      setForm(saved);
      if (check) {
        const result = await post<Record<string, unknown>>('/settings/check');
        setChecked(String(result.message || result.text || 'Connected. Your provider is ready.'));
      } else onClose();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      setChecking(false);
    }
  }
  return (
    <Modal
      title="Settings"
      subtitle="Connect a model and set the pace of your agents."
      onClose={onClose}
    >
      <div className="settings-body">
        <label className="field-label">API protocol</label>
        <div className="segmented">
          <button
            className={form.api_style === 'responses' ? 'selected' : ''}
            onClick={() => field('api_style', 'responses')}
          >
            Responses <span>OpenAI</span>
          </button>
          <button
            className={form.api_style !== 'responses' ? 'selected' : ''}
            onClick={() => field('api_style', 'chat_completions')}
          >
            Chat Completions <span>Compatible providers</span>
          </button>
        </div>
        <div className="form-grid">
          <label>
            Model
            <input
              value={form.model}
              onChange={(e) => field('model', e.target.value)}
              placeholder="gpt-6.1-terra"
              spellCheck={false}
            />
          </label>
          <label>
            Reasoning effort
            <select
              value={form.reasoning_effort || ''}
              onChange={(e) => field('reasoning_effort', e.target.value)}
            >
              <option value="">Provider default</option>
              {['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
        </div>
        <label>
          API base URL
          <input
            value={form.base_url}
            onChange={(e) => field('base_url', e.target.value)}
            placeholder="https://api.openai.com/v1"
            spellCheck={false}
          />
        </label>
        {!form.base_url.startsWith('https://api.openai.com/') &&
          form.api_key_env === 'OPENAI_API_KEY' && (
            <p className="field-help">
              For another provider, paste its key below or change the key environment variable in
              Runtime settings. The selected variable is currently OPENAI_API_KEY.
            </p>
          )}
        <div className="key-heading">
          <label className="field-label" htmlFor="api-key">
            API key
          </label>
          {form.api_key_configured && (
            <span className="inline-success">
              <Check size={12} /> Key available
            </span>
          )}
        </div>
        <div className="input-icon">
          <KeyRound size={16} />
          <input
            id="api-key"
            type="password"
            autoComplete="off"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={
              form.api_key_configured
                ? 'Leave empty to keep your current key'
                : 'Paste a key, or use an environment variable'
            }
          />
        </div>
        <p className="field-help">
          A pasted key lasts until the server restarts.
        </p>
        <button
          className="disclosure-button"
          onClick={() => setAdvanced(!advanced)}
          aria-expanded={advanced}
        >
          Runtime settings
          <ChevronDown size={15} className={advanced ? 'rotate' : ''} />
        </button>
        {advanced && (
          <div className="advanced-settings">
            <div className="runtime-preset">
              <div>
                <strong>
                  {form.runtime_preset === 'auto' ? 'Model defaults' : 'Custom budgets'}
                </strong>
              </div>
              <button
                className="text-button"
                disabled={recommending || !recommendations}
                onClick={() =>
                  recommendations &&
                  setForm((old) => ({ ...old, ...recommendations.values, runtime_preset: 'auto' }))
                }
              >
                {recommending ? <LoaderCircle size={14} className="spin" /> : 'Use defaults'}
              </button>
            </div>
            <div className="runtime-context-note">
              {modelInfo ? (
                <>
                  <strong>{modelInfo.context_window.toLocaleString()} token model capacity</strong>
                  <p>
                    {modelInfo.standard_input_limit ? (
                      <>
                        Standard API pricing through{' '}
                        {modelInfo.standard_input_limit.toLocaleString()} input tokens per request.
                        Above that, input and cache rates are 2× and output is 1.5×.{' '}
                      </>
                    ) : null}
                    <a href={modelInfo.source_url} target="_blank" rel="noreferrer">
                      Model details <ArrowUpRight size={11} />
                    </a>
                  </p>
                </>
              ) : (
                <p>
                  Use your provider’s documented limits.
                </p>
              )}
            </div>
            <div className="form-grid">
              <label>
                Key environment variable
                <input
                  value={form.api_key_env}
                  onChange={(e) => field('api_key_env', e.target.value)}
                />
              </label>
              <label>
                Working input budget
                <input
                  type="number"
                  min={4000}
                  value={form.context_tokens}
                  onChange={(e) => field('context_tokens', Number(e.target.value))}
                />
              </label>
              <label>
                Compact at (%)
                <input
                  type="number"
                  min={10}
                  max={100}
                  step={1}
                  value={Math.round(form.compact_at * 100)}
                  onChange={(e) => field('compact_at', Number(e.target.value) / 100)}
                />
              </label>
              <label>
                Maximum output tokens
                <input
                  type="number"
                  min={256}
                  value={form.max_output_tokens}
                  onChange={(e) => field('max_output_tokens', Number(e.target.value))}
                />
              </label>
              <label>
                Maximum steps per agent
                <input
                  type="number"
                  min={1}
                  value={form.max_steps}
                  onChange={(e) => field('max_steps', Number(e.target.value))}
                />
              </label>
              <label>
                Maximum agents per run
                <input
                  type="number"
                  min={1}
                  value={form.max_agents}
                  onChange={(e) => field('max_agents', Number(e.target.value))}
                />
              </label>
              <label>
                Total run token budget
                <input
                  type="number"
                  min={1000}
                  value={form.max_run_tokens}
                  onChange={(e) => field('max_run_tokens', Number(e.target.value))}
                />
              </label>
              <label>
                Delegation depth
                <input
                  type="number"
                  min={0}
                  value={form.max_depth}
                  onChange={(e) => field('max_depth', Number(e.target.value))}
                />
              </label>
            </div>
            <p className="field-help runtime-budget-help">
              Compaction targets about <strong>{compactAt.toLocaleString()} input tokens</strong>.
              The output allowance is separate and includes reasoning. Total run usage adds up input
              and output across all agents and requests, including cached input.
            </p>
            {modelInfo?.standard_input_limit &&
              form.context_tokens > modelInfo.standard_input_limit && (
                <p className="runtime-price-note">
                  This custom input budget permits requests above the standard pricing threshold.
                  Use model defaults to target the lower tier.
                </p>
              )}
            <div className="form-grid">
              {(
                [
                  ['input_price', 'Input price'],
                  ['cached_input_price', 'Cached input price'],
                  ['cache_write_price', 'Cache write price'],
                  ['output_price', 'Output price'],
                ] as const
              ).map(([key, label]) => (
                <label key={key}>
                  {label} (USD per 1M tokens)
                  <input
                    type="number"
                    min={0}
                    step="0.01"
                    value={Number(form[key] ?? 0)}
                    onChange={(e) => field(key, Number(e.target.value))}
                  />
                </label>
              ))}
            </div>
            <p className="field-help">
              Prices are used to report spending and to enforce spending limits. Leave them at 0 if
              you don't track cost. A cache write price of 0 is treated as the input price.
            </p>
            <p className="field-help">
              Agents execute Python and shell commands locally. Connect repositories you trust.
            </p>
          </div>
        )}
        {error && (
          <div className="form-error" role="alert">
            {error}
          </div>
        )}
        {checked && (
          <div className="form-success">
            <Check size={15} />
            {checked}
          </div>
        )}
      </div>
      <div className="modal-footer">
        <button
          className="button secondary"
          disabled={busy || checking || recommending}
          onClick={() => save(true)}
        >
          {checking ? <LoaderCircle size={15} className="spin" /> : <ArrowUpRight size={15} />}Check
          connection
        </button>
        <button
          className="button primary"
          disabled={busy || checking || recommending || !form.model || !form.base_url}
          onClick={() => save()}
        >
          {busy && <LoaderCircle size={15} className="spin" />}Save settings
        </button>
      </div>
    </Modal>
  );
}
