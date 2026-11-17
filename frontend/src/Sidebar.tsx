import {
  BookOpen,
  Bot,
  Check,
  ChevronDown,
  ClipboardList,
  Code2,
  FlaskConical,
  FolderGit2,
  Plus,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  X,
} from 'lucide-react';
import { Logo, Status } from './components';
import { ACTIVE_STATES, type Agent, type Project, type Settings } from './types';

export type View = 'conversation' | 'memory' | 'skills' | 'evolution';

export const RESOURCE_TABS = [
  { id: 'memory' as View, label: 'Memory', icon: BookOpen },
  { id: 'skills' as View, label: 'Skills', icon: Sparkles },
  { id: 'evolution' as View, label: 'Evolution', icon: FlaskConical },
];

const CORE_ROLES = [
  { role: 'spec', name: 'Spec', description: 'Understand & plan', icon: ClipboardList },
  { role: 'code', name: 'Code', description: 'Build the change', icon: Code2 },
  { role: 'test', name: 'Test', description: 'Verify the behavior', icon: FlaskConical },
  { role: 'qa', name: 'QA', description: 'Independent review', icon: ShieldCheck },
];

type Props = {
  open: boolean;
  onClose: () => void;
  projects: Project[];
  project: Project | null;
  projectMenu: boolean;
  setProjectMenu: (open: boolean) => void;
  onSelectProject: (project: Project) => void;
  onConnectProject: () => void;
  onSetup: () => void;
  view: View;
  onView: (view: View) => void;
  mainName: string;
  active: boolean;
  /** The main agent is open (no team member selected). */
  mainSelected: boolean;
  onMainAgent: () => void;
  onConfigureAgent: () => void;
  agents: Agent[];
  /** The team member whose work is open, if any. */
  openAgentId: string | null;
  onOpenAgent: (id: string) => void;
  settings: Settings | null;
  onSettings: () => void;
  /** What background evolution is doing, if anything. */
  evolving?: string | null;
};

export default function Sidebar(props: Props) {
  const { project, projects, projectMenu, setProjectMenu, view, active, settings } = props;
  const crew = props.agents.filter((a) => a.role !== 'orchestrator');
  const helperRoles = Array.from(
    new Set(crew.map((a) => a.role).filter((r) => !CORE_ROLES.some((slot) => slot.role === r))),
  );
  const roleSlots = [
    ...CORE_ROLES,
    ...helperRoles.map((role) => ({
      role,
      name: 'Helper',
      description: 'Specialist support',
      icon: Bot,
    })),
  ];
  return (
    <aside className={`sidebar ${props.open ? 'open' : ''}`} aria-label="Sidebar">
      <div className="brand">
        <Logo small />
        <span>evo</span>
        <button
          className="icon-button mobile-menu"
          aria-label="Close sidebar"
          onClick={props.onClose}
        >
          <X size={18} />
        </button>
      </div>
      <div className="project-selector-wrap">
        <button
          className="project-selector"
          onClick={() => (projects.length ? setProjectMenu(!projectMenu) : props.onSetup())}
          aria-expanded={projectMenu}
          title={project?.path}
        >
          <FolderGit2 size={17} />
          <span>
            <strong>{project?.name || 'Connect a project'}</strong>
            <small>{project ? 'Local repository' : 'Set up your workspace'}</small>
          </span>
          <ChevronDown size={14} />
        </button>
        {projectMenu && (
          <div className="project-menu">
            <div className="menu-caption">Your projects</div>
            {projects.map((p) => (
              <button key={p.id} onClick={() => props.onSelectProject(p)} title={p.path}>
                <FolderGit2 size={14} />
                <span>{p.name}</span>
                {p.id === project?.id && <Check size={13} />}
              </button>
            ))}
            <button
              onClick={() => {
                setProjectMenu(false);
                props.onConnectProject();
              }}
            >
              <Plus size={14} />
              Connect a repository
            </button>
          </div>
        )}
      </div>
      <div className="main-agent-nav">
        <button
          className={`main-agent-link ${props.mainSelected ? 'selected' : ''}`}
          onClick={props.onMainAgent}
        >
          <Logo small />
          <span>
            <strong>{props.mainName}</strong>
            <small>Your main agent</small>
          </span>
          {active && <span className="live-dot" />}
        </button>
        {project && (
          <button
            className="icon-button agent-settings"
            title="Configure main agent"
            aria-label="Configure main agent"
            onClick={props.onConfigureAgent}
          >
            <SlidersHorizontal size={14} />
          </button>
        )}
      </div>
      <div className="crew-label">Your team {active && <span>Working</span>}</div>
      <nav className="agent-roster" aria-label="Agents">
        {roleSlots.map((slot) => {
          const members = crew.filter((a) => a.role === slot.role);
          return members.length ? (
            members.map((a, index) => (
              <button
                key={a.id}
                className={`crew-link ${props.openAgentId === a.id ? 'selected' : ''}`}
                onClick={() => props.onOpenAgent(a.id)}
                title={a.task}
              >
                <span className="crew-icon" data-role={slot.role}>
                  <slot.icon size={17} />
                </span>
                <span>
                  <strong>
                    {slot.name}
                    {members.length > 1 ? ` ${index + 1}` : ''}
                  </strong>
                  <small>
                    {ACTIVE_STATES.includes(a.status)
                      ? 'Working on it'
                      : a.status === 'completed'
                        ? 'Work completed'
                        : a.status}
                  </small>
                </span>
                <Status status={a.status} compact />
              </button>
            ))
          ) : (
            <div key={slot.role} className="crew-link idle">
              <span className="crew-icon" data-role={slot.role}>
                <slot.icon size={17} />
              </span>
              <span>
                <strong>{slot.name}</strong>
                <small>{slot.description}</small>
              </span>
              <span className="idle-dot" />
            </div>
          );
        })}
      </nav>
      <div className="sidebar-bottom">
        <nav className="sidebar-nav" aria-label="Project resources">
          {RESOURCE_TABS.map((t) => (
            <button
              key={t.id}
              className={view === t.id ? 'active' : ''}
              onClick={() => props.onView(t.id)}
            >
              <t.icon size={16} />
              <span>{t.label}</span>
              {t.id === 'evolution' && props.evolving && (
                <i
                  className="nav-activity"
                  title={`Evolution: ${props.evolving}`}
                  aria-label="Experiment running"
                />
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-divider" />
        <button className="settings-button" onClick={props.onSetup}>
          <FolderGit2 size={16} />
          <span>Project setup</span>
        </button>
        <button className="settings-button" onClick={props.onSettings} disabled={!settings}>
          <Settings2 size={16} />
          <span>Settings</span>
          <span className={`connection-dot ${settings?.api_key_configured ? 'ready' : ''}`} />
        </button>
      </div>
    </aside>
  );
}
