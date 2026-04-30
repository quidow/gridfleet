import { useState } from 'react';
import { toast } from 'sonner';
import { invokeFeatureAction } from '../../api/hostFeatureActions';
import type { PackFeatureAction } from '../../types/driverPacks';

export interface HostFeatureActionButtonProps {
  hostId: string;
  packId: string;
  featureId: string;
  action: PackFeatureAction;
}

export default function HostFeatureActionButton({
  hostId,
  packId,
  featureId,
  action,
}: HostFeatureActionButtonProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleClick() {
    setLoading(true);
    setError(null);
    try {
      const result = await invokeFeatureAction(hostId, packId, featureId, action.id, {});
      if (result.ok) {
        toast.success(`Action ${action.label} succeeded`);
      } else {
        const msg = result.detail || 'Action failed';
        setError(msg);
        toast.error(msg);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Action failed';
      setError(msg);
      toast.error(msg);
    } finally {
      setLoading(false);
    }
  }

  return (
    <span className="inline-flex flex-col items-start gap-0.5">
      <button
        type="button"
        disabled={loading}
        onClick={() => void handleClick()}
        className="inline-flex items-center gap-1.5 rounded-md border border-accent/30 bg-accent-soft px-2.5 py-1 text-xs font-medium text-accent hover:bg-accent/10 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {loading ? (
          <>
            <span
              className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-accent border-t-transparent"
              aria-hidden="true"
            />
            {action.label}
          </>
        ) : (
          action.label
        )}
      </button>
      {error != null ? <span className="text-xs text-danger-foreground">{error}</span> : null}
    </span>
  );
}
