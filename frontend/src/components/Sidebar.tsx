import { NavLink } from 'react-router-dom';
import { LayoutDashboard, Smartphone, Server, Clock, FolderOpen, Package, Bell, Play, BarChart3, Settings2, PanelLeftClose, PanelLeftOpen, LogOut, Moon, Sun } from 'lucide-react';
import { useAuth } from '../context/auth';
import { useSidebar } from '../context/SidebarContext';
import { useTheme } from '../context/theme';
import { useDevices } from '../hooks/useDevices';
import { useHosts } from '../hooks/useHosts';
import { useRuns } from '../hooks/useRuns';
import type { RunState } from '../types';

type CountTone = 'neutral' | 'warn';
type NavLinkDef = {
  to: string;
  label: string;
  icon: React.ElementType;
  count?: number;
  countTone?: CountTone;
};

type NavGroup = {
  title: string;
  links: NavLinkDef[];
};

const ACTIVE_RUN_STATES: RunState[] = ['pending', 'preparing', 'ready', 'active', 'completing'];

function AppMark() {
  return (
    <svg
      role="img"
      aria-label="GridFleet mark"
      width="30"
      height="30"
      viewBox="0 0 30 30"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="shrink-0"
    >
      <defs>
        <linearGradient id="app-mark-gradient" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#14b8a6" />
          <stop offset="100%" stopColor="#0d9488" />
        </linearGradient>
      </defs>
      <rect x="0" y="0" width="30" height="30" rx="8" fill="url(#app-mark-gradient)" />
      <g
        stroke="#ffffff"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
        transform="translate(7 7)"
      >
        <rect x="3" y="0.5" width="10" height="15" rx="2" />
        <line x1="8" y1="12.5" x2="8.01" y2="12.5" />
      </g>
    </svg>
  );
}

function CountPill({ count, tone }: { count: number; tone: CountTone }) {
  const toneClass =
    tone === 'warn'
      ? 'bg-warning-strong text-[#111827]'
      : 'bg-sidebar-hover-bg text-sidebar-text';
  return (
    <span
      aria-hidden="true"
      className={`ml-auto rounded-full px-1.5 py-[1px] text-[10px] font-medium tabular-nums ${toneClass}`}
    >
      {count}
    </span>
  );
}

function NavItem({ to, label, icon: Icon, count, countTone = 'neutral', collapsed }: NavLinkDef & { collapsed: boolean }) {
  return (
    <NavLink
      to={to}
      end={to === '/'}
      title={count != null ? `${label} (${count})` : label}
      className={({ isActive }) =>
        `flex items-center gap-2.5 rounded-md border-l-[3px] py-2 text-sm font-medium transition-colors ${
          collapsed ? 'justify-center px-2' : 'px-2.5'
        } ${
          isActive
            ? 'border-accent bg-sidebar-active-bg text-sidebar-heading'
            : 'border-transparent hover:bg-sidebar-hover-bg hover:text-sidebar-heading'
        }`
      }
    >
      {({ isActive }) => (
        <>
          <Icon size={17} className={`shrink-0 ${isActive ? 'text-accent' : ''}`} />
          {!collapsed && <span className="whitespace-nowrap">{label}</span>}
          {!collapsed && count != null && <CountPill count={count} tone={countTone} />}
        </>
      )}
    </NavLink>
  );
}

function NavGroupHeading({ title }: { title: string }) {
  return (
    <p className="px-3 pb-1 pt-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-sidebar-text-muted">
      {title}
    </p>
  );
}

