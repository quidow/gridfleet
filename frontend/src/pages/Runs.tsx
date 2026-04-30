import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Play } from 'lucide-react';
import { useRuns, useCancelRun, useForceReleaseRun } from '../hooks/useRuns';
import { useCursorQueryState } from '../hooks/useCursorQueryState';
import StatusBadge from '../components/StatusBadge';
import EmptyState from '../components/ui/EmptyState';
import DataTable from '../components/ui/DataTable';
import FilterBar from '../components/ui/FilterBar';
import CursorPagination from '../components/ui/CursorPagination';
import type { DataTableColumn } from '../components/ui/DataTable';
import ConfirmDialog from '../components/ui/ConfirmDialog';
import type { RunRead, RunSortKey, RunState } from '../types';
import { usePageTitle } from '../hooks/usePageTitle';
import { formatDateTime, formatDuration } from '../utils/dateFormatting';
import FetchError from '../components/ui/FetchError';
import RunProgressBar from '../components/runs/RunProgressBar';
import RunsSummaryRow from '../components/runs/RunsSummaryRow';
import RunActionButtons from '../components/runs/RunActionButtons';
import Button from '../components/ui/Button';
import PageHeader from '../components/ui/PageHeader';
import Select from '../components/ui/Select';
import DateInput from '../components/ui/DateInput';
import { resolvePlatformLabel } from '../lib/labels';

const RUN_STATES: RunState[] = [
  'pending', 'preparing', 'ready', 'active', 'completing',
  'completed', 'failed', 'expired', 'cancelled',
];
const ACTIVE_STATES: RunState[] = ['pending', 'preparing', 'ready', 'active', 'completing'];

function readEnumSearchParam<T extends string>(searchParams: URLSearchParams, key: string, values: readonly T[]): T | '' {
  const value = searchParams.get(key);
  return value && values.includes(value as T) ? (value as T) : '';
}

function platformSummary(
  requirements: Array<{ platform_id: string; count?: number | null; allocation?: string | null; min_count?: number | null }>,
): string {
  return requirements.map((r) => {
    const label = resolvePlatformLabel(r.platform_id, null);
    if (r.allocation === 'all_available') {
      return `all available ${label} (min ${r.min_count ?? 1})`;
    }
    return `${r.count ?? 1}x ${label}`;
  }).join(', ');
}

