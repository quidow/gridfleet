import { useEffect } from 'react';
import { Bell } from 'lucide-react';
import { useNotifications } from '../hooks/useNotifications';
import { useEventCatalog } from '../hooks/useEventCatalog';
import { usePaginatedQueryState } from '../hooks/usePaginatedQueryState';
import { DataTable } from '../components/ui/DataTable';
import type { DataTableColumn } from '../components/ui/DataTable';
import { EmptyState } from '../components/ui/EmptyState';
import { FilterBar } from '../components/ui/FilterBar';
import { ListPageSubheader } from '../components/ui/ListPageSubheader';
import { Pagination } from '../components/ui/Pagination';
import { usePageTitle } from '../hooks/usePageTitle';
import { formatDateTime } from '../utils/dateFormatting';
import type { SystemEventRead } from '../types';
import { PageHeader } from '../components/ui/PageHeader';
import { ChevronDown } from 'lucide-react';
import { Select } from '../components/ui/Select';
import { Popover } from '../components/ui/Popover';
import { Checkbox } from '../components/ui/Checkbox';
import { Badge } from '../components/ui/Badge';
import { SeverityBadge } from '../components/notifications/SeverityBadge';
import { EventDetailsCell } from '../components/notifications/EventDetailsCell';
import {
  EVENT_SEVERITY_LABEL,
  type EventSeverity,
} from '../components/notifications/eventRegistry';
import { SectionErrorBoundary } from '../components/ErrorBoundary';

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

function severityPlaceholder(selected: EventSeverity[]): string {
  if (selected.length === 0 || selected.length === SEVERITIES.length) return 'All Severities';
  if (selected.length === 1) return EVENT_SEVERITY_LABEL[selected[0]];
  return `${selected.length} severities`;
}

function isSeverityChecked(severities: EventSeverity[], s: EventSeverity): boolean {
  return severities.length === 0 || severities.includes(s);
}

function toggleSeverityInSet(severities: EventSeverity[], target: EventSeverity): EventSeverity[] {
  if (severities.length === 0) {
    return SEVERITIES.filter((s) => s !== target);
  }
  const next = severities.includes(target)
    ? severities.filter((s) => s !== target)
    : [...severities, target];
  if (next.length === SEVERITIES.length) return [];
  return next;
}

function NotificationsContent() {
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
  const { data: events, isLoading, dataUpdatedAt } = useNotifications({
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
    updateParams(
      {
        severity: (prev) => {
          const next = toggleSeverityInSet(parseSeverities(prev), target);
          return next.length ? next.join(',') : null;
        },
      },
      { resetPage: true },
    );
  }

  function clearAll() {
    updateParams({ type: null, severity: null }, { resetPage: true });
  }

  return (
    <>
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
          <Popover
            ariaLabel="Severity filter"
            triggerClassName="inline-flex items-center gap-1.5 rounded-md border border-border-strong bg-surface-1 px-2 py-1.5 text-xs text-text-1 focus:outline-none focus:ring-2 focus:ring-accent"
            contentClassName="rounded-lg border border-border bg-surface-1 p-2 shadow-lg"
            trigger={<>{severityPlaceholder(severities)}<ChevronDown size={14} className="text-text-3" /></>}
          >
            <div className="flex flex-col gap-1">
              {SEVERITIES.map((s) => (
                <div key={s} className="px-2 py-1">
                  <Checkbox
                    checked={isSeverityChecked(severities, s)}
                    onChange={() => toggleSeverity(s)}
                    label={<Badge tone={s} dot size="sm">{EVENT_SEVERITY_LABEL[s]}</Badge>}
                  />
                </div>
              ))}
            </div>
          </Popover>
        </FilterBar>

        <ListPageSubheader title={showingLabel} />

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
    </>
  );
}

export function Notifications() {
  usePageTitle('Notifications');

  return (
    <div>
      <SectionErrorBoundary scope="notifications">
        <NotificationsContent />
      </SectionErrorBoundary>
    </div>
  );
}
