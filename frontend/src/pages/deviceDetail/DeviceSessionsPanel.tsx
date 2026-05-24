import { useSessions } from '../../hooks/useSessions';
import { buildSessionColumns } from '../../components/sessions/sessionColumns';
import { Card } from '../../components/ui/Card';
import { DataTable } from '../../components/ui/DataTable';
import type { SessionDetail } from '../../types';

const columns = buildSessionColumns({ hideDevice: true, hidePlatform: true });

type Props = {
  deviceId: string;
};

export function DeviceSessionsPanel({ deviceId }: Props) {
  const { data, isLoading } = useSessions({ device_id: deviceId, limit: 50 });
  const sessions = data?.items ?? [];

  return (
    <Card padding="none" as="section" className="overflow-hidden">
      <div className="px-5 py-4">
        <h2 className="text-sm font-semibold text-text-1">Sessions</h2>
        <p className="mt-1 text-xs text-text-2">Recent test sessions on this device.</p>
      </div>
      <div className="border-t border-border">
        <DataTable<SessionDetail>
          columns={columns}
          rows={sessions}
          rowKey={(s) => s.id}
          loading={isLoading}
          emptyState={<p className="py-8 text-center text-sm text-text-3">No sessions</p>}
        />
      </div>
    </Card>
  );
}
