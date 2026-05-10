import { Fragment, useState } from 'react';
import { Download, AlertTriangle, ChevronDown, ChevronRight } from 'lucide-react';
import { useDeviceReliability } from '../../hooks/useAnalytics';
import { downloadAnalyticsCsv } from '../../api/analytics';
import LoadingSpinner from '../LoadingSpinner';
import PlatformIcon from '../PlatformIcon';
import AnalyticsEmptyState from './AnalyticsEmptyState';
import Card from '../ui/Card';
import type { AnalyticsParams } from '../../api/analytics';

interface Props {
  params: AnalyticsParams;
}

export default function ReliabilityTab({ params }: Props) {
  const { data, isLoading } = useDeviceReliability(params);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  if (isLoading) return <LoadingSpinner />;

  const rows = data ?? [];

  return (
    <div className="space-y-8">
      <Card padding="none">
        <div className="px-5 py-4 border-b border-border flex items-center justify-between">
          <h3 className="text-sm font-medium text-text-2">Device Reliability</h3>
          <button
            onClick={() => downloadAnalyticsCsv('devices/reliability', params)}
            className="flex items-center gap-1 text-xs text-text-3 hover:text-text-2"
          >
            <Download size={14} /> CSV
          </button>
        </div>

        {rows.length === 0 ? (
          <div className="px-5 py-6">
            <AnalyticsEmptyState
              title="No incidents recorded in this period"
              description="This range has no reliability incidents. Try widening the window if you want to compare historical trouble spots."
            />
          </div>
        ) : (
          <table className="min-w-full divide-y divide-border">
            <thead className="bg-surface-2">
              <tr>
                <th className="px-5 py-3 text-left text-xs font-medium text-text-3 uppercase w-8"></th>
                <th className="px-5 py-3 text-left text-xs font-medium text-text-3 uppercase">Device</th>
                <th className="px-5 py-3 text-left text-xs font-medium text-text-3 uppercase">Platform</th>
                <th className="px-5 py-3 text-left text-xs font-medium text-text-3 uppercase">Health Failures</th>
                <th className="px-5 py-3 text-left text-xs font-medium text-text-3 uppercase">Connectivity Lost</th>
                <th className="px-5 py-3 text-left text-xs font-medium text-text-3 uppercase">Node Crashes</th>
                <th className="px-5 py-3 text-left text-xs font-medium text-text-3 uppercase">Total</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((row) => {
                const needsAttention = row.total_incidents > 5;
                const isExpanded = expandedId === row.device_id;
                return (
                  <Fragment key={row.device_id}>
                    <tr
                      className={`cursor-pointer hover:bg-surface-2 ${needsAttention ? 'bg-danger-soft' : ''}`}
                      onClick={() => setExpandedId(isExpanded ? null : row.device_id)}
                    >
                      <td className="px-5 py-3 w-8">
                        {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      </td>
                      <td className="px-5 py-3 text-sm text-text-1">
                        <div className="flex items-center gap-2">
                          {needsAttention && <AlertTriangle size={14} className="text-danger-strong" />}
                          {row.device_name}
                        </div>
                      </td>
                      <td className="px-5 py-3 text-sm text-text-2">
                        <PlatformIcon platformId={row.platform_id} platformLabel={null} />
                      </td>
                      <td className="px-5 py-3 text-sm text-text-2">{row.health_check_failures}</td>
                      <td className="px-5 py-3 text-sm text-text-2">{row.connectivity_losses}</td>
                      <td className="px-5 py-3 text-sm text-text-2">{row.node_crashes}</td>
                      <td className="px-5 py-3 text-sm font-medium text-text-1">{row.total_incidents}</td>
                    </tr>
                    {isExpanded && (
                      <tr>
                        <td colSpan={7} className="px-10 py-4 bg-surface-2">
                          <div className="text-sm text-text-2 space-y-2">
                            <p className="font-medium text-text-2">Incident Breakdown</p>
                            <div className="grid grid-cols-3 gap-4">
                              <div>
                                <p className="text-xs text-text-3 uppercase">Health Check Failures</p>
                                <p className="text-lg font-semibold text-text-1">{row.health_check_failures}</p>
                              </div>
                              <div>
                                <p className="text-xs text-text-3 uppercase">Connectivity Losses</p>
                                <p className="text-lg font-semibold text-text-1">{row.connectivity_losses}</p>
                              </div>
                              <div>
                                <p className="text-xs text-text-3 uppercase">Node Crashes</p>
                                <p className="text-lg font-semibold text-text-1">{row.node_crashes}</p>
                              </div>
                            </div>
                            {needsAttention && (
                              <p className="text-danger-foreground text-xs mt-2">
                                This device has more than 5 incidents — investigate recurring issues.
                              </p>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
