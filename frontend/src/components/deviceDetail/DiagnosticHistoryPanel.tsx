import { useState } from 'react';

import {
  useDeviceDiagnosticSnapshot,
  useDeviceDiagnosticSnapshots,
} from '../../hooks/useDeviceDiagnostics';
import Badge, { type BadgeTone } from '../ui/Badge';
import DiagnosticBundleModal from './DiagnosticBundleModal';
import { formatDate } from './utils';

type Props = { deviceId: string };

const TRIGGER_LABELS: Record<string, { label: string; tone: BadgeTone }> = {
  operator: { label: 'Operator', tone: 'neutral' },
  review_required: { label: 'Auto: review', tone: 'warning' },
};

export default function DiagnosticHistoryPanel({ deviceId }: Props) {
  const { data, isLoading } = useDeviceDiagnosticSnapshots(deviceId, 5);
  const [openSnapshotId, setOpenSnapshotId] = useState<string | null>(null);
  const [redacted, setRedacted] = useState(false);
  const detail = useDeviceDiagnosticSnapshot(deviceId, openSnapshotId, redacted);
  const items = data?.items ?? [];

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface-1 shadow-sm">
      <div className="border-b border-border px-5 py-4">
        <h2 className="text-sm font-semibold text-text-1">Diagnostic history</h2>
      </div>
      {isLoading ? (
        <div className="px-5 py-8 text-center text-sm text-text-2">Loading...</div>
      ) : items.length === 0 ? (
        <div className="px-5 py-4">
          <p className="rounded-lg border border-dashed border-border-strong bg-surface-2 px-4 py-4 text-center text-sm text-text-2">
            No diagnostic snapshots captured yet.
          </p>
        </div>
      ) : (
        <table className="min-w-full divide-y divide-border">
          <thead className="bg-surface-2">
            <tr>
              <th className="px-5 py-3 text-left text-xs font-medium uppercase text-text-2">Trigger</th>
              <th className="px-5 py-3 text-left text-xs font-medium uppercase text-text-2">Reason</th>
              <th className="px-5 py-3 text-left text-xs font-medium uppercase text-text-2">Captured</th>
              <th className="px-5 py-3"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {items.map((item) => {
              const meta = TRIGGER_LABELS[item.trigger] ?? {
                label: item.trigger,
                tone: 'neutral' as BadgeTone,
              };
              return (
                <tr key={item.id} className="hover:bg-surface-2">
                  <td className="px-5 py-3 text-sm">
                    <Badge tone={meta.tone}>{meta.label}</Badge>
                  </td>
                  <td className="max-w-xs truncate px-5 py-3 text-sm text-text-1" title={item.reason ?? ''}>
                    {item.reason ?? '-'}
                  </td>
                  <td className="whitespace-nowrap px-5 py-3 text-sm text-text-2">
                    {formatDate(item.captured_at)}
                  </td>
                  <td className="px-5 py-3 text-right">
                    <button
                      type="button"
                      className="text-sm text-accent hover:underline"
                      onClick={() => {
                        setRedacted(false);
                        setOpenSnapshotId(item.id);
                      }}
                      aria-label={`Open snapshot ${item.id}`}
                    >
                      Open
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <DiagnosticBundleModal
        open={openSnapshotId !== null}
        title={`Snapshot ${openSnapshotId ?? ''}`}
        payload={detail.data?.payload}
        redacted={redacted}
        loading={detail.isLoading}
        onClose={() => setOpenSnapshotId(null)}
        onToggleRedact={() => setRedacted((prev) => !prev)}
      />
    </div>
  );
}
