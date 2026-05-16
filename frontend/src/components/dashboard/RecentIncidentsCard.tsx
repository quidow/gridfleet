import { Link } from 'react-router-dom';
import { useDevices } from '../../hooks/useDevices';
import { useRecentLifecycleIncidents } from '../../hooks/useLifecycle';
import { LifecyclePolicyBadge } from '../LifecyclePolicyBadge';
import Badge from '../ui/Badge';
import Card from '../ui/Card';
import FetchError from '../ui/FetchError';
import SectionSkeleton from '../ui/SectionSkeleton';
import { deriveDashboardFleetSummary, groupLifecycleIncidents, incidentToneFromEventType } from './dashboardSummary';

const MAX_INCIDENTS = 4;
const MAX_AFFECTED = 3;

function compactRelativeTime(isoString: string): string {
  const diffMs = Date.now() - new Date(isoString).getTime();
  const absS = Math.round(Math.abs(diffMs) / 1000);
  if (absS < 60) return `${absS}s`;
  const absM = Math.round(absS / 60);
  if (absM < 60) return `${absM}m`;
  const absH = Math.round(absM / 60);
  if (absH < 24) return `${absH}h`;
  return `${Math.round(absH / 24)}d`;
}

export default function RecentIncidentsCard() {
  const devicesQuery = useDevices();
  const incidentsQuery = useRecentLifecycleIncidents({ limit: 20 });
  const { data, isError, status, refetch } = incidentsQuery;

  const isInitialLoading = devicesQuery.status === 'pending' || status === 'pending';

  if (isInitialLoading) {
    return (
      <Card padding="lg" className="h-full">
        <SectionSkeleton shape="list" rows={4} label="Device recovery loading" />
      </Card>
    );
  }

  const fleet = deriveDashboardFleetSummary(devicesQuery.data ?? []);
  const lifecycleDevices = fleet.lifecycleDevices;
  const incidents = groupLifecycleIncidents(data ?? []).slice(0, MAX_INCIDENTS);
  const hasRecoveryWork = lifecycleDevices.length > 0 || incidents.length > 0 || isError || devicesQuery.isError;

  return (
    <Card padding="lg" className="h-full">
      <div className="flex items-baseline justify-between">
        <div>
          <h2 className="heading-section">Device recovery</h2>
          {lifecycleDevices.length > 0 ? (
            <p className="mt-0.5 text-xs text-text-2">
              <span className="font-mono tabular-nums">{lifecycleDevices.length}</span> affected
            </p>
          ) : null}
        </div>
        <Link
          to="/analytics?tab=reliability"
          className="text-xs font-medium text-accent hover:text-accent-hover"
        >
          View all
        </Link>
      </div>

      {devicesQuery.isError ? (
        <div className="mt-4">
          <FetchError message="Could not load device recovery data." onRetry={() => void devicesQuery.refetch()} />
        </div>
      ) : !hasRecoveryWork ? (
        <p className="mt-4 text-sm text-text-2">No recovery work right now.</p>
      ) : (
        <>
          {lifecycleDevices.length > 0 ? (
            <>
              <ul className="mt-4 flex flex-col gap-1">
                {lifecycleDevices.slice(0, MAX_AFFECTED).map((device) => (
                  <li
                    key={device.id}
                    className="flex items-start justify-between gap-3 rounded-md px-2 py-2 transition-colors hover:bg-surface-2"
                  >
                    <div className="min-w-0 flex-1">
                      <Link to={`/devices/${device.id}`} className="block truncate text-sm font-medium text-accent hover:text-accent-hover">
                        {device.name}
                      </Link>
                      {device.lifecycle_policy_summary.detail ? (
                        <p className="mt-0.5 truncate text-xs text-text-2">{device.lifecycle_policy_summary.detail}</p>
                      ) : null}
                    </div>
                    <div className="shrink-0">
                      <LifecyclePolicyBadge summary={device.lifecycle_policy_summary} />
                    </div>
                  </li>
                ))}
              </ul>
              {lifecycleDevices.length > MAX_AFFECTED ? (
                <Link
                  to="/devices?needs_attention=true"
                  className="flex items-center justify-between rounded-md px-2 py-1.5 text-xs font-medium text-text-2 transition-colors hover:bg-surface-2 hover:text-accent"
                >
                  <span>+ {lifecycleDevices.length - MAX_AFFECTED} more affected</span>
                  <span aria-hidden="true">→</span>
                </Link>
              ) : null}
            </>
          ) : null}

          <div className={`${lifecycleDevices.length > 0 ? 'mt-4 border-t border-border pt-4' : 'mt-4'}`}>
            <div className="mb-2 flex items-center justify-between">
              <span className="heading-label">Recent incidents</span>
              {incidents.length > 0 ? (
                <span className="text-xs text-text-3">{incidents.length}</span>
              ) : null}
            </div>
            {isError ? (
              <FetchError message="Could not load incidents." onRetry={() => void refetch()} />
            ) : incidents.length === 0 ? (
              <p className="text-sm text-text-2">No recent incidents.</p>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {incidents.map((incident) => (
                  <li key={incident.key} className="flex items-center justify-between gap-3 text-xs">
                    <Link
                      to={`/devices/${incident.deviceId}`}
                      className="min-w-0 flex-1 truncate font-medium text-text-2 hover:text-accent"
                    >
                      {incident.deviceName}
                    </Link>
                    <Badge tone={incidentToneFromEventType(incident.eventType)} size="sm">
                      {incident.label} · {compactRelativeTime(incident.latestCreatedAt)}
                    </Badge>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </Card>
  );
}
