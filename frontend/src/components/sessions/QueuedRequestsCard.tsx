import { Link } from 'react-router-dom';
import { Card } from '../ui/Card';
import { Badge } from '../ui/Badge';
import { formatWaitTime } from '../../utils/dateFormatting';
import type { GridQueueRequest } from '../../types';

interface QueuedRequestsCardProps {
  requests: GridQueueRequest[];
}

function extractPlatformLabel(caps: Record<string, unknown> | undefined): string {
  if (!caps) return '—';
  const platform = caps.platformName as string | undefined;
  if (!platform) return '—';
  const version = caps['appium:platformVersion'] as string | undefined;
  return version ? `${platform} ${version}` : platform;
}

function extractDeviceId(caps: Record<string, unknown> | undefined): string | null {
  if (!caps) return null;
  const deviceId = caps['appium:gridfleet:deviceId'] as string | undefined;
  return deviceId || null;
}

export function QueuedRequestsCard({ requests }: QueuedRequestsCardProps) {
  if (requests.length === 0) return null;

  return (
    <Card padding="none">
      <div className="px-5 py-3 border-b border-border">
        <h2 className="text-sm font-medium text-text-1">
          Queued Requests ({requests.length})
        </h2>
        <p className="text-xs text-text-3 mt-0.5">Waiting for a matching Grid node</p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-text-3">
              <th className="px-5 py-2 font-medium">Platform</th>
              <th className="px-5 py-2 font-medium">Run</th>
              <th className="px-5 py-2 font-medium">Device</th>
              <th className="px-5 py-2 font-medium">Waiting</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {requests.map((req, i) => {
              const runId = req.runId ?? null;
              const deviceId = extractDeviceId(req.capabilities);
              return (
                <tr key={req.requestId ?? i}>
                  <td className="px-5 py-2">
                    <Badge tone="neutral">{extractPlatformLabel(req.capabilities)}</Badge>
                  </td>
                  <td className="px-5 py-2 font-mono text-xs">
                    {runId ? (
                      <Link to={`/runs/${runId}`} className="text-accent hover:text-accent-hover">
                        {runId.slice(0, 8)}
                      </Link>
                    ) : (
                      <span className="text-text-3">—</span>
                    )}
                  </td>
                  <td className="px-5 py-2 text-xs">
                    {deviceId ? (
                      <Link to={`/devices/${deviceId}`} className="text-accent hover:text-accent-hover">
                        {deviceId}
                      </Link>
                    ) : (
                      <span className="text-text-3">Any</span>
                    )}
                  </td>
                  <td className="px-5 py-2 font-mono text-xs tabular-nums text-text-2">
                    {formatWaitTime(req.requestTimestamp)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
