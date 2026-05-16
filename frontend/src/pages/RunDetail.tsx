import { useParams, Link, useNavigate } from 'react-router-dom';
import { useState } from 'react';
import { useRun, useCancelRun, useForceReleaseRun } from '../hooks/useRuns';
import { useSessions } from '../hooks/useSessions';
import { StatusBadge } from '../components/StatusBadge';
import { LoadingSpinner } from '../components/LoadingSpinner';
import ConfirmDialog from '../components/ui/ConfirmDialog';
import DataTable from '../components/ui/DataTable';
import FetchError from '../components/ui/FetchError';
import PageHeader from '../components/ui/PageHeader';
import Card from '../components/ui/Card';
import RunActionButtons from '../components/runs/RunActionButtons';
import type { DataTableColumn } from '../components/ui/DataTable';
import { buildSessionColumns } from '../components/sessions/sessionColumns';
import type { RunState, SessionDetail, SessionSortKey } from '../types';
import { usePageTitle } from '../hooks/usePageTitle';
import { formatDateTime, formatDuration } from '../utils/dateFormatting';
import DefinitionList from '../components/ui/DefinitionList';
import { resolvePlatformLabel } from '../lib/labels';

const ACTIVE_STATES: RunState[] = ['pending', 'preparing', 'active', 'completing'];
const STATE_ORDER: RunState[] = ['pending', 'preparing', 'active', 'completing', 'completed'];

function formatDate(dateStr: string | null): string {
  return formatDateTime(dateStr);
}

type ReservedDevice = {
  device_id: string;
  identity_value: string;
  connection_target: string | null;
  pack_id: string;
  platform_id: string;
  platform_label: string | null;
  os_version: string;
  host_ip: string | null;
  excluded: boolean;
  exclusion_reason: string | null;
  excluded_until: string | null;
  cooldown_count: number;
  cooldown_escalated: boolean;
};

const DEVICE_COLUMNS: DataTableColumn<ReservedDevice>[] = [
  {
    key: 'identity',
    header: 'Identity',
    render: (d) => (
      <Link to={`/devices/${d.device_id}`} className="text-accent hover:underline text-sm font-mono">
        {d.identity_value}
      </Link>
    ),
  },
  {
    key: 'target',
    header: 'Target',
    render: (d) => <span className="text-sm font-mono text-text-3">{d.connection_target ?? '-'}</span>,
  },
  {
    key: 'platform',
    header: 'Platform',
    render: (d) => <span className="text-sm text-text-2">{resolvePlatformLabel(d.platform_id, d.platform_label)}</span>,
  },
  {
    key: 'os_version',
    header: 'OS Version',
    render: (d) => <span className="text-sm text-text-2">{d.os_version}</span>,
  },
  {
    key: 'host_ip',
    header: 'Host IP',
    render: (d) => <span className="text-sm text-text-3">{d.host_ip ?? '-'}</span>,
  },
  {
    key: 'cooldowns',
    header: 'Cooldowns',
    render: (d) =>
      d.cooldown_count > 0 ? (
        <span className="text-sm text-text-2">{d.cooldown_count}</span>
      ) : (
        <span className="text-sm text-text-3">-</span>
      ),
  },
  {
    key: 'reservation',
    header: 'Reservation',
    render: (d) => {
      if (d.cooldown_escalated) {
        return (
          <span className="text-sm text-warning-foreground">
            Escalated to maintenance ({d.exclusion_reason ?? 'cooldown threshold'})
          </span>
        );
      }
      if (d.excluded) {
        return (
          <span className="text-sm text-warning-foreground">{d.exclusion_reason ?? 'Excluded'}</span>
        );
      }
      return <span className="text-sm text-text-3">Active</span>;
    },
  },
];

const SESSION_COLUMNS = buildSessionColumns({ hideDevice: false });

