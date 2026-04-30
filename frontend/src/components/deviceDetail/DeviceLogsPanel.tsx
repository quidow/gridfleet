import { useEffect, useRef } from 'react';
import DeviceLogsEmptyPanel from './DeviceLogsEmptyPanel';

type Props = {
  logsData?: { lines: string[]; count: number };
};

export default function DeviceLogsPanel({ logsData }: Props) {
  const logEndRef = useRef<HTMLDivElement>(null);
  const lines = Array.isArray(logsData?.lines) ? logsData.lines : [];
  const count = logsData?.count ?? lines.length;

  useEffect(() => {
    if (logEndRef.current && lines.length > 0) {
      logEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logsData, lines.length]);

  if (lines.length === 0) {
    return <DeviceLogsEmptyPanel />;
  }

  return (
    <section className="overflow-hidden rounded-lg border border-border bg-surface-1 shadow-sm">
      <div className="flex items-center justify-between px-5 py-4">
        <div>
          <h2 className="text-sm font-semibold text-text-1">Appium Logs</h2>
          <p className="mt-1 text-xs text-text-2">{count} retained lines from the active node.</p>
        </div>
      </div>
      <div className="max-h-[32rem] overflow-auto border-t border-border bg-sidebar-surface p-4 font-mono text-xs text-sidebar-heading">
        {lines.map((line, index) => (
          <div key={index} className="whitespace-pre-wrap leading-5">
            {line}
          </div>
        ))}
        <div ref={logEndRef} />
      </div>
    </section>
  );
}
