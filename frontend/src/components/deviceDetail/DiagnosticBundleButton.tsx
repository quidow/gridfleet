import { useState } from 'react';

import { fetchDeviceDiagnosticSnapshot } from '../../api/deviceDiagnostics';
import { useExportDeviceDiagnostics } from '../../hooks/useDeviceDiagnostics';
import { Button } from '../ui';
import { DiagnosticBundleModal } from './DiagnosticBundleModal';

type Props = { deviceId: string };

export function DiagnosticBundleButton({ deviceId }: Props) {
  // Capture is a single POST that persists a snapshot row. The redact toggle
  // re-renders the same snapshot via GET /snapshots/{id}?redact=true rather
  // than re-POSTing. This avoids the per-device 5-second rate limit on the
  // toggle path and prevents the snapshot history from filling with duplicate
  // rows every time the operator flips between raw and redacted views.
  const exportMutation = useExportDeviceDiagnostics(deviceId);
  const [payload, setPayload] = useState<unknown>(null);
  const [redacted, setRedacted] = useState(false);
  const [snapshotId, setSnapshotId] = useState<string | null>(null);
  const [toggling, setToggling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const capture = async () => {
    setError(null);
    try {
      const result = await exportMutation.mutateAsync({ redact: false, persist: true });
      setPayload(result.payload);
      setRedacted(false);
      setSnapshotId(result.snapshot_id ?? null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Capture failed');
    }
  };

  const toggleRedact = async () => {
    const next = !redacted;
    if (snapshotId === null) {
      // No persisted snapshot to re-fetch — fall back to a fresh capture so
      // the user still sees the redacted view. Subject to the rate limit.
      setError(null);
      try {
        const result = await exportMutation.mutateAsync({ redact: next, persist: true });
        setPayload(result.payload);
        setRedacted(next);
        setSnapshotId(result.snapshot_id ?? null);
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : 'Capture failed');
      }
      return;
    }
    setError(null);
    setToggling(true);
    try {
      const detail = await fetchDeviceDiagnosticSnapshot(deviceId, snapshotId, { redact: next });
      setPayload(detail.payload);
      setRedacted(next);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Toggle failed');
    } finally {
      setToggling(false);
    }
  };

  const close = () => {
    setPayload(null);
    setRedacted(false);
    setSnapshotId(null);
    setError(null);
  };

  return (
    <>
      <Button onClick={() => void capture()} loading={exportMutation.isPending}>
        Capture diagnostic bundle
      </Button>
      <DiagnosticBundleModal
        open={payload !== null || Boolean(error)}
        title={`Device ${deviceId} diagnostics`}
        payload={payload}
        redacted={redacted}
        error={error}
        onClose={close}
        onToggleRedact={() => void toggleRedact()}
        toggling={toggling}
      />
    </>
  );
}
