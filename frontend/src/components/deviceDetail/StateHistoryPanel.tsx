import { useCursorQueryState } from '../../hooks/useCursorQueryState';
import { useLifecycleIncidents } from '../../hooks/useLifecycle';
import type { LifecycleIncidentRead } from '../../types';
import { Badge } from '../ui/Badge';
import { Card } from '../ui/Card';
import { CursorPagination } from '../ui/CursorPagination';
import { incidentToneFromEventType } from '../dashboard/dashboardSummary';
import { formatDate } from './utils';

function eventBadge(incident: LifecycleIncidentRead) {
  // Label text is the backend's (LIFECYCLE_INCIDENT_LABELS), so the label can't drift
  // from the endpoint's types. Tone is shared with the dashboard so one event never
  // shows two severities, but unlike the label, that isn't caught by the type system —
  // the drift guard is dashboardSummary.test.ts's tone-coverage test instead.
  return <Badge tone={incidentToneFromEventType(incident.event_type)}>{incident.label}</Badge>;
}

type Props = {
  deviceId: string;
};

export function StateHistoryPanel({ deviceId }: Props) {
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
    <Card padding="none" className="overflow-hidden">
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
                  <td className="px-5 py-3 text-sm">{eventBadge(incident)}</td>
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
    </Card>
  );
}
