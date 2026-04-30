import type { SessionCounts } from '../../types';

type Props = { counts: SessionCounts };

export default function RunProgressBar({ counts }: Props) {
  const failTotal = counts.failed + counts.error;

  if (counts.total === 0) {
    return (
      <div className="font-mono text-xs text-text-3" data-testid="run-progress-empty">
        no sessions yet
      </div>
    );
  }

  const segments = [
    { key: 'pass', n: counts.passed, className: 'bg-success-strong' },
    { key: 'fail', n: failTotal, className: 'bg-danger-strong' },
    { key: 'running', n: counts.running, className: 'bg-accent' },
  ].filter((s) => s.n > 0);

  const captionParts: string[] = [];
  if (counts.passed > 0) captionParts.push(`${counts.passed} pass`);
  if (failTotal > 0) captionParts.push(`${failTotal} fail`);
  if (counts.running > 0) captionParts.push(`${counts.running} running`);
  const ariaLabel = captionParts.join(', ');

  return (
    <div className="w-full max-w-[220px]">
      <div
        role="img"
        aria-label={ariaLabel}
        className="flex h-1.5 w-full overflow-hidden rounded-full bg-surface-2"
      >
        {segments.map((s) => (
          <span
            key={s.key}
            className={s.className}
            style={{ width: `${(s.n / counts.total) * 100}%` }}
          />
        ))}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-2 font-mono text-[11px] tabular-nums text-text-3">
        {counts.passed > 0 && <span className="text-success-fg">{counts.passed} pass</span>}
        {failTotal > 0 && <span className="text-danger-fg">{failTotal} fail</span>}
        {counts.running > 0 && <span>{counts.running} running</span>}
      </div>
    </div>
  );
}
