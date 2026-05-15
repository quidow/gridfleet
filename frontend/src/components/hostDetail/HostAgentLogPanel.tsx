import { useEffect, useMemo, useState } from 'react';

import { useHostAgentLogs } from '../../hooks/useHosts';
import LoadingSpinner from '../LoadingSpinner';
import FetchError from '../ui/FetchError';
import Select from '../ui/Select';

type Level = 'INFO' | 'WARN' | 'ERROR';
type LineCount = 100 | 500 | 1000 | 2000;

interface Props {
  hostId: string;
}

const LEVEL_OPTIONS: { value: Level; label: string }[] = [
  { value: 'INFO', label: 'INFO+' },
  { value: 'WARN', label: 'WARN+' },
  { value: 'ERROR', label: 'ERROR+' },
];

const LINE_COUNTS: LineCount[] = [100, 500, 1000, 2000];

export default function HostAgentLogPanel({ hostId }: Props) {
  const [level, setLevel] = useState<Level>('INFO');
  const [q, setQ] = useState('');
  const [limit, setLimit] = useState<LineCount>(500);
  const debouncedQ = useDebounce(q, 250);
  const filters = useMemo(
    () => ({ level, q: debouncedQ || undefined, limit }),
    [level, debouncedQ, limit],
  );
  const { data, isLoading, error, refetch } = useHostAgentLogs(hostId, filters);

  if (isLoading) return <LoadingSpinner />;
  if (error) return <FetchError message="Could not load agent logs." onRetry={() => void refetch()} />;

  const lines = data?.lines ?? [];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3 border-b border-border-subtle pb-3">
        <label className="flex items-center gap-2 text-sm text-text-2">
          Level
          <Select
            ariaLabel="Level"
            value={level}
            onChange={(value) => setLevel(value as Level)}
            options={LEVEL_OPTIONS}
            size="sm"
          />
        </label>
        <input
          aria-label="Search"
          placeholder="Search messages"
          value={q}
          onChange={(event) => setQ(event.target.value)}
          className="min-w-52 flex-1 rounded-md border border-border-subtle bg-surface-1 px-2 py-1 text-sm text-text-1"
        />
        <label className="flex items-center gap-2 text-sm text-text-2">
          Lines
          <Select
            ariaLabel="Line count"
            value={String(limit)}
            onChange={(value) => setLimit(Number(value) as LineCount)}
            options={LINE_COUNTS.map((count) => ({ value: String(count), label: String(count) }))}
            size="sm"
          />
        </label>
      </div>

      {lines.length === 0 ? (
        <div className="border border-dashed border-border-subtle px-4 py-6 text-sm text-text-3">
          No logs received yet. Agent may be offline or shipping disabled.
        </div>
      ) : (
        <div className="overflow-x-auto font-mono text-xs leading-relaxed">
          {lines.map((line, index) => {
            const previous = index > 0 ? lines[index - 1] : null;
            const showBoundary = previous && previous.boot_id !== line.boot_id;
            return (
              <div key={`${line.boot_id}-${line.sequence_no}`}>
                {showBoundary ? (
                  <div className="my-2 border-y border-border-subtle py-1 text-center text-text-3">
                    agent restarted at {line.ts}
                  </div>
                ) : null}
                <pre className={`whitespace-pre-wrap border-l-2 py-1 pl-2 ${levelStripe(line.level)}`}>
                  {line.ts} {line.level} [{line.logger_name}] {line.message}
                </pre>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function levelStripe(level: string): string {
  switch (level) {
    case 'ERROR':
    case 'CRITICAL':
      return 'border-red-500';
    case 'WARNING':
      return 'border-amber-500';
    default:
      return 'border-border-subtle';
  }
}

function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}
