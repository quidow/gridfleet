import { Suspense, lazy, useCallback, useState } from 'react';

const HostTerminalView = lazy(() => import('./HostTerminalView'));

interface HostTerminalPanelProps {
  hostId: string;
  hostOnline: boolean;
  terminalEnabled: boolean;
}

type SessionStatus = 'connecting' | 'connected' | 'error' | 'closed';

export default function HostTerminalPanel({ hostId, hostOnline, terminalEnabled }: HostTerminalPanelProps) {
  // `attempt` is bumped to remount HostTerminalView for reconnect.
  const [attempt, setAttempt] = useState(0);
  const [opened, setOpened] = useState(false);
  const [status, setStatus] = useState<SessionStatus | 'idle'>('idle');

  const handleStatusChange = useCallback((next: SessionStatus) => {
    setStatus(next);
  }, []);

  if (!terminalEnabled) {
    return (
      <div className="rounded-lg border border-border bg-surface-1 p-4 text-text-2">
        Web terminal is not enabled for this deployment. Ask an operator to set{' '}
        <code className="font-mono text-sm">GRIDFLEET_ENABLE_WEB_TERMINAL=true</code> and{' '}
        <code className="font-mono text-sm">AGENT_ENABLE_WEB_TERMINAL=true</code>.
      </div>
    );
  }

  if (!hostOnline) {
    return (
      <div className="rounded-lg border border-border bg-surface-1 p-4 text-text-2">
        Host must be online before opening a terminal session.
      </div>
    );
  }

  const start = () => {
    setOpened(true);
    setAttempt((n) => n + 1);
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm text-text-2">
        <span>Status: {status}</span>
        {status === 'idle' && !opened && (
          <button
            type="button"
            className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-2"
            onClick={start}
          >
            Open terminal
          </button>
        )}
        {status === 'connecting' && <span className="text-text-3">Connecting…</span>}
        {(status === 'closed' || status === 'error') && (
          <button
            type="button"
            className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-2"
            onClick={start}
          >
            Reconnect
          </button>
        )}
      </div>
      {opened ? (
        <Suspense fallback={<div className="h-96 rounded-md border border-border bg-black/40" />}>
          <HostTerminalView key={attempt} hostId={hostId} onStatusChange={handleStatusChange} />
        </Suspense>
      ) : null}
    </div>
  );
}
