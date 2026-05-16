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
import SeverityBadge from '../components/notifications/SeverityBadge';
import EventDetailsCell from '../components/notifications/EventDetailsCell';

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
  const { data: eventCatalog, isLoading: eventCatalogLoading } = useEventCatalog();
  const types = filterType ? [filterType] : undefined;
  const { data: events, isLoading, isError, refetch, dataUpdatedAt } = useNotifications({
    types,
    limit: pageSize,
    offset: (page - 1) * pageSize,
  });

  const eventTypes = eventCatalog?.map((event) => event.name) ?? [];
  const eventRows = events?.items ?? [];
  const showingLabel = `Showing ${eventRows.length} notification${eventRows.length === 1 ? '' : 's'}`;

  useEffect(() => {
    if (!isLoading && events && page > 1 && events.items.length === 0 && events.total > 0) {
      setPage(1);
    }
  }, [events, isLoading, page, setPage]);

  return (
    <div>
      <PageHeader
        title="Notifications"
        subtitle="System-wide event stream"
        updatedAt={dataUpdatedAt}
      />

      <div className="fade-in-stagger flex flex-col gap-4">
        <FilterBar onClear={filterType ? () => updateParams({ type: null }, { resetPage: true }) : undefined}>
          <Select
            value={filterType}
            onChange={(value) => updateParams({ type: value || null }, { resetPage: true })}
            placeholder="All Events"
            ariaLabel="Event type"
            size="sm"
            options={eventTypes.map((t) => ({ value: t, label: t }))}
          />
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
              description={filterType ? 'Try adjusting your filters.' : 'Events will appear here as they occur.'}
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
