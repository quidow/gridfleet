import { Play, Wifi } from 'lucide-react';
import { useReconnectDevice, useRunDeviceSessionTest } from '../../hooks/useDevices';
import type { ConnectionType, DeviceHealth, DeviceType } from '../../types';
import Button from '../ui/Button';
import { formatDate, formatViabilityStatus, getCheckLabels } from './utils';
import { usePlatformDescriptor } from '../../hooks/usePlatformDescriptor';

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span className={`inline-block h-2.5 w-2.5 rounded-full ${ok ? 'bg-success-strong' : 'bg-danger-strong'}`} />
  );
}

function HealthSummaryPill({ healthy }: { healthy: boolean }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full bg-surface-1 px-2.5 py-1 text-xs font-medium ${healthy ? 'text-success-foreground' : 'text-danger-foreground'}`}>
      <StatusDot ok={healthy} />
      {healthy ? 'All checks passing' : 'Checks failing'}
    </span>
  );
}

function HealthCheckRow({ label, check }: { label: string; check: Record<string, unknown> }) {
  const ok =
    check.ok === true ||
    check.connected === true ||
    check.responsive === true ||
    check.visible === true ||
    check.reachable === true ||
    check.booted === true ||
    check.healthy === true ||
    check.status === 'ok';
  const detail = Object.entries(check)
    .filter(([key]) => !['check_id', 'connected', 'ok', 'responsive', 'visible', 'reachable', 'booted', 'healthy', 'status'].includes(key))
    .filter(([, value]) => value !== '' && value !== null && value !== undefined)
    .map(([key, value]) => `${key}: ${value}`)
    .join(', ');

  return (
    <div className="py-2 text-sm">
      <div className="flex items-center justify-between gap-3">
        <span className="text-text-2">{label}</span>
        <span className="flex items-center gap-2">
          <StatusDot ok={ok} />
          <span className={ok ? 'font-medium text-success-foreground' : 'font-medium text-danger-foreground'}>{ok ? 'OK' : 'Fail'}</span>
        </span>
      </div>
      {detail ? <p className="mt-1 break-words font-mono text-xs text-text-3">{detail}</p> : null}
    </div>
  );
}

function DetailRow({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: string;
  tone?: 'neutral' | 'success' | 'danger';
}) {
  const valueClass =
    tone === 'success'
      ? 'text-success-foreground'
      : tone === 'danger'
        ? 'text-danger-foreground'
        : 'text-text-1';

  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <span className="text-text-2">{label}</span>
      <span className={`min-w-0 truncate text-right font-medium ${valueClass}`}>{value}</span>
    </div>
  );
}

type Props = {
  health?: DeviceHealth;
  packId?: string | null;
  platformId?: string | null;
  deviceType?: DeviceType;
  connectionType?: ConnectionType;
  deviceId: string;
  canTestSession: boolean;
  isLoading: boolean;
};

export default function DeviceHealthPanel({
  health,
  packId,
  platformId,
  deviceId,
  canTestSession,
  isLoading,
}: Props) {
  const reconnect = useReconnectDevice();
  const runSessionTest = useRunDeviceSessionTest();
  const resolvedPackId = packId ?? '';
  const resolvedPlatformId = platformId ?? '';
  const descriptor = usePlatformDescriptor(resolvedPackId, resolvedPlatformId);
  const canReconnect = descriptor?.lifecycleActions.includes('reconnect') === true;

  if (isLoading && !health) {
    return (
      <div>
        <h2 className="text-sm font-semibold text-text-1">Device Health</h2>
        <p className="mt-1 text-xs text-text-2">Connectivity checks, probe result, and recovery state.</p>
        <p className="mt-4 text-sm text-text-2">Checking…</p>
      </div>
    );
  }

  if (!health) {
    return null;
  }

  const labels = getCheckLabels(descriptor);
  const node = health.node ?? { running: false, port: null, state: null };
  const deviceChecks =
    health.device_checks && typeof health.device_checks === 'object'
      ? health.device_checks
      : {};
  const checksById = { ...deviceChecks } as Record<string, unknown>;
  if (Array.isArray(deviceChecks.checks)) {
    for (const check of deviceChecks.checks) {
      if (!check || typeof check !== 'object') {
        continue;
      }
      const checkId = (check as Record<string, unknown>).check_id;
      if (typeof checkId === 'string' && checkId) {
        checksById[checkId] = check;
      }
    }
  }

  return (
    <div>
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-text-1">Device Health</h2>
            <HealthSummaryPill healthy={health.healthy} />
          </div>
          <p className="mt-1 text-xs text-text-2">Connectivity checks, probe result, and recovery state.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="secondary"
            onClick={() => runSessionTest.mutate(deviceId)}
            disabled={runSessionTest.isPending || !canTestSession}
            leadingIcon={<Play size={12} />}
            title={canTestSession ? 'Create and tear down a probe Appium session' : 'Only available, unreserved devices can be probed'}
          >
            {runSessionTest.isPending ? 'Testing...' : 'Test Session'}
          </Button>
          {canReconnect ? (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => reconnect.mutate(deviceId)}
              disabled={reconnect.isPending}
              leadingIcon={<Wifi size={12} />}
            >
              {reconnect.isPending ? 'Reconnecting...' : 'Reconnect Device'}
            </Button>
          ) : null}
        </div>
      </div>

      <div className="mb-3 rounded-md border border-border bg-surface-1 px-3 py-2 text-sm">
        <div className="flex items-center justify-between gap-3">
          <span className="text-text-2">Appium Node</span>
          <span className="flex items-center gap-2">
            {node.port ? <span className="font-mono text-xs text-text-3">port: {node.port}</span> : null}
            <span className={`inline-block h-2.5 w-2.5 rounded-full ${node.running ? 'bg-success-strong' : 'bg-neutral-strong'}`} />
            <span className={node.running ? 'font-medium text-success-foreground' : 'font-medium text-text-2'}>
              {node.state ?? 'none'}
            </span>
          </span>
        </div>
      </div>

      <div className="divide-y divide-border rounded-md border border-border bg-surface-1 px-3">
        {Object.entries(labels).map(([key, label]) => {
          const check = checksById[key];
          if (!check || typeof check !== 'object') {
            return null;
          }
          return <HealthCheckRow key={key} label={label} check={check as Record<string, unknown>} />;
        })}
      </div>

      {typeof deviceChecks.detail === 'string' && deviceChecks.detail ? (
        <p className="mt-2 text-xs text-danger-foreground">{deviceChecks.detail}</p>
      ) : null}

      <div className="mt-4 space-y-2 border-t border-border pt-3">
        <h3 className="text-xs font-medium uppercase tracking-wide text-text-3">Session Viability</h3>
        <DetailRow
          label="Status"
          value={formatViabilityStatus(health.session_viability?.status)}
          tone={health.session_viability?.status === 'passed' ? 'success' : health.session_viability?.status === 'failed' ? 'danger' : 'neutral'}
        />
        <DetailRow label="Last Attempted" value={formatDate(health.session_viability?.last_attempted_at ?? null)} />
        <DetailRow label="Last Succeeded" value={formatDate(health.session_viability?.last_succeeded_at ?? null)} />
        <DetailRow label="Last Trigger" value={health.session_viability?.checked_by ?? '-'} />
        {health.session_viability?.error ? <p className="text-xs text-danger-foreground">{health.session_viability.error}</p> : null}
      </div>
    </div>
  );
}
