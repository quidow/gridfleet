import Card from '../../components/ui/Card';
import { Badge, DefinitionList } from '../../components/ui';
import type { DriverPack } from '../../types/driverPacks';

function CountBadge({ label, value, tone = 'neutral' }: { label: string; value: number; tone?: 'neutral' | 'warning' }) {
  return (
    <Badge tone={tone}>
      {value} {label}
    </Badge>
  );
}

function versionList(versions: string[] | undefined): string {
  if (!versions || versions.length === 0) return 'none';
  if (versions.length <= 2) return versions.join(', ');
  return `${versions.slice(0, 2).join(', ')} +${versions.length - 2}`;
}

export default function DriverOverviewPanel({ pack }: { pack: DriverPack }) {
  const summary = pack.runtime_summary;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card padding="md">
        <h2 className="mb-3 text-sm font-semibold text-text-1">Live Rollout</h2>
        <div className="mb-4 flex flex-wrap gap-2">
          <CountBadge label="installed hosts" value={summary?.installed_hosts ?? 0} />
          <CountBadge label="blocked hosts" value={summary?.blocked_hosts ?? 0} tone={(summary?.blocked_hosts ?? 0) > 0 ? 'warning' : 'neutral'} />
          <CountBadge label="active runs" value={pack.active_runs} />
          <CountBadge label="live sessions" value={pack.live_sessions} />
        </div>
        <DefinitionList
          layout="stacked"
          items={[
            { term: 'Appium Server', definition: `server actual ${versionList(summary?.actual_appium_server_versions)}` },
            { term: 'Appium Driver', definition: `driver actual ${versionList(summary?.actual_appium_driver_versions)}` },
          ]}
        />
      </Card>

      <Card padding="md">
        <h2 className="mb-3 text-sm font-semibold text-text-1">Runtime Drift</h2>
        {(summary?.driver_drift_hosts ?? 0) === 0 ? (
          <p className="text-sm text-text-3">No driver version drift reported.</p>
        ) : (
          <Badge tone="warning">
            {summary?.driver_drift_hosts} host{summary?.driver_drift_hosts === 1 ? '' : 's'} with driver drift
          </Badge>
        )}
      </Card>
    </div>
  );
}
