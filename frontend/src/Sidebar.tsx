import {
  BookOpen,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardList,
  Code2,
  FileCheck2,
  FlaskConical,
  FolderGit2,
  PanelLeftClose,
  Plus,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  X,
} from 'lucide-react';
import { Logo, Status } from './components';
import { ACTIVE_STATES, type Agent, type Project, type Settings } from './types';
import './Sidebar.css';

export type View =
  'workspace' | 'conversation' | 'agents' | 'memory' | 'skills' | 'evolution' | 'spec';

export const RESOURCE_TABS = [
  { id: 'memory' as View, label: 'Memory', icon: BookOpen },
  { id: 'skills' as View, label: 'Skills', icon: Sparkles },
  { id: 'evolution' as View, label: 'Evolution', icon: FlaskConical },
  { id: 'spec' as View, label: 'Spec Lab', icon: FileCheck2 },
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
  collapsed: boolean;
  onToggleCollapse: () => void;
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
  historyRole: string | null;
  onPhaseHistory: (role: string) => void;
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
  const helperRoles = new Set(
    crew
      .map((agent) => agent.role)
      .filter((role) => !CORE_ROLES.some((slot) => slot.role === role)),
  );
  return (
    <aside
      id="workspace-sidebar"
      className={`sidebar ${props.open ? 'open' : ''} ${props.collapsed ? 'collapsed' : ''}`}
      aria-label="Sidebar"
    >
      <div className="brand">
        <button
          type="button"
          className="sidebar-brand-link"
          aria-label="Go to dashboard"
          aria-current={view === 'workspace' ? 'page' : undefined}
          title="Go to dashboard"
          onClick={() => props.onView('workspace')}
        >
          <Logo small />
          <span>evo</span>
        </button>
        <button
          type="button"
          className="icon-button sidebar-collapse"
          aria-label="Collapse sidebar"
          aria-controls="workspace-sidebar"
          aria-expanded={!props.collapsed}
          title="Collapse sidebar"
          onClick={props.onToggleCollapse}
        >
          <PanelLeftClose size={18} />
        </button>
        <button
          type="button"
          className="icon-button mobile-menu"
          aria-label="Close sidebar"
          aria-controls="workspace-sidebar"
          aria-expanded={props.open}
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
            <small>Project conversation</small>
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
      <div className="crew-label">
        Your team{' '}
        {crew.some((agent) => ACTIVE_STATES.includes(agent.status)) && <span>Working</span>}
      </div>
      <nav className="agent-roster" aria-label="Agents">
        {CORE_ROLES.map(({ role, name, description, icon: Icon }) => {
          const working = crew.filter(
            (agent) => agent.role === role && ACTIVE_STATES.includes(agent.status),
          ).length;
          const selected = view === 'agents' && props.historyRole === role;
          return (
            <button
              key={role}
              type="button"
              className={`crew-link ${selected ? 'selected' : ''}`}
              aria-label={`${name} agent history`}
              aria-describedby={`sidebar-phase-${role}-status`}
              aria-current={selected ? 'page' : undefined}
              onClick={() => props.onPhaseHistory(role)}
              title={`View ${name} agent history`}
            >
              <span className="crew-icon" data-role={role}>
                <Icon size={17} />
              </span>
              <span>
                <strong>{name}</strong>
                <small id={`sidebar-phase-${role}-status`}>
                  {working ? `${working} active` : description}
                </small>
              </span>
              <ChevronRight className="sidebar-phase-chevron" size={14} aria-hidden="true" />
            </button>
          );
        })}
        {Array.from(helperRoles, (role) => {
          const members = crew.filter((agent) => agent.role === role);
          return members.map((agent, index) => (
            <button
              key={agent.id}
              className={`crew-link ${props.openAgentId === agent.id ? 'selected' : ''}`}
              onClick={() => props.onOpenAgent(agent.id)}
              title={agent.task}
            >
              <span className="crew-icon" data-role={role}>
                <Bot size={17} />
              </span>
              <span>
                <strong>Helper{members.length > 1 ? ` ${index + 1}` : ''}</strong>
                <small>
                  {ACTIVE_STATES.includes(agent.status)
                    ? 'Working on it'
                    : agent.status === 'completed'
                      ? 'Work completed'
                      : agent.status}
                </small>
              </span>
              <Status status={agent.status} compact />
            </button>
          ));
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
