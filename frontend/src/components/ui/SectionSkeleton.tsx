type SectionSkeletonShape = 'strip' | 'split' | 'list';

interface SectionSkeletonProps {
  shape: SectionSkeletonShape;
  rows?: number;
  label?: string;
}

function skeletonRows(rows: number) {
  return Array.from({ length: Math.max(1, rows) });
}

function StripSkeleton() {
  return (
    <div className="grid grid-cols-1 divide-y divide-border rounded-lg border border-border md:grid-cols-3 md:divide-x md:divide-y-0">
      {skeletonRows(3).map((_, index) => (
        <div key={index} data-testid="section-skeleton-cell" className="px-4 py-3">
          <div className="h-3 w-24 rounded bg-surface-2" />
          <div className="mt-3 h-4 w-32 rounded bg-surface-2" />
          <div className="mt-2 h-3 w-20 rounded bg-surface-2" />
        </div>
      ))}
    </div>
  );
}

function SplitSkeleton({ rows }: { rows: number }) {
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      {skeletonRows(2).map((_, panelIndex) => (
        <div key={panelIndex} data-testid="section-skeleton-panel" className="rounded-lg border border-border p-4">
          <div className="h-4 w-36 rounded bg-surface-2" />
          <div className="mt-4 space-y-3">
            {skeletonRows(rows).map((__, rowIndex) => (
              <div key={rowIndex} data-testid="section-skeleton-row" className="h-10 rounded-md bg-surface-2" />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function ListSkeleton({ rows }: { rows: number }) {
  return (
    <div className="space-y-3">
      <div className="h-4 w-40 rounded bg-surface-2" />
      <div className="divide-y divide-border rounded-lg border border-border">
        {skeletonRows(rows).map((_, index) => (
          <div key={index} data-testid="section-skeleton-row" className="px-3 py-3">
            <div className="h-3 w-3/5 rounded bg-surface-2" />
            <div className="mt-2 h-3 w-2/5 rounded bg-surface-2" />
          </div>
        ))}
      </div>
    </div>
  );
}

export default function SectionSkeleton({
  shape,
  rows = 3,
  label = 'Section loading',
}: SectionSkeletonProps) {
  return (
    <div
      role="status"
      aria-label={label}
      data-testid={`section-skeleton-${shape}`}
      className="animate-pulse p-5"
    >
      {shape === 'strip' ? <StripSkeleton /> : null}
      {shape === 'split' ? <SplitSkeleton rows={rows} /> : null}
      {shape === 'list' ? <ListSkeleton rows={rows} /> : null}
    </div>
  );
}
