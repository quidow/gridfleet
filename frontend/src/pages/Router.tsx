import { useMemo, useState } from 'react';
import { Network } from 'lucide-react';

import { EmptyState } from '../components/ui/EmptyState';
import { FetchError } from '../components/ui/FetchError';
import { FilterBar } from '../components/ui/FilterBar';
import { PageHeader } from '../components/ui/PageHeader';
import { Select } from '../components/ui/Select';
import { LoadingSpinner } from '../components/LoadingSpinner';
import { SectionErrorBoundary } from '../components/ErrorBoundary';
import { useGridRouter } from '../hooks/useGridRouter';
import { NodeCard } from './router/NodeCard';
import { QueueTile } from './router/QueueTile';
import { RouterSummaryPills } from './router/RouterSummaryPills';

const STATES = ['all', 'available', 'busy', 'verifying', 'offline', 'maintenance'] as const;

export function RouterPage() {
  const { data, isLoading, isError, refetch, dataUpdatedAt } = useGridRouter();
  const [search, setSearch] = useState('');
  const [state, setState] = useState<(typeof STATES)[number]>('all');

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.trim().toLowerCase();
    return data.nodes.filter(
      (node) =>
        (state === 'all' || node.operational_state === state) &&
        node.device_name.toLowerCase().includes(q),
    );
  }, [data, state, search]);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Router"
        subtitle="Grid nodes, routing stereotypes, and the live allocation queue."
        updatedAt={data ? new Date(dataUpdatedAt) : null}
        summary={data ? <RouterSummaryPills counts={data.counts} /> : undefined}
      />

      {isLoading ? <LoadingSpinner /> : null}
      {isError && !data ? <FetchError onRetry={() => refetch()} /> : null}

      {data ? (
        <SectionErrorBoundary scope="router-grid">
          <div className="space-y-4">
            <div>
              <FilterBar
                onClear={() => {
                  setSearch('');
                  setState('all');
                }}
              >
                <input
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Search device…"
                  aria-label="Search device"
                  className="rounded-md border border-border bg-surface-2 px-3 py-1.5 text-sm"
                />
                <Select
                  value={state}
                  onChange={(value) => setState(value as (typeof STATES)[number])}
                  ariaLabel="State filter"
                  size="sm"
                  options={STATES.map((value) => ({ value, label: value === 'all' ? 'All states' : value }))}
                />
              </FilterBar>

              {filtered.length === 0 ? (
                <EmptyState
                  icon={Network}
                  title={data.nodes.length === 0 ? 'No devices' : 'No matches'}
                  description={
                    data.nodes.length === 0
                      ? 'No devices are registered in the grid.'
                      : 'No devices match the current filters.'
                  }
                  className="mt-3"
                />
              ) : (
                <div className="mt-3 grid grid-cols-[repeat(auto-fill,minmax(min(30rem,100%),1fr))] gap-3">
                  {filtered.map((node) => (
                    <NodeCard key={node.device_id} node={node} />
                  ))}
                </div>
              )}
            </div>

            <QueueTile queue={data.queue} />
          </div>
        </SectionErrorBoundary>
      ) : null}
    </div>
  );
}
