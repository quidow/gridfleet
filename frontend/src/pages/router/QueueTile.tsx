import { Link } from 'react-router-dom';

import { formatWaitTime } from '../../utils/dateFormatting';
import type { GridRouterRead } from '../../types/gridRouter';

export function QueueTile({ queue }: { queue: GridRouterRead['queue'] }) {
  return (
    <div className="rounded-xl border border-border bg-surface-1 p-4">
      <h2 className="text-sm font-semibold">Queue ({queue.length})</h2>
      {queue.length === 0 ? (
        <p className="mt-3 text-xs text-text-3">No queued requests.</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {queue.map((entry) => (
            <li key={entry.requestId} className="rounded-lg border border-border bg-surface-2 px-3 py-2 text-xs">
              <div className="break-all font-mono text-text-2">{JSON.stringify(entry.capabilities)}</div>
              <div className="mt-1 flex items-center gap-2 text-text-3">
                <span>{formatWaitTime(entry.requestTimestamp)}</span>
                <span>·</span>
                {entry.runId ? (
                  <Link to={`/runs/${entry.runId}`} className="hover:underline">
                    run
                  </Link>
                ) : (
                  <span>free</span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
