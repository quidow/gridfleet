import { useEffect } from 'react';
import { Bell } from 'lucide-react';
import { useNotifications } from '../hooks/useNotifications';
import { useEventCatalog } from '../hooks/useEventCatalog';
import { usePaginatedQueryState } from '../hooks/usePaginatedQueryState';
import DataTable from '../components/ui/DataTable';
import type { DataTableColumn } from '../components/ui/DataTable';
import EmptyState from '../components/ui/EmptyState';
import FilterBar from '../components/ui/FilterBar';
import ListPageSubheader from '../components/ui/ListPageSubheader';
import Pagination from '../components/ui/Pagination';
import { usePageTitle } from '../hooks/usePageTitle';
import { formatDateTime } from '../utils/dateFormatting';
import type { SystemEventRead } from '../types';
import FetchError from '../components/ui/FetchError';
import PageHeader from '../components/ui/PageHeader';
import Select from '../components/ui/Select';
import Badge from '../components/ui/Badge';
import SeverityBadge from '../components/notifications/SeverityBadge';
import EventDetailsCell from '../components/notifications/EventDetailsCell';
import {
  EVENT_SEVERITY_LABEL,
  type EventSeverity,
} from '../components/notifications/eventRegistry';

const COLUMNS: DataTableColumn<SystemEventRead>[] = [
  {
    key: 'timestamp',
    header: 'Time',
    render: (row) => (
      <span className="text-sm text-text-3 whitespace-nowrap">{formatDateTime(row.timestamp)}</span>
    ),
  },
  {
    key: 'severity',
    header: 'Severity',
    render: (row) => <SeverityBadge event={row} />,
  },
  {
    key: 'type',
    header: 'Event',
    render: (row) => (
      <code className="font-mono text-xs text-text-2 whitespace-nowrap">{row.type}</code>
    ),
  },
  {
    key: 'details',
    header: 'Details',
    render: (row) => <EventDetailsCell type={row.type} data={row.data} />,
  },
];

const SEVERITIES: EventSeverity[] = ['info', 'success', 'warning', 'critical', 'neutral'];

function parseSeverities(raw: string | null): EventSeverity[] {
  if (!raw) return [];
  const tokens = raw
    .split(',')
    .map((t) => t.trim())
    .filter((t): t is EventSeverity => SEVERITIES.includes(t as EventSeverity));
  return Array.from(new Set(tokens));
}

interface SeverityChipFilterProps {
  selected: EventSeverity[];
  onToggle: (severity: EventSeverity) => void;
}

function SeverityChipFilter({ selected, onToggle }: SeverityChipFilterProps) {
  const selectedSet = new Set(selected);
  return (
    <div className="flex flex-wrap gap-1.5">
      {SEVERITIES.map((s) => {
        const active = selectedSet.has(s);
        return (
          <button
            key={s}
            type="button"
            aria-pressed={active}
            onClick={() => onToggle(s)}
            className={[
              'rounded-full transition-opacity',
              active ? '' : 'opacity-50 hover:opacity-75',
            ].join(' ')}
          >
            <Badge tone={s} dot>
              {EVENT_SEVERITY_LABEL[s]}
            </Badge>
          </button>
        );
      })}
    </div>
  );
}

export default function Notifications() {
  usePageTitle('Notifications');
  const {
    searchParams,
    page,
    pageSize,
    updateParams,
    setPage,
    setPageSize,
  } = usePaginatedQueryState({
    defaultPageSize: 25,
  });
  const filterType = searchParams.get('type') ?? '';
  const severities = parseSeverities(searchParams.get('severity'));

  const { data: eventCatalog, isLoading: eventCatalogLoading } = useEventCatalog();
  const types = filterType ? [filterType] : undefined;
  const { data: events, isLoading, isError, refetch, dataUpdatedAt } = useNotifications({
    types,
    severities: severities.length ? severities : undefined,
    limit: pageSize,
    offset: (page - 1) * pageSize,
  });

  const eventTypes = eventCatalog?.map((event) => event.name) ?? [];
  const eventRows = events?.items ?? [];
  const showingLabel = `Showing ${eventRows.length} notification${eventRows.length === 1 ? '' : 's'}`;
  const anyFilterActive = Boolean(filterType) || severities.length > 0;

  useEffect(() => {
    if (!isLoading && events && page > 1 && events.items.length === 0 && events.total > 0) {
      setPage(1);
    }
  }, [events, isLoading, page, setPage]);

  function toggleSeverity(target: EventSeverity) {
    const next = severities.includes(target)
      ? severities.filter((s) => s !== target)
      : [...severities, target];
    updateParams(
      { severity: next.length ? next.join(',') : null },
      { resetPage: true },
    );
  }

  function clearAll() {
    updateParams({ type: null, severity: null }, { resetPage: true });
  }

  return (
    <div>
      <PageHeader
        title="Notifications"
        subtitle="System-wide event stream"
        updatedAt={dataUpdatedAt}
      />

      <div className="fade-in-stagger flex flex-col gap-4">
        <FilterBar onClear={anyFilterActive ? clearAll : undefined}>
          <Select
            value={filterType}
            onChange={(value) => updateParams({ type: value || null }, { resetPage: true })}
            placeholder="All Events"
            ariaLabel="Event type"
            size="sm"
            options={eventTypes.map((t) => ({ value: t, label: t }))}
          />
          <SeverityChipFilter selected={severities} onToggle={toggleSeverity} />
        </FilterBar>

        <ListPageSubheader title={showingLabel} />

        {isError && (
          <FetchError
            message="Could not load notifications. Check your connection and try again."
            onRetry={() => void refetch()}
          />
        )}

        <DataTable
          columns={COLUMNS}
          rows={eventRows}
          rowKey={(row) => row.id}
          loading={isLoading || eventCatalogLoading}
          emptyState={
            <EmptyState
              icon={Bell}
              title="No events yet"
              description={anyFilterActive ? 'Try adjusting your filters.' : 'Events will appear here as they occur.'}
            />
          }
        />

        <Pagination
          page={page}
          pageSize={pageSize}
          total={events?.total ?? 0}
          onPageChange={setPage}
          onPageSizeChange={setPageSize}
        />
      </div>
    </div>
  );
}
