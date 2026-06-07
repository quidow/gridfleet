import { useEffect, useState } from 'react';
import { Activity } from 'lucide-react';
import { toast } from 'sonner';
import { DataTable } from '../ui/DataTable';
import { EmptyState } from '../ui/EmptyState';
import { Button } from '../ui/Button';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { ListPageSubheader } from '../ui/ListPageSubheader';
import { QueuedRequestsCard } from './QueuedRequestsCard';
import { SessionCapabilities } from './SessionCapabilities';
import { buildExpanderColumn, buildSessionColumns } from './sessionColumns';
import { useKillSession, useSessions } from '../../hooks/useSessions';
import { useGridQueue } from '../../hooks/useGridQueue';
import type { DataTableColumn } from '../ui/DataTable';
import type { SessionDetail, SessionSortKey } from '../../types';

const ACTIVE_POLL_MS = 5_000;

export function ActiveSessionsSection({ onUpdatedAt }: { onUpdatedAt: (t: number) => void }) {
  const { data: sessions, isLoading, dataUpdatedAt } = useSessions({ active: true, limit: 200 }, ACTIVE_POLL_MS);
  const { data: queue } = useGridQueue();
  const killMutation = useKillSession();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [killTarget, setKillTarget] = useState<SessionDetail | null>(null);

  useEffect(() => {
    onUpdatedAt(dataUpdatedAt);
  }, [dataUpdatedAt, onUpdatedAt]);

  const rows = sessions?.items ?? [];

  function toggleExpanded(s: SessionDetail) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(s.id)) next.delete(s.id);
      else next.add(s.id);
      return next;
    });
  }

  function confirmKill() {
    if (!killTarget) return;
    killMutation.mutate(killTarget.session_id, {
      onSuccess: (result) => {
        if (result.terminated) toast.success('Session killed');
        else toast.success('Session ended in GridFleet; Appium unreachable — orphan sweep will retire it');
      },
      onError: () => toast.error('Could not kill session'),
    });
  }

  const columns: DataTableColumn<SessionDetail, SessionSortKey>[] = [
    buildExpanderColumn((s) => expanded.has(s.id), toggleExpanded),
    ...buildSessionColumns(),
    {
      key: 'kill',
      header: '',
      align: 'right',
      render: (s) =>
        s.status === 'running' ? (
          <Button
            variant="danger"
            size="sm"
            onClick={() => setKillTarget(s)}
            aria-label={`Kill session ${s.session_id}`}
          >
            Kill
          </Button>
        ) : null,
    },
  ];

  return (
    <div className="fade-in-stagger flex flex-col gap-4">
      <QueuedRequestsCard requests={queue?.requests ?? []} />
      <ListPageSubheader title={`Showing ${rows.length} active session${rows.length === 1 ? '' : 's'}`} />
      <DataTable<SessionDetail, SessionSortKey>
        columns={columns}
        rows={rows}
        rowKey={(s) => s.id}
        loading={isLoading}
        renderExpandedRow={(s) => (expanded.has(s.id) ? <SessionCapabilities session={s} /> : null)}
        emptyState={
          <EmptyState
            icon={Activity}
            title="No active sessions"
            description="Running and pending sessions will appear here."
          />
        }
      />
      <ConfirmDialog
        isOpen={killTarget !== null}
        onClose={() => setKillTarget(null)}
        onConfirm={confirmKill}
        title="Kill session"
        message={`Kill session ${killTarget?.session_id ?? ''} on ${killTarget?.device_name ?? 'unknown device'}? The Appium session will be terminated.`}
        confirmLabel="Kill session"
        variant="danger"
      />
    </div>
  );
}
