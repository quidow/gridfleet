import { Clock } from 'lucide-react';
import { useSessions } from '../hooks/useSessions';
import { useDevices } from '../hooks/useDevices';
import { useCursorQueryState } from '../hooks/useCursorQueryState';
import { Checkbox } from '../components/ui/Checkbox';
import { EmptyState } from '../components/ui/EmptyState';
import { DataTable } from '../components/ui/DataTable';
import { FilterBar } from '../components/ui/FilterBar';
import { CursorPagination } from '../components/ui/CursorPagination';
import { ListPageSubheader } from '../components/ui/ListPageSubheader';
import { PageHeader } from '../components/ui/PageHeader';
import { SectionErrorBoundary } from '../components/ErrorBoundary';
import { Select } from '../components/ui/Select';
import { DateInput } from '../components/ui/DateInput';
import { buildSessionColumns } from '../components/sessions/sessionColumns';
import { QueuedRequestsCard } from '../components/sessions/QueuedRequestsCard';
import type { SessionDetail, SessionSortKey, SessionStatus } from '../types';
import { SESSION_STATUS_LABELS, resolvePlatformLabel } from '../lib/labels';
import { usePageTitle } from '../hooks/usePageTitle';
import { dateOnlyToEndOfDayIso, dateOnlyToStartOfDayIso } from '../utils/dateFormatting';
import { useDriverPackCatalog } from '../hooks/useDriverPacks';
import { useGridQueue } from '../hooks/useGridQueue';

// 'pending' is the grid allocate->confirm window; those rows render in the list
// with a "Pending" badge, so the filter must be able to select them (wave-5 #27).
const SESSION_STATUSES: SessionStatus[] = ['running', 'pending', 'passed', 'failed', 'error'];

function readEnumSearchParam<T extends string>(searchParams: URLSearchParams, key: string, values: readonly T[]): T | '' {
  const value = searchParams.get(key);
  return value && values.includes(value as T) ? (value as T) : '';
}

const COLUMNS = buildSessionColumns();

function SessionsTableSection() {
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

  const deviceFilter = searchParams.get('device_id') ?? '';
  const statusFilter = readEnumSearchParam(searchParams, 'status', SESSION_STATUSES);
  const platformIdFilter = searchParams.get('platform_id') ?? '';
  const startedAfter = searchParams.get('started_after') ?? '';
  const startedBefore = searchParams.get('started_before') ?? '';
  const includeProbes = searchParams.get('include_probes') === '1';

  const { data: devices } = useDevices();
  const { data: queue } = useGridQueue();
  const { data: catalog = [] } = useDriverPackCatalog();

  // Build sorted list of unique platform options from catalog
  const platformOptions = catalog.flatMap((pack) =>
    (pack.platforms ?? []).map((p) => ({
      value: `${pack.id}:${p.id}`,
      label: resolvePlatformLabel(p.id, p.display_name),
    })),
  );

  const { data: sessions, isLoading, dataUpdatedAt } = useSessions({
    device_id: deviceFilter || undefined,
    status: statusFilter || undefined,
    platform_id: platformIdFilter || undefined,
    started_after: startedAfter ? dateOnlyToStartOfDayIso(startedAfter) : undefined,
    started_before: startedBefore ? dateOnlyToEndOfDayIso(startedBefore) : undefined,
    include_probes: includeProbes || undefined,
    limit: pageSize,
    cursor: cursor || undefined,
    direction,
  });

  const sortedDevices = (devices ?? []).toSorted((a, b) => a.name.localeCompare(b.name));
  const sessionRows = sessions?.items ?? [];
  const hasFilters = Boolean(
    deviceFilter || statusFilter || platformIdFilter || startedAfter || startedBefore || includeProbes,
  );
  const showingLabel = `Showing ${sessionRows.length} session${sessionRows.length === 1 ? '' : 's'}`;

  return (
    <>
      <PageHeader
        title="Sessions"
        subtitle="Cross-run history and sessions not attached to a run."
        updatedAt={dataUpdatedAt}
      />

      <div className="fade-in-stagger flex flex-col gap-4">
        <QueuedRequestsCard requests={queue?.requests ?? []} />
        <FilterBar
          onClear={
            hasFilters
              ? () => updateParams(
                {
                  device_id: null,
                  status: null,
                  platform_id: null,
                  started_after: null,
                  started_before: null,
                  include_probes: null,
                },
                { resetCursor: true },
              )
              : undefined
          }
        >
          <Select
            value={deviceFilter}
            onChange={(value) => updateParams({ device_id: value || null }, { resetCursor: true })}
            placeholder="All Devices"
            ariaLabel="Device"
            options={sortedDevices.map((device) => ({ value: device.id, label: device.name }))}
          />
          <Select
            value={statusFilter}
            onChange={(value) => updateParams({ status: value || null }, { resetCursor: true })}
            placeholder="All Statuses"
            ariaLabel="Status"
            options={SESSION_STATUSES.map((s) => ({ value: s, label: SESSION_STATUS_LABELS[s] }))}
          />
          <Select
            value={platformIdFilter}
            onChange={(value) => updateParams({ platform_id: value || null }, { resetCursor: true })}
            placeholder="All Platforms"
            ariaLabel="Platform"
            options={platformOptions}
          />
          <label className="flex items-center gap-2 text-sm text-text-2">
            <span>From</span>
            <DateInput
              ariaLabel="Started after"
              value={startedAfter}
              onChange={(value) => updateParams({ started_after: value || null }, { resetCursor: true })}
              size="sm"
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-text-2">
            <span>To</span>
            <DateInput
              ariaLabel="Started before"
              value={startedBefore}
              onChange={(value) => updateParams({ started_before: value || null }, { resetCursor: true })}
              size="sm"
            />
          </label>
          <Checkbox
            checked={includeProbes}
            onChange={(checked) =>
              updateParams({ include_probes: checked ? '1' : null }, { resetCursor: true })
            }
            label="Include probes"
            aria-label="Include probe sessions"
          />
        </FilterBar>

        <ListPageSubheader title={showingLabel} />

        <DataTable<SessionDetail, SessionSortKey>
          columns={COLUMNS}
          rows={sessionRows}
          rowKey={(s) => s.id}
          loading={isLoading}
          emptyState={
            <EmptyState
              icon={Clock}
              title="No sessions found"
              description={hasFilters ? 'Try adjusting your filters.' : 'Sessions will appear here when tests run through the Grid.'}
            />
          }
        />

        <CursorPagination
          pageSize={pageSize}
          nextCursor={sessions?.next_cursor ?? null}
          prevCursor={sessions?.prev_cursor ?? null}
          isNewestPage={!cursor}
          onOlder={goOlder}
          onNewer={goNewer}
          onBackToNewest={resetToNewest}
          onPageSizeChange={setPageSize}
        />
      </div>
    </>
  );
}

export function Sessions() {
  usePageTitle('Sessions');

  return (
    <div>
      <SectionErrorBoundary scope="sessions-table">
        <SessionsTableSection />
      </SectionErrorBoundary>
    </div>
  );
}