export default function Sidebar() {
  const { collapsed, toggle } = useSidebar();
  const auth = useAuth();
  const { mode, toggle: toggleTheme } = useTheme();

  const devicesQuery = useDevices();
  const hostsQuery = useHosts();
  const runsQuery = useRuns();

  const deviceCount = devicesQuery.data?.length;
  const hostCount = hostsQuery.data?.length;
  const runItems = runsQuery.data?.items;
  const activeRunCount = Array.isArray(runItems)
    ? runItems.filter((r) => ACTIVE_RUN_STATES.includes(r.state)).length
    : undefined;

  const groups: NavGroup[] = [
    {
      title: 'Operate',
      links: [
        { to: '/', label: 'Dashboard', icon: LayoutDashboard },
        { to: '/devices', label: 'Devices', icon: Smartphone, count: deviceCount },
        { to: '/groups', label: 'Device Groups', icon: FolderOpen },
        { to: '/hosts', label: 'Hosts', icon: Server, count: hostCount },
        { to: '/drivers', label: 'Drivers', icon: Package },
      ],
    },
    {
      title: 'Automate',
      links: [
        {
          to: '/runs',
          label: 'Test Runs',
          icon: Play,
          count: activeRunCount && activeRunCount > 0 ? activeRunCount : undefined,
          countTone: 'warn',
        },
        { to: '/sessions', label: 'Sessions', icon: Clock },
        { to: '/analytics', label: 'Analytics', icon: BarChart3 },
      ],
    },
    {
      title: 'System',
      links: [
        { to: '/notifications', label: 'Notifications', icon: Bell },
        { to: '/settings', label: 'Settings', icon: Settings2 },
      ],
    },
  ];

  return (
    <aside
      className={`${collapsed ? 'w-16' : 'w-60'} flex h-full shrink-0 flex-col overflow-hidden border-r border-sidebar-border bg-sidebar-surface text-sidebar-text transition-all duration-200`}
    >
      <div className={`flex items-center border-b border-sidebar-border ${collapsed ? 'justify-center px-2 py-5' : 'justify-between px-5 py-5'}`}>
        {!collapsed ? (
          <div className="flex items-center gap-2.5">
            <AppMark />
            <h1 className="whitespace-nowrap font-display text-[15px] font-semibold text-sidebar-heading">GridFleet</h1>
          </div>
        ) : (
          <AppMark />
        )}
        <button
          onClick={toggle}
          className="text-sidebar-text-muted transition-colors hover:text-sidebar-heading"
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
        </button>
      </div>
      <nav className={`flex-1 ${collapsed ? 'px-1.5' : 'px-3'} py-4`}>
        {groups.map((group, idx) => (
          <div
            key={group.title}
            className={idx === 0 ? 'space-y-1' : 'mt-5 space-y-1'}
          >
            {!collapsed && <NavGroupHeading title={group.title} />}
            {group.links.map((link) => (
              <NavItem key={link.to} {...link} collapsed={collapsed} />
            ))}
          </div>
        ))}
      </nav>
      <div className={`space-y-1 border-t border-sidebar-border ${collapsed ? 'px-1.5 py-3' : 'px-3 py-4'}`}>
        <button
          type="button"
          title={mode === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          onClick={toggleTheme}
          className={`flex w-full items-center gap-3 rounded-md py-2 text-sm font-medium text-sidebar-text transition-colors hover:bg-sidebar-hover-bg hover:text-sidebar-heading ${
            collapsed ? 'justify-center px-2' : 'px-3'
          }`}
        >
          {mode === 'dark' ? <Sun size={18} className="shrink-0" /> : <Moon size={18} className="shrink-0" />}
          {!collapsed ? <span>{mode === 'dark' ? 'Light mode' : 'Dark mode'}</span> : null}
        </button>
        {auth.enabled ? (
          <>
            {!collapsed ? (
              <div className="mb-1 mt-3 px-3">
                <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-sidebar-text-muted">Signed in</p>
                <p className="mt-1 truncate text-sm text-sidebar-heading">{auth.username ?? 'Operator'}</p>
              </div>
            ) : null}
            <button
              type="button"
              title="Log out"
              onClick={() => {
                void auth.logout();
              }}
              className={`flex w-full items-center gap-3 rounded-md py-2 text-sm font-medium text-sidebar-text transition-colors hover:bg-sidebar-hover-bg hover:text-sidebar-heading ${
                collapsed ? 'justify-center px-2' : 'px-3'
              }`}
            >
              <LogOut size={18} className="shrink-0" />
              {!collapsed ? <span>Log out</span> : null}
            </button>
          </>
        ) : null}
      </div>
    </aside>
  );
}
