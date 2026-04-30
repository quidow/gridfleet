import type { DeviceHealth } from '../../types';
import { formatDate, formatRecoveryState } from './utils';

type LifecyclePolicy = NonNullable<DeviceHealth['lifecycle_policy']>;

const DEFAULT_POLICY: LifecyclePolicy = {
  last_failure_source: null,
  last_failure_reason: null,
  last_action: null,
  last_action_at: null,
  stop_pending: false,
  stop_pending_reason: null,
  stop_pending_since: null,
  excluded_from_run: false,
  excluded_run_id: null,
  excluded_run_name: null,
  excluded_at: null,
  will_auto_rejoin_run: false,
  recovery_suppressed_reason: null,
  backoff_until: null,
  recovery_state: 'idle',
};

type Props = {
  policy?: LifecyclePolicy | null;
};

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <span className="text-text-2">{label}</span>
      <span className="min-w-0 truncate text-right font-medium text-text-1">{value}</span>
    </div>
  );
}

export default function DeviceLifecyclePolicyPanel({ policy }: Props) {
  const effective = policy ?? DEFAULT_POLICY;

  return (
    <div className="p-5">
      <div className="mb-4">
        <h2 className="text-sm font-semibold text-text-1">Lifecycle Policy</h2>
        <p className="mt-1 text-xs text-text-2">Recovery state and run-exclusion metadata.</p>
      </div>
      <div className="space-y-2">
        <Row label="Recovery State" value={formatRecoveryState(effective.recovery_state)} />
        <Row label="Last Auto Action" value={effective.last_action ?? '-'} />
        <Row label="Failure Source" value={effective.last_failure_source ?? '-'} />
        <Row label="Deferred Stop" value={effective.stop_pending ? 'Waiting for session end' : 'No'} />
        <Row
          label="Run Exclusion"
          value={effective.excluded_from_run ? `Excluded from ${effective.excluded_run_name ?? 'active run'}` : 'No'}
        />
        <Row label="Auto Rejoin" value={effective.will_auto_rejoin_run ? 'Will rejoin on recovery' : 'No'} />
        {effective.last_failure_reason ? (
          <p className="text-xs text-danger-foreground">{effective.last_failure_reason}</p>
        ) : null}
        {effective.recovery_suppressed_reason ? (
          <p className="text-xs text-warning-foreground">{effective.recovery_suppressed_reason}</p>
        ) : null}
        {effective.backoff_until ? (
          <p className="text-xs text-text-2">Next recovery attempt after {formatDate(effective.backoff_until)}</p>
        ) : null}
      </div>
    </div>
  );
}
