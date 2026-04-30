import { Link } from 'react-router-dom';

interface ProportionalBarSegment {
  key: string;
  label: string;
  count: number;
  barClassName: string;
  dotClassName?: string;
  to?: string;
}

interface ProportionalBarProps {
  segments: ProportionalBarSegment[];
  showLegend?: boolean;
}

export default function ProportionalBar({ segments, showLegend = true }: ProportionalBarProps) {
  const total = segments.reduce((sum, segment) => sum + Math.max(0, segment.count), 0);
  const safeTotal = total > 0 ? total : 1;

  return (
    <>
      <div className="mt-4 flex h-2 w-full overflow-hidden rounded-full bg-surface-2">
        {total === 0
          ? null
          : segments.map((segment) =>
              segment.count > 0 ? (
                <div
                  key={segment.key}
                  className={segment.barClassName}
                  style={{ width: `${(segment.count / safeTotal) * 100}%` }}
                  aria-label={`${segment.label}: ${segment.count}`}
                />
              ) : null,
            )}
      </div>

      {showLegend ? (
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        {segments.map((segment) => {
          const content = (
            <>
              <span className={`inline-block h-2 w-2 rounded-full ${segment.dotClassName ?? segment.barClassName}`} />
              <span className="font-medium">{segment.label}</span>
              <span className="tabular-nums text-text-1">{segment.count}</span>
            </>
          );

          if (segment.to) {
            return (
              <Link
                key={segment.key}
                to={segment.to}
                className="inline-flex items-center gap-1.5 text-text-2 hover:text-text-1"
              >
                {content}
              </Link>
            );
          }

          return (
            <span key={segment.key} className="inline-flex items-center gap-1.5 text-text-2">
              {content}
            </span>
          );
        })}
      </div>
      ) : null}
    </>
  );
}
