import { useMemo, useState } from 'react';

import { useHostEvents } from '../../hooks/useHosts';
import { Select } from '../ui/Select';
import { TextField } from '../ui/TextField';
import { Button } from '../ui/Button';
import { DataTable } from '../ui';
import type { DataTableColumn } from '../ui';
import type { HostEventEntry } from '../../types';
type RangeKey = '1h' | '6h' | '24h' | '7d';

interface Props {
  hostId: string;
}

const RANGES: Record<RangeKey, number> = { '1h': 1, '6h': 6, '24h': 24, '7d': 24 * 7 };

const SUMMARY_PREFERRED_KEYS = ['old_status', 'new_status', 'pack_id', 'stale_for_sec'];

function summarize(data: Record<string, unknown>): string {
  const known = SUMMARY_PREFERRED_KEYS.filter((key) => key in data);
  if (known.length > 0) {
    return known.map((key) => `${key}=${String(data[key])}`).join(' ');
  }
  return Object.keys(data)
    .slice(0, 3)
    .map((key) => `${key}=${String(data[key])}`)
    .join(' ');
}

const TIME_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  dateStyle: 'short',
  timeStyle: 'medium',
});

function formatEventTime(ts: string): string {
  const date = new Date(ts);
  return Number.isNaN(date.getTime()) ? ts : TIME_FORMATTER.format(date);
}

export function HostEventsPanel({ hostId }: Props) {
  const [range, setRange] = useState<RangeKey>('24h');
  const [typesText, setTypesText] = useState('');
  const [limit, setLimit] = useState(50);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  const since = useMemo(() => {
    const date = new Date();
    date.setHours(date.getHours() - RANGES[range]);
    return date.toISOString();
  }, [range]);
  const types = useMemo(
    () => typesText.split(',').map((item) => item.trim()).filter(Boolean),
    [typesText],
  );

  const { data, isLoading } = useHostEvents(hostId, {
    since,
    types: types.length > 0 ? types : undefined,
    limit,
  });

  const events = data?.events ?? [];

  const columns: DataTableColumn<HostEventEntry>[] = [
    {
      key: 'ts',
      header: 'Time',
      render: (event) => (
        <span className="whitespace-nowrap font-mono text-xs text-text-2">{formatEventTime(event.ts)}</span>
      ),
    },
    {
      key: 'type',
      header: 'Type',
      render: (event) => (
        <span className="text-sm font-medium text-text-1">{event.type}</span>
      ),
    },
    {
      key: 'payload',
      header: 'Payload',
      render: (event) => (
        <span className="text-sm text-text-2">{summarize(event.data)}</span>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-text-2">
          Range
          <Select
            ariaLabel="Range"
            value={range}
            onChange={(value) => setRange(value as RangeKey)}
            options={(Object.keys(RANGES) as RangeKey[]).map((key) => ({ value: key, label: key }))}
            size="sm"
          />
        </label>
        <TextField
          aria-label="Event types"
          placeholder="host.status_changed,host.heartbeat_lost"
          value={typesText}
          onChange={setTypesText}
          size="sm"
          className="min-w-72 flex-1"
        />
      </div>

      <DataTable<HostEventEntry>
        columns={columns}
        rows={events}
        rowKey={(event) => event.event_id}
        loading={isLoading}
        emptyState={<p className="px-5 py-8 text-center text-sm text-text-3">No events in this time range.</p>}
        onRowClick={(event) => {
          setExpandedIds((prev) => {
            const next = new Set(prev);
            if (next.has(event.event_id)) next.delete(event.event_id);
            else next.add(event.event_id);
            return next;
          });
        }}
        renderExpandedRow={(event) => {
          if (!expandedIds.has(event.event_id)) return null;
          return (
            <pre className="whitespace-pre-wrap font-mono text-xs text-text-2">
              {JSON.stringify(event.data, null, 2)}
            </pre>
          );
        }}
      />

      {data?.has_more ? (
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setLimit((current) => current + 50)}
          className="self-start"
        >
          Load more
        </Button>
      ) : null}
    </div>
  );
}