export default function Runs() {
  usePageTitle('Test Runs');
  const {
    searchParams,
    pageSize,
    direction,
    updateParams,
    setPageSize,
    cursor,
    goOlder,
    goNewer,
    resetToNewest,
  } = useCursorQueryState({
    defaultPageSize: 50,
  });
  const stateFilter = readEnumSearchParam(searchParams, 'state', RUN_STATES);
  const createdFrom = searchParams.get('created_from') ?? '';
  const createdTo = searchParams.get('created_to') ?? '';
  const { data: runs, isLoading, isError, refetch, dataUpdatedAt } = useRuns({
    state: stateFilter || undefined,
    created_from: createdFrom || undefined,
    created_to: createdTo || undefined,
    limit: pageSize,
    cursor: cursor || undefined,
    direction,
  });
  const [last24hParams] = useState(() => {
    const from = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    return { created_from: from, limit: 200 };
  });
  const { data: last24h } = useRuns(last24hParams);
  const cancelMutation = useCancelRun();
  const forceReleaseMutation = useForceReleaseRun();

  const [cancelTarget, setCancelTarget] = useState<string | null>(null);
  const [forceReleaseTarget, setForceReleaseTarget] = useState<string | null>(null);
  const runRows = runs?.items ?? [];
  const hasFilters = Boolean(stateFilter || createdFrom || createdTo);

  const columns: DataTableColumn<RunRead, RunSortKey>[] = [
    {
      key: 'name',
      header: 'Name',
      sortKey: 'name',
      render: (run) => (
        <Link to={`/runs/${run.id}`} className="text-accent hover:text-accent-hover text-sm">{run.name}</Link>
      ),
    },
    {
      key: 'state',
      header: 'State',
      sortKey: 'state',
      render: (run) => <StatusBadge status={run.state} />,
    },
    {
      key: 'progress',
      header: 'Progress',
      render: (run) => <RunProgressBar counts={run.session_counts} />,
    },
    {
      key: 'devices',
      header: 'Devices',
      sortKey: 'devices',
      render: (run) => (
        <span className="text-sm text-text-2">
          {run.reserved_devices?.length ?? 0} ({platformSummary(run.requirements)})
        </span>
      ),
    },
    {
      key: 'created_by',
      header: 'Created By',
      sortKey: 'created_by',
      render: (run) => <span className="text-sm text-text-3">{run.created_by ?? '-'}</span>,
    },
    {
      key: 'created_at',
      header: 'Created',
      sortKey: 'created_at',
      render: (run) => <span className="text-sm text-text-3">{formatDateTime(run.created_at)}</span>,
    },
    {
      key: 'duration',
      header: 'Duration',
      sortKey: 'duration',
      render: (run) => <span className="text-sm text-text-3">{formatDuration(run.created_at, run.completed_at)}</span>,
    },
    {
      key: 'actions',
      header: 'Actions',
      render: (run) => {
        const isActive = ACTIVE_STATES.includes(run.state);
        return (
          <div className="flex items-center gap-2">
            <Link to={`/runs/${run.id}`}>
              <Button variant="ghost" size="sm">View</Button>
            </Link>
            {isActive && (
              <RunActionButtons
                onCancel={() => setCancelTarget(run.id)}
                onForceRelease={() => setForceReleaseTarget(run.id)}
              />
            )}
          </div>
        );
      },
    },
  ];

  return (
    <div>
      <PageHeader
        title="Test Runs"
        subtitle="Device reservations for CI test runs"
        updatedAt={dataUpdatedAt}
        summary={
          <RunsSummaryRow
            currentPageRuns={runRows}
            last24hRuns={last24h?.items}
          />
        }
      />

      <div className="fade-in-stagger flex flex-col gap-4">
      <FilterBar
        onClear={
          hasFilters
            ? () => updateParams(
              {
                state: null,
                created_from: null,
                created_to: null,
              },
              { resetCursor: true },
            )
            : undefined
        }
      >
        <Select
          value={stateFilter}
          onChange={(value) => updateParams({ state: value || null }, { resetCursor: true })}
          placeholder="All States"
          ariaLabel="Run state"
          options={RUN_STATES.map((s) => ({ value: s, label: s }))}
        />
        <DateInput
          value={createdFrom}
          onChange={(value) => updateParams({ created_from: value || null }, { resetCursor: true })}
          ariaLabel="Created from"
        />
        <DateInput
          value={createdTo}
          onChange={(value) => updateParams({ created_to: value || null }, { resetCursor: true })}
          ariaLabel="Created to"
        />
      </FilterBar>

      {isError && (
        <FetchError
          message="Could not load test runs. Check your connection and try again."
          onRetry={() => void refetch()}
        />
      )}

      <DataTable<RunRead, RunSortKey>
        columns={columns}
        rows={runRows}
        rowKey={(run) => run.id}
        loading={isLoading}
        emptyState={
          <EmptyState
            icon={Play}
            title="No test runs found"
            description={hasFilters ? 'Try adjusting your filters.' : 'Test runs will appear here when CI reserves devices.'}
          />
        }
      />

      <CursorPagination
        pageSize={pageSize}
        nextCursor={runs?.next_cursor ?? null}
        prevCursor={runs?.prev_cursor ?? null}
        isNewestPage={!cursor}
        onOlder={goOlder}
        onNewer={goNewer}
        onBackToNewest={resetToNewest}
        onPageSizeChange={setPageSize}
      />
      </div>

      <ConfirmDialog
        isOpen={!!cancelTarget}
        onClose={() => setCancelTarget(null)}
        onConfirm={() => { if (cancelTarget) cancelMutation.mutate(cancelTarget); }}
        title="Cancel Run?"
        message="This will cancel the run and release all reserved devices."
        confirmLabel="Cancel Run"
        variant="default"
      />

      <ConfirmDialog
        isOpen={!!forceReleaseTarget}
        onClose={() => setForceReleaseTarget(null)}
        onConfirm={() => { if (forceReleaseTarget) forceReleaseMutation.mutate(forceReleaseTarget); }}
        title="Force Release?"
        message="This will force release all devices regardless of run state. Use this for stuck runs."
        confirmLabel="Force Release"
        variant="danger"
      />
    </div>
  );
}
