import type { DeviceVerificationJob } from '../../types';
import {
  VERIFICATION_STAGE_LABELS,
  verificationStatusClasses,
  verificationStatusLabel,
} from './deviceVerificationWorkflow';

type Props = {
  activeJob?: DeviceVerificationJob | null;
  showStartError?: boolean;
};

export default function DeviceVerificationProgress({ activeJob, showStartError = false }: Props) {
  if (!activeJob && !showStartError) return null;

  const overallStatus =
    activeJob?.status === 'completed'
      ? 'passed'
      : activeJob?.status === 'failed'
        ? 'failed'
        : activeJob?.current_stage_status ?? 'running';
  const currentStageLabel = activeJob?.current_stage
    ? (VERIFICATION_STAGE_LABELS[activeJob.current_stage] ?? activeJob.current_stage)
    : null;

  return (
    <div
      className="space-y-3 rounded-lg border border-border bg-surface-2 p-4"
      data-testid="device-verification-progress"
    >
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-text-1">Verification Progress</p>
        {activeJob && (
          <span
            className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${verificationStatusClasses(overallStatus)}`}
          >
            {activeJob.status === 'completed'
              ? 'Completed'
              : activeJob.status === 'failed'
                ? 'Failed'
                : 'Running'}
          </span>
        )}
      </div>
      {activeJob && (
        <div className={`rounded-md border px-3 py-2 ${verificationStatusClasses(activeJob.current_stage_status ?? 'pending')}`}>
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm font-medium">
              {currentStageLabel ?? 'Waiting to start'}
            </span>
            <span className="text-xs font-semibold uppercase tracking-wide">
              {verificationStatusLabel(activeJob.current_stage_status ?? 'pending')}
            </span>
          </div>
          {activeJob.detail && <p className="mt-1 text-xs">{activeJob.detail}</p>}
          {!activeJob.detail && !currentStageLabel && (
            <p className="mt-1 text-xs">Waiting for verification to begin.</p>
          )}
        </div>
      )}
      {showStartError && <p className="text-sm text-danger-foreground">Unable to start verification. Please try again.</p>}
      {activeJob?.error && <p className="text-sm text-danger-foreground">{activeJob.error}</p>}
    </div>
  );
}
