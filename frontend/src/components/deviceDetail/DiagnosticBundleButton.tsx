import { useState } from 'react';

import { useExportDeviceDiagnostics } from '../../hooks/useDeviceDiagnostics';
import { Button } from '../ui';
import DiagnosticBundleModal from './DiagnosticBundleModal';

type Props = { deviceId: string };

export default function DiagnosticBundleButton({ deviceId }: Props) {
  const exportMutation = useExportDeviceDiagnostics(deviceId);
  const [payload, setPayload] = useState<unknown>(null);
  const [redacted, setRedacted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trigger = async (nextRedacted: boolean) => {
    setError(null);
    try {
      const result = await exportMutation.mutateAsync({ redact: nextRedacted, persist: true });
      setPayload(result.payload);
      setRedacted(nextRedacted);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Capture failed');
    }
  };

  return (
    <>
      <Button onClick={() => void trigger(false)} loading={exportMutation.isPending}>
        Capture diagnostic bundle
      </Button>
      <DiagnosticBundleModal
        open={payload !== null || Boolean(error)}
        title={`Device ${deviceId} diagnostics`}
        payload={payload}
        redacted={redacted}
        error={error}
        onClose={() => {
          setPayload(null);
          setError(null);
        }}
        onToggleRedact={() => void trigger(!redacted)}
      />
    </>
  );
}
