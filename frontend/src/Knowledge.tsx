import { useCallback, useEffect, useState } from 'react';
import {
  BookOpen,
  Plus,
  Save,
  Pencil,
  ArrowLeft,
  FileText,
  LoaderCircle,
  Sparkles,
  ArrowUpRight,
  Check,
} from 'lucide-react';
import { api, arrayOf, errorText, post, put } from './api';
import { Empty, MarkdownText, Modal, Status } from './components';
import type { Page, Project, Skill } from './types';

type Props = { project: Project; notify: (text: string, error?: boolean) => void };
/** Skills are proposed by agents or people and approved by a person. */
const skillState = (status: string) =>
  status === 'candidate' ? 'proposed' : status === 'active' ? 'approved' : status;
export function MemoryView({ project, notify }: Props) {
  const [pages, setPages] = useState<Page[]>([]),
    [page, setPage] = useState<Page | null>(null),
    [content, setContent] = useState(''),
    [editing, setEditing] = useState(false),
    [loading, setLoading] = useState(true),
    [saving, setSaving] = useState(false),
    [creating, setCreating] = useState(false),
    [path, setPath] = useState('');
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api<unknown>(`/projects/${project.id}/memory`);
      setPages(arrayOf<Page>(data, 'pages'));
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setLoading(false);
    }
  }, [project.id, notify]);
  useEffect(() => {
    setPage(null);
    void load();
  }, [load]);
  async function open(p: Page) {
    try {
      const detail = await api<Page>(
        `/projects/${project.id}/memory/${p.path.split('/').map(encodeURIComponent).join('/')}`,
      );
      setPage(detail);
      setContent(detail.content || '');
      setEditing(false);
    } catch (e) {
      notify(errorText(e), true);
    }
  }
  async function save() {
    if (!page) return;
    setSaving(true);
    try {
      const saved = await put<Page>(
        `/projects/${project.id}/memory/${page.path.split('/').map(encodeURIComponent).join('/')}`,
        { content, expected_revision: page.revision },
      );
      setPage({ ...page, ...saved, content });
      setEditing(false);
      void load();
      notify('Saved to project memory.');
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setSaving(false);
    }
  }
  return (
    <div className="knowledge-view">
      <div className="page-heading">
        <div>
          <h1>Memory</h1>
          <p>Decisions, conventions, and lessons. Written by you and your agents.</p>
        </div>
        <button className="button secondary" onClick={() => setCreating(true)}>
          <Plus size={15} />
          New page
        </button>
      </div>
      {page ? (
        <div className="document-view">
          <div className="document-toolbar">
            <button className="text-button" onClick={() => setPage(null)}>
              <ArrowLeft size={14} />
              All pages
            </button>
            <span className="mono">{page.path}</span>
            <button
              className="button small secondary"
              disabled={saving}
              onClick={() => (editing ? save() : setEditing(true))}
            >
              {saving ? (
                <LoaderCircle size={14} className="spin" />
              ) : editing ? (
                <Save size={14} />
              ) : (
                <Pencil size={14} />
              )}{' '}
              {editing ? 'Save page' : 'Edit'}
            </button>
          </div>
          {editing ? (
            <textarea
              className="document-editor"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              spellCheck={false}
              aria-label="Page Markdown"
            />
          ) : (
            <div className="document-content">
              <MarkdownText>{content || '*This page is empty.*'}</MarkdownText>
            </div>
          )}
          {page.revision && (
            <div className="document-revision">
              Git revision <code>{page.revision.slice(0, 12)}</code>
            </div>
          )}
        </div>
      ) : loading ? (
        <div className="loading-inline">
          <LoaderCircle size={18} className="spin" />
          Opening the wiki…
        </div>
      ) : pages.length ? (
        <div className="page-grid">
          {pages.map((p) => (
            <button className="page-card" key={p.path} onClick={() => open(p)}>
              <FileText size={21} />
              <h3>{p.title || p.path.replace('.md', '')}</h3>
              <span>{p.path}</span>
              <ArrowUpRight size={15} className="card-arrow" />
            </button>
          ))}
        </div>
      ) : (
        <Empty icon={<BookOpen size={28} />} title="A little knowledge goes a long way.">
          Your agents will save useful findings here as they work. Add a first page to give them a
          head start.
        </Empty>
      )}
      {creating && (
        <Modal
          title="Give an idea a home."
          subtitle="Pages are Markdown files, versioned with Git."
          onClose={() => setCreating(false)}
        >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const p = path.endsWith('.md') ? path : `${path}.md`;
              setPage({ path: p, title: path });
              setContent(`# ${path.replace(/[-_]/g, ' ')}\n\n`);
              setEditing(true);
              setCreating(false);
              setPath('');
            }}
          >
            <div className="settings-body">
              <label>
                Page path
                <input
                  autoFocus
                  required
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                  placeholder="architecture/decisions.md"
                  pattern="[a-zA-Z0-9_./ -]+"
                />
              </label>
            </div>
            <div className="modal-footer">
              <button className="button primary" type="submit">
                Create page
                <ArrowUpRight size={15} />
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}

