import { Card } from '../ui/Card';
import type { HostCircuitBreaker } from '../../types';

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
  breaker: HostCircuitBreaker;
};

export function HostCircuitBreakerCard({ breaker }: Props) {
  return (
    <Card padding="none">
      <div className="flex items-center justify-between border-b border-border px-5 py-4">
        <h2 className="text-sm font-medium text-text-2">Circuit Breaker</h2>
        <span
          className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ${breakerStatusClasses(breaker.status)}`}
        >
          {formatBreakerStatus(breaker.status)}
        </span>
      </div>
      <dl className="space-y-2 p-5 text-sm">
        <div className="flex justify-between gap-3">
          <dt className="text-text-3">Consecutive Failures</dt>
          <dd className="font-medium text-text-1">{breaker.consecutive_failures}</dd>
        </div>
        <div className="flex justify-between gap-3">
          <dt className="text-text-3">Cooldown Window</dt>
          <dd className="font-medium text-text-1">{formatRetryAfter(breaker.cooldown_seconds)}</dd>
        </div>
        <div className="flex justify-between gap-3">
          <dt className="text-text-3">Retry After</dt>
          <dd className="font-medium text-text-1">{formatRetryAfter(breaker.retry_after_seconds)}</dd>
        </div>
        <div className="flex justify-between gap-3">
          <dt className="text-text-3">Probe In Flight</dt>
          <dd className="font-medium text-text-1">{breaker.probe_in_flight ? 'Yes' : 'No'}</dd>
        </div>
      </dl>
      {breaker.last_error ? (
        <div className="mx-5 mb-5 rounded-md border border-danger-strong/30 bg-danger-soft px-3 py-2 text-sm text-danger-foreground">
          {breaker.last_error}
        </div>
      ) : (
        <p className="px-5 pb-5 text-sm text-text-3">No recent breaker error is recorded.</p>
      )}
    </Card>
  );
}
