import { Link } from 'react-router-dom';
import { formatHostTimestamp } from '../hosts/hostFormatting';
import type { HostRead, HostDiagnostics, HostRecoveryEvent } from '../../types';

function formatBreakerStatus(status: string) {
  switch (status) {
    case 'open':
      return 'Open';
    case 'half_open':
      return 'Half Open';
    default:
      return 'Closed';
  }
}

function breakerStatusClasses(status: string) {
  switch (status) {
    case 'open':
      return 'bg-danger-soft text-danger-foreground';
    case 'half_open':
      return 'bg-warning-soft text-warning-foreground';
    default:
      return 'bg-success-soft text-success-foreground';
  }
}

function formatRecoveryKind(event: HostRecoveryEvent) {
  switch (event.kind) {
    case 'restart_exhausted':
      return 'Restart Exhausted';
    case 'restart_succeeded':
      return 'Restart Succeeded';
    default:
      return 'Crash Detected';
  }
}

function formatRecoveryProcess(process: string | null) {
  if (process === 'grid_relay') {
    return 'Grid Relay';
  }
  return 'Appium';
}

function recoveryKindClasses(kind: string) {
  switch (kind) {
    case 'restart_succeeded':
      return 'bg-success-soft text-success-foreground';
    case 'restart_exhausted':
      return 'bg-danger-soft text-danger-foreground';
    default:
      return 'bg-warning-soft text-warning-foreground';
  }
}

function diagnosticsNodeStateLabel(nodeState: string | null, managed: boolean) {
  if (!managed) {
    return 'Unmapped';
  }
  if (nodeState === 'running') {
    return 'Running';
  }
  if (nodeState === 'error') {
    return 'Error';
  }
  if (nodeState === 'stopped') {
    return 'Stopped';
  }
  return 'Managed';
}

function formatRetryAfter(seconds: number | null) {
  if (seconds === null) {
    return '-';
  }
  if (seconds < 1) {
    return '<1s';
  }
  return `${Math.ceil(seconds)}s`;
}

type Props = {
  host: HostRead;
  hostDiagnostics: HostDiagnostics | undefined;
  diagnosticsLoading: boolean;
  diagnosticsError: unknown;
};