export function SkillsView({ project, notify }: Props) {
  const [skills, setSkills] = useState<Skill[]>([]),
    [selected, setSelected] = useState<Skill | null>(null),
    [creating, setCreating] = useState(false),
    [name, setName] = useState(''),
    [description, setDescription] = useState(''),
    [content, setContent] = useState(''),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    try {
      setSkills(arrayOf<Skill>(await api(`/projects/${project.id}/skills`), 'skills'));
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setLoading(false);
    }
  }, [project.id, notify]);
  useEffect(() => {
    setSelected(null);
    void load();
  }, [load]);
  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await post(`/projects/${project.id}/skills`, { name, description, content });
      setCreating(false);
      setName('');
      setContent('');
      setDescription('');
      void load();
      notify('Skill proposed. Approve it to make it available to new runs.');
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setBusy(false);
    }
  }
  async function promote(skill: Skill) {
    setBusy(true);
    try {
      await post(`/projects/${project.id}/skills/${skill.id}/promote`);
      setSelected({ ...skill, status: 'active' });
      void load();
      notify('Skill approved for new runs.');
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="knowledge-view">
      <div className="page-heading">
        <div>
          <h1>Skills</h1>
          <p>Turn a useful procedure into something every agent can learn.</p>
        </div>
        <button className="button secondary" onClick={() => setCreating(true)}>
          <Plus size={15} />
          New skill
        </button>
      </div>
      {loading ? (
        <div className="loading-inline">
          <LoaderCircle size={18} className="spin" />
          Loading skills…
        </div>
      ) : skills.length ? (
        <div className="skill-grid">
          {skills.map((skill) => (
            <button className="skill-card" key={skill.id} onClick={() => setSelected(skill)}>
              <div className="skill-card-top">
                <span className="skill-symbol">
                  <Sparkles size={19} />
                </span>
                <Status status={skillState(skill.status)} />
              </div>
              <h3>{skill.name}</h3>
              <p>{skill.description || 'A reusable procedure for your project agents.'}</p>
              <div className="skill-card-footer">
                <span>Version {skill.version || 1}</span>
                <ArrowUpRight size={15} />
              </div>
            </button>
          ))}
        </div>
      ) : (
        <Empty icon={<Sparkles size={28} />} title="Practice makes a skill.">
          Agents can propose reusable instructions from their work. Review a candidate here and
          activate it for future runs.
        </Empty>
      )}
      {selected && (
        <Modal
          title={selected.name}
          subtitle={selected.description}
          onClose={() => setSelected(null)}
          wide
        >
          <div className="skill-detail">
            <MarkdownText>{selected.content || 'No instructions recorded.'}</MarkdownText>
            <div className="skill-evidence">
              <div className="field-title">Evidence</div>
              {selected.evidence?.length ? (
                selected.evidence.map((c, i) => (
                  <div key={i} className="citation">
                    <span className={`citation-kind ${c.kind}`}>{c.kind}</span>
                    {c.event_id && <code>event {c.event_id.slice(0, 10)}</code>}
                    {c.artifact_id && <code>artifact {c.artifact_id.slice(0, 10)}</code>}
                    {c.commit && <code>commit {c.commit.slice(0, 10)}</code>}
                    {c.url && (
                      <a href={c.url} target="_blank" rel="noreferrer">
                        {c.url}
                      </a>
                    )}
                  </div>
                ))
              ) : (
                <small className="field-help">No evidence was cited.</small>
              )}
              {selected.evaluation && (
                <small className="field-help">
                  Evaluated in experiment {selected.evaluation.experiment_id.slice(0, 8)}:{' '}
                  {selected.evaluation.passed
                    ? 'passed the gate'
                    : selected.evaluation.reasons.join(' ')}
                </small>
              )}
            </div>
          </div>
          <div className="modal-footer">
            <Status status={skillState(selected.status)} />
            {selected.status === 'candidate' && (
              <button className="button primary" disabled={busy} onClick={() => promote(selected)}>
                {busy ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}Approve
                for new runs
              </button>
            )}
          </div>
        </Modal>
      )}
      {creating && (
        <Modal
          title="Teach a useful habit."
          subtitle="New skills start as proposals for you to review."
          onClose={() => setCreating(false)}
        >
          <form onSubmit={create}>
            <div className="settings-body">
              <label>
                Name
                <input
                  autoFocus
                  required
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Investigate a regression"
                />
              </label>
              <label>
                Description
                <input
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="When should an agent use this?"
                />
              </label>
              <label>
                Instructions
                <textarea
                  required
                  rows={9}
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  placeholder="Explain the steps, useful tools, and what a good result looks like."
                />
              </label>
            </div>
            <div className="modal-footer">
              <button className="button primary" disabled={busy} type="submit">
                {busy && <LoaderCircle size={15} className="spin" />}Propose skill
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
