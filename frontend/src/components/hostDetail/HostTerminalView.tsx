import { useEffect, useRef, useState } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

import { buildTerminalWebSocketUrl } from '../../api/hostTerminal';

type Status = 'connecting' | 'connected' | 'error' | 'closed';

interface HostTerminalViewProps {
  hostId: string;
  onStatusChange?: (status: Status) => void;
}

export function HostTerminalView({ hostId, onStatusChange }: HostTerminalViewProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [status, setStatus] = useState<Status>('connecting');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    onStatusChange?.(status);
  }, [status, onStatusChange]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const term = new Terminal({
      cursorBlink: true,
      fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)',
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(container);
    fit.fit();

    const ws = new WebSocket(buildTerminalWebSocketUrl(hostId));

    ws.onopen = () => {
      setStatus('connected');
      ws.send(JSON.stringify({ type: 'open', cols: term.cols, rows: term.rows }));
    };
    ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data as string) as {
          type: string;
          data?: string;
          exit_code?: number;
          message?: string;
        };
        if (frame.type === 'output') {
          term.write(frame.data ?? '');
        } else if (frame.type === 'exit') {
          term.write(`\r\n[process exited with code ${frame.exit_code ?? '?'}]\r\n`);
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
      setStatus('error');
      setErrorMessage(
        'Connection failed. The terminal feature may be misconfigured or the host may be unreachable.',
      );
    };
    ws.onclose = () => {
      setStatus((prev) => (prev === 'error' ? 'error' : 'closed'));
    };

    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'input', data }));
      }
    });
    term.onResize(({ cols, rows }) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols, rows }));
      }
    });

    const handleResize = () => fit.fit();
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      ws.close();
      term.dispose();
      fit.dispose();
    };
  }, [hostId]);

  return (
    <div className="space-y-3">
      {errorMessage ? (
        <div className="rounded-md bg-danger-soft px-3 py-2 text-sm text-danger-foreground">{errorMessage}</div>
      ) : null}
      <div ref={containerRef} className="h-96 rounded-md border border-border bg-black" />
    </div>
  );
}

export default HostTerminalView;
