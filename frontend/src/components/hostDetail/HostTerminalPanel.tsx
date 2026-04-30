import { useEffect, useRef, useState } from 'react';

import { buildTerminalWebSocketUrl } from '../../api/hostTerminal';

interface HostTerminalPanelProps {
  hostId: string;
  hostOnline: boolean;
  terminalEnabled: boolean;
}

type Status = 'idle' | 'connecting' | 'connected' | 'error' | 'closed';

export default function HostTerminalPanel({ hostId, hostOnline, terminalEnabled }: HostTerminalPanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const termRef = useRef<import('@xterm/xterm').Terminal | null>(null);
  const fitRef = useRef<import('@xterm/addon-fit').FitAddon | null>(null);
  // Store the resize handler so the cleanup effect and startSession can remove it.
  const resizeHandlerRef = useRef<(() => void) | null>(null);
  const [status, setStatus] = useState<Status>('idle');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      if (resizeHandlerRef.current) {
        window.removeEventListener('resize', resizeHandlerRef.current);
      }
      wsRef.current?.close();
      termRef.current?.dispose();
      fitRef.current?.dispose();
    };
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

  const startSession = async () => {
    if (status === 'connecting' || status === 'connected') return;

    // Clean up any previous session — prevents stale listener accumulation on reconnect.
    if (resizeHandlerRef.current) {
      window.removeEventListener('resize', resizeHandlerRef.current);
      resizeHandlerRef.current = null;
    }
    // Null out wsRef before closing so stale onclose can detect the identity mismatch.
    const prevWs = wsRef.current;
    wsRef.current = null;
    prevWs?.close();
    termRef.current?.dispose();
    fitRef.current?.dispose();
    termRef.current = null;
    fitRef.current = null;

    setStatus('connecting');
    setErrorMessage(null);

    const { Terminal } = await import('@xterm/xterm');
    const { FitAddon } = await import('@xterm/addon-fit');
    await import('@xterm/xterm/css/xterm.css');

    const term = new Terminal({
      cursorBlink: true,
      fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)',
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    if (containerRef.current) {
      term.open(containerRef.current);
      fit.fit();
    }
    termRef.current = term;
    fitRef.current = fit;

    const ws = new WebSocket(buildTerminalWebSocketUrl(hostId));
    wsRef.current = ws;

    ws.onopen = () => {
      // Guard against stale callback firing after reconnect replaces wsRef.
      if (wsRef.current !== ws) return;
      setStatus('connected');
      ws.send(JSON.stringify({ type: 'open', cols: term.cols, rows: term.rows }));
    };
    ws.onmessage = (ev) => {
      // Guard against stale callback firing after reconnect replaces wsRef.
      if (wsRef.current !== ws) return;
      try {
        const frame = JSON.parse(ev.data as string) as {
          type: string;
          data?: string;
          exit_code?: number;
          message?: string;
        };
        if (frame.type === 'output') {
          // Guard against writing to a disposed terminal.
          if (termRef.current !== term) return;
          term.write(frame.data ?? '');
        } else if (frame.type === 'exit') {
          if (termRef.current === term) {
            term.write(`\r\n[process exited with code ${frame.exit_code ?? '?'}]\r\n`);
          }
          setStatus('closed');
        } else if (frame.type === 'error') {
          setErrorMessage(frame.message ?? 'Unknown terminal error.');
          setStatus('error');
        }
      } catch {
        // ignore non-JSON frames
      }
    };
    ws.onerror = () => {
      // Guard against stale callback firing after reconnect replaces wsRef.
      if (wsRef.current !== ws) return;
      setStatus('error');
      setErrorMessage(
        'Connection failed. The terminal feature may be misconfigured or the host may be unreachable.',
      );
    };
    ws.onclose = () => {
      // Guard against stale onclose stomping a reset status after reconnect.
      if (wsRef.current !== ws) return;
      setStatus((prev) => (prev === 'error' ? 'error' : 'closed'));
    };

    term.onData((data) => {
      if (wsRef.current === ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'input', data }));
      }
    });
    term.onResize(({ cols, rows }) => {
      if (wsRef.current === ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols, rows }));
      }
    });

    // Store and register the resize handler so the cleanup effect and next startSession can remove it.
    const handleResize = () => fit.fit();
    resizeHandlerRef.current = handleResize;
    window.addEventListener('resize', handleResize);
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm text-text-2">
        <span>Status: {status}</span>
        {status === 'idle' && (
          <button
            type="button"
            className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-2"
            onClick={() => void startSession()}
          >
            Open terminal
          </button>
        )}
        {status === 'connecting' && (
          <span className="text-text-3">Connecting…</span>
        )}
        {(status === 'closed' || status === 'error') && (
          <button
            type="button"
            className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-2"
            onClick={() => void startSession()}
          >
            Reconnect
          </button>
        )}
      </div>
      {errorMessage ? (
        <div className="rounded-md bg-danger-soft px-3 py-2 text-sm text-danger-foreground">{errorMessage}</div>
      ) : null}
      <div ref={containerRef} className="h-96 rounded-md border border-border bg-black" />
    </div>
  );
}