export default function HostDiagnosticsPanel({ host, hostDiagnostics, diagnosticsLoading, diagnosticsError }: Props) {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface-1">
      <div className="border-b border-border px-5 py-4">
        <h2 className="text-sm font-medium text-text-2">Diagnostics</h2>
        <p className="mt-1 text-sm text-text-3">
          Backend-owned breaker state, current managed Appium processes, and recent local recovery activity.
        </p>
      </div>

      {diagnosticsLoading ? (
        <p className="px-5 py-8 text-center text-sm text-text-3">Loading diagnostics...</p>
      ) : diagnosticsError || !hostDiagnostics ? (
        <p className="px-5 py-8 text-center text-sm text-text-3">
          Diagnostics are currently unavailable for this host.
        </p>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-6 px-5 py-5 lg:grid-cols-2">
            <div className="rounded-lg border border-border bg-surface-2 p-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <h3 className="text-sm font-medium text-text-2">Circuit Breaker</h3>
                <span
                  className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ${breakerStatusClasses(hostDiagnostics.circuit_breaker.status)}`}
                >
                  {formatBreakerStatus(hostDiagnostics.circuit_breaker.status)}
                </span>
              </div>
              <dl className="space-y-2 text-sm">
                <div className="flex justify-between gap-3">
                  <dt className="text-text-3">Consecutive Failures</dt>
                  <dd className="font-medium text-text-1">{hostDiagnostics.circuit_breaker.consecutive_failures}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-text-3">Cooldown Window</dt>
                  <dd className="font-medium text-text-1">{formatRetryAfter(hostDiagnostics.circuit_breaker.cooldown_seconds)}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-text-3">Retry After</dt>
                  <dd className="font-medium text-text-1">{formatRetryAfter(hostDiagnostics.circuit_breaker.retry_after_seconds)}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-text-3">Probe In Flight</dt>
                  <dd className="font-medium text-text-1">{hostDiagnostics.circuit_breaker.probe_in_flight ? 'Yes' : 'No'}</dd>
                </div>
              </dl>
              {hostDiagnostics.circuit_breaker.last_error ? (
                <div className="mt-3 rounded-md border border-danger-strong/30 bg-danger-soft px-3 py-2 text-sm text-danger-foreground">
                  {hostDiagnostics.circuit_breaker.last_error}
                </div>
              ) : (
                <p className="mt-3 text-sm text-text-3">No recent breaker error is recorded.</p>
              )}
            </div>

            <div className="rounded-lg border border-border bg-surface-2 p-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <h3 className="text-sm font-medium text-text-2">Managed Appium Processes</h3>
                <span className="text-xs text-text-3">
                  {formatHostTimestamp(hostDiagnostics.appium_processes.reported_at)}
                </span>
              </div>
              <p className="mb-3 text-sm text-text-3">
                {host.status === 'offline' && hostDiagnostics.appium_processes.reported_at
                  ? 'Host is offline, so this is the last reported process snapshot.'
                  : 'Appium nodes are shown from the most recent successful heartbeat.'}
              </p>
              {!hostDiagnostics.appium_processes.running_nodes.length ? (
                <p className="text-sm text-text-3">No managed Appium nodes were reported in the latest snapshot.</p>
              ) : (
                <div className="space-y-2">
                  {hostDiagnostics.appium_processes.running_nodes.map((node) => (
                    <div
                      key={`${node.port}-${node.connection_target ?? 'process'}`}
                      className="rounded-md border border-border bg-surface-1 px-3 py-2"
                    >
                      <div className="flex items-center justify-between gap-3">
                        <div>
                          <div className="text-sm font-medium text-text-1">
                            {node.device_id && node.device_name ? (
                              <Link to={`/devices/${node.device_id}`} className="text-accent hover:text-accent-hover">
                                {node.device_name}
                              </Link>
                            ) : (
                              'Unmapped process'
                            )}
                          </div>
                          <div className="text-xs text-text-3">
                            {node.connection_target ?? 'No connection target'} • port {node.port}
                          </div>
                        </div>
                        <span
                          className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ${
                            node.managed ? 'bg-info-soft text-info-foreground' : 'bg-neutral-soft text-neutral-foreground'
                          }`}
                        >
                          {diagnosticsNodeStateLabel(node.node_state, node.managed)}
                        </span>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-3">
                        <span>Platform: {node.platform_id ?? '-'}</span>
                        <span>PID: {node.pid ?? '-'}</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="border-t border-border px-5 py-5">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="text-sm font-medium text-text-2">Recent Recovery Events</h3>
              <span className="text-xs text-text-3">{hostDiagnostics.recent_recovery_events.length} shown</span>
            </div>
            {!hostDiagnostics.recent_recovery_events.length ? (
              <p className="text-sm text-text-3">No recent agent-local recovery events were recorded for this host.</p>
            ) : (
              <div className="space-y-2">
                {hostDiagnostics.recent_recovery_events.map((event) => (
                  <div key={event.id} className="rounded-md border border-border bg-surface-2 px-4 py-3">
                    <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
                      <div className="flex items-center gap-2">
                        <span className="inline-flex rounded-full bg-neutral-soft px-2.5 py-1 text-xs font-medium text-neutral-foreground">
                          {formatRecoveryProcess(event.process)}
                        </span>
                        <span
                          className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ${recoveryKindClasses(event.kind)}`}
                        >
                          {formatRecoveryKind(event)}
                        </span>
                        <Link to={`/devices/${event.device_id}`} className="text-sm font-medium text-accent hover:text-accent-hover">
                          {event.device_name}
                        </Link>
                      </div>
                      <span className="text-xs text-text-3">{formatHostTimestamp(event.occurred_at)}</span>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-3">
                      <span>Port: {event.port ?? '-'}</span>
                      <span>Attempt: {event.attempt ?? '-'}</span>
                      <span>PID: {event.pid ?? '-'}</span>
                      <span>Exit Code: {event.exit_code ?? '-'}</span>
                      <span>Will Restart: {event.will_restart === null ? '-' : event.will_restart ? 'Yes' : 'No'}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
