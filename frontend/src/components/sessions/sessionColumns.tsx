import { Link } from 'react-router-dom';
import { ChevronRight, Copy } from 'lucide-react';
import { toast } from 'sonner';
import { StatusBadge } from '../StatusBadge';
import { PlatformIcon } from '../PlatformIcon';
import { Badge } from '../ui/Badge';
import type { DataTableColumn } from '../ui/DataTable';
import type { SessionDetail, SessionSortKey } from '../../types';
import { formatDateTime, formatDuration, formatRelativeTime } from '../../utils/dateFormatting';

function formatSessionIdentifier(sessionId: string): string {
  if (sessionId.length <= 18) return sessionId;
  return `${sessionId.slice(0, 8)}...${sessionId.slice(-6)}`;
}

async function copySessionId(sessionId: string): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(sessionId);
      toast.success('Session ID copied');
      return;
    }
  } catch {
    // fall through
  }
  const input = document.createElement('textarea');
  input.value = sessionId;
  input.setAttribute('readonly', '');
  input.style.position = 'absolute';
  input.style.left = '-9999px';
  document.body.appendChild(input);
  input.select();
  try {
    document.execCommand('copy');
    toast.success('Session ID copied');
  } catch {
    toast.error('Could not copy session ID');
  } finally {
    document.body.removeChild(input);
  }
}

interface SessionColumnsOptions {
  /** Omit the device column (use in run-scoped views where devices are shown separately). */
  hideDevice?: boolean;
  /** Omit the platform column (use in device-scoped views where platform is already known). */
  hidePlatform?: boolean;
}

export function buildSessionColumns(
  options: SessionColumnsOptions = {},
): DataTableColumn<SessionDetail, SessionSortKey>[] {
  const { hideDevice = false, hidePlatform = false } = options;

  const cols: DataTableColumn<SessionDetail, SessionSortKey>[] = [
    {
      key: 'session_id',
      header: 'Session ID',
      sortKey: 'session_id',
      render: (s) => {
        return (
          <div className="flex items-center gap-2">
            <div className="space-y-0.5">
              <span className="font-mono text-sm text-text-2" title={s.session_id}>
                {formatSessionIdentifier(s.session_id)}
              </span>
            </div>
            <button
              type="button"
              onClick={() => void copySessionId(s.session_id)}
              className="rounded p-1 text-text-3 hover:bg-surface-2 hover:text-accent-hover"
              aria-label={`Copy session ID ${s.session_id}`}
              title="Copy full session ID"
            >
              <Copy size={14} />
            </button>
          </div>
        );
      },
    },
  ];

  if (!hideDevice) {
    cols.push({
      key: 'device',
      header: 'Device',
      sortKey: 'device',
      render: (s) => {
        if (s.device_name && s.device_id) {
          return (
            <Link to={`/devices/${s.device_id}`} className="text-accent hover:underline text-sm">
              {s.device_name}
            </Link>
          );
        }
        return <span className="text-text-3 text-sm">-</span>;
      },
    });
  }

  cols.push({
    key: 'test_name',
    header: 'Test Name',
    sortKey: 'test_name',
    render: (s) => {
      if (s.is_probe) {
        return (
          <div className="space-y-0.5">
            <Badge tone="neutral" size="sm">probe</Badge>
            {s.probe_checked_by && (
              <p className="text-xs text-text-3">{s.probe_checked_by}</p>
            )}
          </div>
        );
      }
      return (
        <div className="space-y-0.5">
          <p className="text-sm text-text-2">{s.test_name ?? '-'}</p>
        </div>
      );
    },
  });

  if (!hidePlatform) {
    cols.push({
      key: 'platform',
      header: 'Platform',
      sortKey: 'platform',
      render: (s) => {
        const platformId = s.device_platform_id;
        const platformLabel = s.device_platform_label ?? null;
        return platformId ? <PlatformIcon platformId={platformId} platformLabel={platformLabel} /> : <span className="text-text-3 text-sm">-</span>;
      },
    });
  }

  cols.push(
    {
      key: 'started_at',
      header: 'Started',
      sortKey: 'started_at',
      render: (s) => (
        <div className="space-y-0.5">
          <p className="font-medium text-text-2 text-sm">{formatRelativeTime(s.started_at)}</p>
          <p className="text-xs text-text-3">{formatDateTime(s.started_at)}</p>
        </div>
      ),
    },
    {
      key: 'duration',
      header: 'Duration',
      sortKey: 'duration',
      render: (s) => <span className="text-sm text-text-3">{formatDuration(s.started_at, s.ended_at)}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      sortKey: 'status',
      render: (s) => <StatusBadge status={s.status} />,
    },
  );

  return cols;
}

/** Leading chevron column that toggles the capabilities expansion. Shared by the
 * Active and History tables; expansion state lives in the section component. */
export function buildExpanderColumn(
  isExpanded: (s: SessionDetail) => boolean,
  onToggle: (s: SessionDetail) => void,
): DataTableColumn<SessionDetail, SessionSortKey> {
  return {
    key: 'expand',
    header: <span className="sr-only">Expand</span>,
    width: '2.5rem',
    render: (s) => (
      <button
        type="button"
        onClick={() => onToggle(s)}
        className="rounded p-1 text-text-3 hover:bg-surface-2 hover:text-text-1"
        aria-label={isExpanded(s) ? 'Collapse capabilities' : 'Expand capabilities'}
        aria-expanded={isExpanded(s)}
      >
        <ChevronRight size={16} className={`transition-transform ${isExpanded(s) ? 'rotate-90' : ''}`} />
      </button>
    ),
  };
}
