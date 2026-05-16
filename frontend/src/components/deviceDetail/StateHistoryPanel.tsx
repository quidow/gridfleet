import { useCursorQueryState } from '../../hooks/useCursorQueryState';
import { useLifecycleIncidents } from '../../hooks/useLifecycle';
import type { LifecycleIncidentRead } from '../../types';
import Badge, { type BadgeTone } from '../ui/Badge';
import CursorPagination from '../ui/CursorPagination';
import { formatDate } from './utils';

const EVENT_BADGE_MAP: Record<string, { label: string; tone: BadgeTone }> = {
  lifecycle_auto_stopped: { label: 'Stopped', tone: 'critical' },
  lifecycle_deferred_stop: { label: 'Deferred Stop', tone: 'warning' },
  lifecycle_recovery_suppressed: { label: 'Recovery Blocked', tone: 'warning' },
  lifecycle_recovery_failed: { label: 'Recovery Failed', tone: 'critical' },
  lifecycle_recovery_backoff: { label: 'Backoff', tone: 'warning' },
  lifecycle_recovered: { label: 'Recovered', tone: 'success' },
  lifecycle_run_excluded: { label: 'Run Excluded', tone: 'critical' },
  lifecycle_run_restored: { label: 'Run Restored', tone: 'success' },
  health_check_fail: { label: 'Health Fail', tone: 'critical' },
  connectivity_lost: { label: 'Disconnected', tone: 'critical' },
  connectivity_restored: { label: 'Connected', tone: 'success' },
  node_crash: { label: 'Node Crash', tone: 'critical' },
  node_restart: { label: 'Node Restart', tone: 'info' },
  hardware_health_changed: { label: 'Hardware', tone: 'warning' },
};

function eventBadge(eventType: string) {
  const badge = EVENT_BADGE_MAP[eventType] ?? { label: eventType, tone: 'neutral' as BadgeTone };
  return <Badge tone={badge.tone}>{badge.label}</Badge>;
}

type Props = {
  deviceId: string;
};

export default function StateHistoryPanel({ deviceId }: Props) {
  const { pageSize, cursor, direction, setPageSize, goOlder, goNewer, resetToNewest } =
    useCursorQueryState({ defaultPageSize: 25 });

  const { data, isLoading } = useLifecycleIncidents({
    device_id: deviceId,
    limit: pageSize,
    cursor: cursor || undefined,
    direction: cursor ? direction : undefined,
  });

  const incidents = data?.items ?? [];
  const isNewestPage = !cursor;

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface-1 shadow-sm">
      <div className="border-b border-border px-5 py-4">
        <h2 className="text-sm font-semibold text-text-1">State History</h2>
      </div>
      {isLoading ? (
        <div className="px-5 py-8 text-center text-sm text-text-2">Loading…</div>
      ) : incidents.length === 0 && isNewestPage ? (
        <div className="px-5 py-4">
          <p className="rounded-lg border border-dashed border-border-strong bg-surface-2 px-4 py-4 text-center text-sm text-text-2">
            No lifecycle events recorded.
          </p>
        </div>
      ) : (
        <>
          <table className="min-w-full divide-y divide-border">
            <thead className="bg-surface-2">
              <tr>
                <th className="px-5 py-3 text-left text-xs font-medium uppercase text-text-2">Event</th>
                <th className="px-5 py-3 text-left text-xs font-medium uppercase text-text-2">Reason</th>
                <th className="px-5 py-3 text-left text-xs font-medium uppercase text-text-2">Source</th>
                <th className="px-5 py-3 text-left text-xs font-medium uppercase text-text-2">Run</th>
                <th className="px-5 py-3 text-left text-xs font-medium uppercase text-text-2">Time</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {incidents.map((incident: LifecycleIncidentRead) => (
                <tr key={incident.id} className="hover:bg-surface-2">
                  <td className="px-5 py-3 text-sm">{eventBadge(incident.event_type)}</td>
                  <td className="max-w-xs truncate px-5 py-3 text-sm text-text-1" title={incident.reason ?? ''}>
                    {incident.reason ?? '-'}
                  </td>
                  <td className="px-5 py-3 text-sm text-text-2">{incident.source ?? '-'}</td>
                  <td className="px-5 py-3 text-sm text-text-2">{incident.run_name ?? '-'}</td>
                  <td className="whitespace-nowrap px-5 py-3 text-sm text-text-2">{formatDate(incident.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <CursorPagination
            pageSize={pageSize}
            nextCursor={data?.next_cursor ?? null}
            prevCursor={data?.prev_cursor ?? null}
            isNewestPage={isNewestPage}
            onOlder={goOlder}
            onNewer={goNewer}
            onBackToNewest={resetToNewest}
            onPageSizeChange={setPageSize}
          />
        </>
      )}
    </div>
  );
}
