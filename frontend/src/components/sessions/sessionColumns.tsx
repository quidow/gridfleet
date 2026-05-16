import { Link } from 'react-router-dom';
import { Copy } from 'lucide-react';
import { toast } from 'sonner';
import { StatusBadge } from '../StatusBadge';
import { PlatformIcon } from '../PlatformIcon';
import Badge from '../ui/Badge';
import type { DataTableColumn } from '../ui/DataTable';
import type { SessionDetail, SessionSortKey } from '../../types';
import { CONNECTION_TYPE_LABELS, DEVICE_TYPE_LABELS, resolvePlatformLabel } from '../../lib/labels';
import { formatDateTime, formatRelativeTime } from '../../utils/dateFormatting';

function duration(start: string, end: string | null): string {
  const endMs = end ? new Date(end).getTime() : Date.now();
  const diff = endMs - new Date(start).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ${secs % 60}s`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}

function formatSessionIdentifier(sessionId: string): string {
  if (sessionId.length <= 18) return sessionId;
  return `${sessionId.slice(0, 8)}...${sessionId.slice(-6)}`;
}

function isSetupFailureSession(session: SessionDetail): boolean {
  return session.device_id === null && session.status === 'error';
}

function buildRequestedLaneSummary(session: SessionDetail): string | null {
  const parts: string[] = [];
  if (session.requested_platform_id) {
    parts.push(resolvePlatformLabel(session.requested_platform_id, null));
  }
  if (session.requested_device_type) {
    parts.push(DEVICE_TYPE_LABELS[session.requested_device_type]);
  }
  if (session.requested_connection_type) {
    parts.push(CONNECTION_TYPE_LABELS[session.requested_connection_type]);
  }
  return parts.length > 0 ? parts.join(' • ') : null;
}

function buildFailureSummary(session: SessionDetail): string | null {
  if (!session.error_type && !session.error_message) return null;
  if (session.error_type && session.error_message) {
    return `${session.error_type}: ${session.error_message}`;
  }
  return session.error_type ?? session.error_message;
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
}

export function buildSessionColumns(
  options: SessionColumnsOptions = {},
): DataTableColumn<SessionDetail, SessionSortKey>[] {
  const { hideDevice = false } = options;

  const cols: DataTableColumn<SessionDetail, SessionSortKey>[] = [
    {
      key: 'session_id',
      header: 'Session ID',
      sortKey: 'session_id',
      render: (s) => {
        const setupFailure = isSetupFailureSession(s);
        return (
          <div className="flex items-center gap-2">
            <div className="space-y-0.5">
              {setupFailure && <p className="text-xs uppercase tracking-wide text-text-3">Synthetic ID</p>}
              <span
                className={`font-mono text-sm ${setupFailure ? 'text-text-3' : 'text-text-2'}`}
                title={s.session_id}
              >
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
        const laneSummary = buildRequestedLaneSummary(s);
        if (s.device_name && s.device_id) {
          return (
            <Link to={`/devices/${s.device_id}`} className="text-accent hover:underline text-sm">
              {s.device_name}
            </Link>
          );
        }
        if (isSetupFailureSession(s)) {
          return (
            <div className="space-y-0.5">
              <p className="text-sm font-medium text-text-1">Setup failure</p>
              <p className="text-xs text-text-3">{laneSummary ?? 'Requested lane unavailable'}</p>
            </div>
          );
        }
        return <span className="text-text-3 text-sm">-</span>;
      },
    });
  }

  cols.push(
    {
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
        const failureSummary = isSetupFailureSession(s) ? buildFailureSummary(s) : null;
        return (
          <div className="space-y-0.5">
            <p className="text-sm text-text-2">{s.test_name ?? '-'}</p>
            {failureSummary && <p className="text-xs text-danger-foreground">{failureSummary}</p>}
          </div>
        );
      },
    },
    {
      key: 'platform',
      header: 'Platform',
      sortKey: 'platform',
      render: (s) => {
        const platformId = s.device_platform_id ?? s.requested_platform_id;
        const platformLabel = s.device_platform_label ?? null;
        return platformId ? <PlatformIcon platformId={platformId} platformLabel={platformLabel} /> : <span className="text-text-3 text-sm">-</span>;
      },
    },
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
      render: (s) => <span className="text-sm text-text-3">{duration(s.started_at, s.ended_at)}</span>,
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