export default function RunDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: run, isLoading } = useRun(id!);
  const { data: sessionsData, isLoading: sessionsLoading, isError: sessionsError, refetch: refetchSessions } = useSessions(
    id ? { run_id: id, limit: 50, direction: 'older' } : undefined,
  );
  usePageTitle(run?.name ?? 'Run');
  const cancelMutation = useCancelRun();
  const forceReleaseMutation = useForceReleaseRun();
  const [showCancelDialog, setShowCancelDialog] = useState(false);
  const [showForceReleaseDialog, setShowForceReleaseDialog] = useState(false);

  if (isLoading) return <LoadingSpinner />;
  if (!run) return <p className="text-text-3 text-center mt-12">Run not found</p>;

  const isActive = ACTIVE_STATES.includes(run.state);
  const stateIndex = STATE_ORDER.indexOf(run.state as RunState);
  const isTerminalNonHappy = ['failed', 'expired', 'cancelled'].includes(run.state);

  return (
    <div>
      <PageHeader
        title={run.name}
        subtitle={`Created by ${run.created_by ?? 'unknown'}`}
        summary={<StatusBadge status={run.state} />}
        actions={
          isActive ? (
            <RunActionButtons
              onCancel={() => setShowCancelDialog(true)}
              onForceRelease={() => setShowForceReleaseDialog(true)}
            />
          ) : undefined
        }
      />

      <div className="fade-in-stagger flex flex-col gap-6">
      {run.error && (
        <div className="bg-danger-soft border border-danger-strong/30 rounded-lg p-4">
          <p className="text-sm font-medium text-danger-foreground">Error: {run.error}</p>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card padding="md">
          <h2 className="text-sm font-medium text-text-3 mb-3">Run Info</h2>
          <DefinitionList
            items={[
              { term: 'TTL', definition: `${run.ttl_minutes} min` },
              { term: 'Heartbeat Timeout', definition: `${run.heartbeat_timeout_sec}s` },
              { term: 'Last Heartbeat', definition: formatDate(run.last_heartbeat) },
              { term: 'Duration', definition: formatDuration(run.created_at, run.completed_at) },
            ]}
          />
        </Card>
        <Card padding="md">
          <h2 className="text-sm font-medium text-text-3 mb-3">Timestamps</h2>
          <DefinitionList
            items={[
              { term: 'Created', definition: formatDate(run.created_at) },
              { term: 'Started', definition: formatDate(run.started_at) },
              { term: 'Completed', definition: formatDate(run.completed_at) },
            ]}
          />
        </Card>
      </div>

      <Card padding="md">
        <h2 className="text-sm font-medium text-text-3 mb-4">State Timeline</h2>
        <div className="flex items-center gap-1">
          {STATE_ORDER.map((s, i) => {
            const reached = !isTerminalNonHappy && i <= stateIndex;
            const isCurrent = s === run.state;
            return (
              <div key={s} className="flex items-center gap-1">
                {i > 0 && <div className={`w-8 h-0.5 ${reached ? 'bg-success-strong' : 'bg-border'}`} />}
                <div className={`px-2 py-1 rounded text-xs font-medium ${isCurrent ? 'bg-accent-soft text-accent ring-2 ring-accent' : reached ? 'bg-success-soft text-success-foreground' : 'bg-surface-2 text-text-3'}`}>
                  {s}
                </div>
              </div>
            );
          })}
          {isTerminalNonHappy && (
            <>
              <div className="w-8 h-0.5 bg-danger-strong" />
              <div className="px-2 py-1 rounded text-xs font-medium bg-danger-soft text-danger-foreground ring-2 ring-danger-strong">{run.state}</div>
            </>
          )}
        </div>
      </Card>

      <Card padding="none">
        <div className="px-5 py-4 border-b border-border">
          <h2 className="text-sm font-medium text-text-2">
            Reserved Devices ({run.devices?.length ?? 0})
          </h2>
        </div>
        <DataTable<ReservedDevice>
          columns={DEVICE_COLUMNS}
          rows={run.devices ?? []}
          rowKey={(d) => d.device_id}
          emptyState={<p className="px-5 py-8 text-sm text-text-3 text-center">No devices reserved</p>}
        />
      </Card>

      <Card padding="none">
        <div className="px-5 py-4 border-b border-border">
          <h2 className="text-sm font-medium text-text-2">
            Sessions ({sessionsData?.items?.length ?? 0}{sessionsData?.next_cursor ? '+' : ''})
          </h2>
        </div>
        {sessionsError ? (
          <FetchError
            message="Could not load sessions for this run."
            onRetry={() => void refetchSessions()}
            className="m-4"
          />
        ) : (
          <DataTable<SessionDetail, SessionSortKey>
            columns={SESSION_COLUMNS}
            rows={sessionsData?.items ?? []}
            rowKey={(s) => s.id}
            loading={sessionsLoading}
            emptyState={
              <p className="px-5 py-8 text-sm text-text-3 text-center">No sessions yet for this run.</p>
            }
          />
        )}
        {sessionsData?.next_cursor && (
          <div className="px-5 py-3 border-t border-border">
            <Link to={`/sessions?run_id=${id}`} className="text-sm text-accent hover:underline">
              View all sessions in the Sessions explorer →
            </Link>
          </div>
        )}
      </Card>
      </div>

      <ConfirmDialog
        isOpen={showCancelDialog}
        onClose={() => setShowCancelDialog(false)}
        onConfirm={() => cancelMutation.mutate(run.id, { onSuccess: () => navigate('/runs') })}
        title="Cancel Run?"
        message="This will cancel the run and release all reserved devices."
        confirmLabel="Cancel Run"
        variant="default"
      />

      <ConfirmDialog
        isOpen={showForceReleaseDialog}
        onClose={() => setShowForceReleaseDialog(false)}
        onConfirm={() => forceReleaseMutation.mutate(run.id, { onSuccess: () => navigate('/runs') })}
        title="Force Release?"
        message="This will force release all devices regardless of run state."
        confirmLabel="Force Release"
        variant="danger"
      />
    </div>
  );
}
