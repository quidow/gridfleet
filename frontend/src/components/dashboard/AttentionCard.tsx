import { Link } from 'react-router-dom';
import { useDevices } from '../../hooks/useDevices';
import { useRecentLifecycleIncidents } from '../../hooks/useLifecycle';
import { LifecyclePolicyBadge } from '../LifecyclePolicyBadge';
import { Badge } from '../ui/Badge';
import { Card } from '../ui/Card';
import { SectionSkeleton } from '../ui/SectionSkeleton';
import { deriveAttentionRows } from './dashboardSummary';

const MAX_ROWS = 5;

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

export function AttentionCard() {
  const devicesQuery = useDevices();
  const incidentsQuery = useRecentLifecycleIncidents({ limit: 20 });

  if (devicesQuery.status === 'pending' || incidentsQuery.status === 'pending') {
    return (
      <Card padding="lg" className="h-full">
        <SectionSkeleton shape="list" rows={2} label="Attention loading" />
      </Card>
    );
  }

  const attention = deriveAttentionRows(devicesQuery.data ?? [], incidentsQuery.data ?? []);
  const visible = attention.rows.slice(0, MAX_ROWS);
  const overflow = attention.total - visible.length;
  const hasCritical = visible.some((row) => row.tone === 'critical');
  const accent =
    attention.total === 0 ? '' : hasCritical ? 'border-l-4 border-l-danger-strong' : 'border-l-4 border-l-warning-strong';

  return (
    <Card padding="lg" className={`flex h-full flex-col ${accent}`}>
      <div className="flex items-baseline justify-between">
        <h2 className="heading-section">
          Needs attention{' '}
          {attention.total > 0 ? (
            <span className="font-mono text-sm font-normal tabular-nums text-text-2">{attention.total}</span>
          ) : null}
        </h2>
        {attention.total > 0 ? (
          <Link to="/devices?needs_attention=true" className="text-xs font-medium text-accent hover:text-accent-hover">
            View all
          </Link>
        ) : null}
      </div>

      {attention.total === 0 ? (
        <p className="mt-3 flex items-center gap-2 text-sm text-text-2">
          <span className="inline-block h-2 w-2 rounded-full bg-success-strong" />
          Nothing needs attention.
        </p>
      ) : (
        <>
          <ul className="mt-3 flex flex-col divide-y divide-border">
            {visible.map((row) => {
              const meta = [row.reason, row.latestAt ? `${compactRelativeTime(row.latestAt)} ago` : null]
                .filter((part): part is string => part !== null)
                .join(' · ');
              return (
                <li key={row.deviceId} className="py-2.5 first:pt-0 last:pb-0">
                  <div className="flex items-center justify-between gap-3">
                    <Link
                      to={`/devices/${row.deviceId}`}
                      className="min-w-0 truncate text-sm font-medium text-accent hover:text-accent-hover"
                    >
                      {row.deviceName}
                    </Link>
                    <span className="shrink-0">
                      {row.lifecycleSummary ? (
                        <LifecyclePolicyBadge summary={row.lifecycleSummary} />
                      ) : (
                        <Badge tone={row.tone} size="sm">
                          {row.badgeLabel}
                        </Badge>
                      )}
                    </span>
                  </div>
                  {meta ? <p className="mt-0.5 truncate text-xs text-text-2">{meta}</p> : null}
                </li>
              );
            })}
          </ul>
          {overflow > 0 ? (
            <Link
              to="/devices?needs_attention=true"
              className="mt-2 block text-xs font-medium text-text-2 transition-colors hover:text-accent"
            >
              + {overflow} more
            </Link>
          ) : null}
        </>
      )}

    </Card>
  );
}
