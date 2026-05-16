import { useMemo, useState } from 'react';

import { useHostEvents } from '../../hooks/useHosts';
import { LoadingSpinner } from '../LoadingSpinner';
import FetchError from '../ui/FetchError';
import Select from '../ui/Select';

type RangeKey = '1h' | '6h' | '24h' | '7d';

interface Props {
  hostId: string;
}

const RANGES: Record<RangeKey, number> = { '1h': 1, '6h': 6, '24h': 24, '7d': 24 * 7 };

export default function HostEventsPanel({ hostId }: Props) {
  const [range, setRange] = useState<RangeKey>('24h');
  const [typesText, setTypesText] = useState('');
  const [limit, setLimit] = useState(50);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const since = useMemo(() => {
    const date = new Date();
    date.setHours(date.getHours() - RANGES[range]);
    return date.toISOString();
  }, [range]);
  const types = useMemo(
    () => typesText.split(',').map((item) => item.trim()).filter(Boolean),
    [typesText],
  );

  const { data, isLoading, error, refetch } = useHostEvents(hostId, {
    since,
    types: types.length > 0 ? types : undefined,
    limit,
  });

  if (isLoading) return <LoadingSpinner />;
  if (error) return <FetchError message="Could not load host events." onRetry={() => void refetch()} />;

  const events = data?.events ?? [];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3 border-b border-border-subtle pb-3">
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
        <input
          aria-label="Event types"
          placeholder="host.status_changed,host.heartbeat_lost"
          value={typesText}
          onChange={(event) => setTypesText(event.target.value)}
          className="min-w-72 flex-1 rounded-md border border-border-subtle bg-surface-1 px-2 py-1 text-sm text-text-1"
        />
      </div>

      {events.length === 0 ? (
        <div className="border border-dashed border-border-subtle px-4 py-6 text-sm text-text-3">
          No events in this time range.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase text-text-3">
              <tr>
                <th className="px-2 py-2 font-medium">Time</th>
                <th className="px-2 py-2 font-medium">Type</th>
                <th className="px-2 py-2 font-medium">Payload</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event) => {
                const isExpanded = Boolean(expanded[event.event_id]);
                return (
                  <tr key={event.event_id} className="border-t border-border-subtle align-top">
                    <td className="whitespace-nowrap px-2 py-2 font-mono text-xs text-text-3">{event.ts}</td>
                    <td className="px-2 py-2">
                      <button
                        type="button"
                        className="text-left font-medium text-text-1 hover:text-accent"
                        onClick={() =>
                          setExpanded((current) => ({ ...current, [event.event_id]: !current[event.event_id] }))
                        }
                      >
                        {event.type}
                      </button>
                    </td>
                    <td className="px-2 py-2">
                      {isExpanded ? (
                        <pre className="whitespace-pre-wrap font-mono text-xs text-text-2">
                          {JSON.stringify(event.data, null, 2)}
                        </pre>
                      ) : (
                        <span className="text-text-3">{summarize(event.data)}</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {data?.has_more ? (
        <button
          type="button"
          onClick={() => setLimit((current) => current + 50)}
          className="self-start rounded-md border border-border-subtle px-3 py-1 text-sm text-text-1 hover:border-border-strong"
        >
          Load more
        </button>
      ) : null}
    </div>
  );
}

const SUMMARY_PREFERRED_KEYS = ['old_status', 'new_status', 'pack_id', 'missed_count'];

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
