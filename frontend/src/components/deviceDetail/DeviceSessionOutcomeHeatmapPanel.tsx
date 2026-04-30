import { BarChart3 } from 'lucide-react';
import { useDeviceSessionOutcomeHeatmap } from '../../hooks/useDevices';
import {
  buildSessionOutcomeHeatmap,
  SESSION_OUTCOME_HEATMAP_WEEKDAY_LABELS,
  type SessionOutcomeHeatmapCell,
} from '../../lib/deviceSessionOutcomeHeatmap';
import FetchError from '../ui/FetchError';

type Props = {
  deviceId: string;
  days?: number;
};

function cellTone(cell: SessionOutcomeHeatmapCell): string {
  if (!cell.inRange) {
    return 'border-transparent bg-transparent';
  }
  if (cell.severity === 'error') {
    return 'border-danger-strong bg-danger-strong';
  }
  if (cell.severity === 'failed') {
    return 'border-warning-strong bg-warning-strong';
  }
  if (cell.severity === 'passed') {
    return 'border-success-strong bg-success-soft';
  }
  return 'border-border bg-surface-2';
}

function HeatmapSkeleton() {
  return (
    <div className="mt-5 flex gap-3 overflow-x-auto">
      <div className="grid grid-rows-7 gap-1 pt-6">
        {SESSION_OUTCOME_HEATMAP_WEEKDAY_LABELS.map((day) => (
          <div key={day} className="h-4 text-[11px] text-text-3">
            {day.slice(0, 1)}
          </div>
        ))}
      </div>
      <div className="grid grid-flow-col auto-cols-max gap-1">
        {Array.from({ length: 14 }).map((_, columnIndex) => (
            <div key={columnIndex} className="flex flex-col gap-1">
            <div className="mb-1 h-4 w-8 rounded bg-surface-2" />
            {Array.from({ length: 7 }).map((__, rowIndex) => (
              <div key={rowIndex} className="h-3.5 w-3.5 animate-pulse rounded-sm border border-border bg-surface-2" />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function CompactHeatmapEmptyState({ days }: { days: number }) {
  return (
    <div className="rounded-lg border border-dashed border-border-strong bg-surface-2 px-4 py-5">
      <div className="flex flex-col gap-3 text-center sm:flex-row sm:items-center sm:text-left">
        <div className="mx-auto flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-1 text-text-3 sm:mx-0">
          <BarChart3 size={18} />
        </div>
        <div>
          <h4 className="text-sm font-semibold text-text-1">No completed session outcomes in this window</h4>
          <p className="mt-1 text-sm text-text-2">
            No passed, failed, or errored sessions in the last {days} days.
          </p>
        </div>
      </div>
    </div>
  );
}

export default function DeviceSessionOutcomeHeatmapPanel({ deviceId, days = 90 }: Props) {
  const { data = [], isLoading, error, refetch } = useDeviceSessionOutcomeHeatmap(deviceId, days);
  const heatmap = buildSessionOutcomeHeatmap(data, days);

  return (
    <div className="rounded-lg border border-border bg-surface-1 p-5 shadow-sm">
      <div className="flex flex-col gap-2 border-b border-border pb-4">
        <h2 className="text-sm font-semibold text-text-1">Session Outcome Heatmap</h2>
        <p className="text-sm text-text-2">
          Last {days} days of completed test sessions, grouped by your local calendar day.
        </p>
      </div>

      {isLoading ? (
        <HeatmapSkeleton />
      ) : error ? (
        <div className="mt-5">
          <FetchError
            message="Could not load device session outcome heatmap."
            onRetry={() => {
              void refetch();
            }}
          />
        </div>
      ) : !heatmap.hasData ? (
        <div className="mt-4">
          <CompactHeatmapEmptyState days={days} />
        </div>
      ) : (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-text-2">
            <span>{heatmap.totalSessions} sessions across {heatmap.activeDays} active days</span>
            <span>{heatmap.passRate}% pass rate</span>
            <span>{heatmap.failed} failed</span>
            <span>{heatmap.error} error</span>
          </div>
          <div className="mt-5 flex gap-3 overflow-x-auto pb-1">
            <div className="grid grid-rows-7 gap-1 pt-6 text-[11px] text-text-3">
              {SESSION_OUTCOME_HEATMAP_WEEKDAY_LABELS.map((day, index) => (
                <div key={day} className="h-3.5 leading-4">
                  {index % 2 === 0 ? day.slice(0, 3) : ''}
                </div>
              ))}
            </div>
            <div className="grid grid-flow-col auto-cols-max gap-1">
              {heatmap.weeks.map((week) => (
                <div key={week.id} className="flex flex-col gap-1">
                  <div className="mb-1 h-4 text-[11px] font-medium text-text-3">{week.monthLabel ?? ''}</div>
                  {week.cells.map((cell) => (
                    <div
                      key={cell.dateKey}
                      data-testid={`session-outcome-cell-${cell.dateKey}`}
                      title={cell.title}
                      aria-label={cell.title}
                      className={[
                        'h-3.5 w-3.5 rounded-sm border transition-colors',
                        cellTone(cell),
                        cell.isToday && cell.inRange ? 'ring-1 ring-text-3 ring-offset-1' : '',
                      ].join(' ')}
                    />
                  ))}
                </div>
              ))}
            </div>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-text-2">
            <span className="font-medium uppercase tracking-wide text-text-3">Legend</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-3 w-3 rounded-sm border border-border bg-surface-2" /> No sessions</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-3 w-3 rounded-sm border border-success-strong bg-success-soft" /> Passed only</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-3 w-3 rounded-sm border border-warning-strong bg-warning-strong" /> Failed day</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-3 w-3 rounded-sm border border-danger-strong bg-danger-strong" /> Error day</span>
          </div>
        </>
      )}
    </div>
  );
}
