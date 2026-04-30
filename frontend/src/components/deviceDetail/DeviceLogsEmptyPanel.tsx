import { Terminal } from 'lucide-react';

export default function DeviceLogsEmptyPanel() {
  return (
    <section className="rounded-lg border border-dashed border-border-strong bg-surface-2 px-5 py-8">
      <div className="mx-auto flex max-w-xl flex-col items-center text-center sm:flex-row sm:text-left">
        <div className="mb-4 flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-1 text-text-2 sm:mb-0 sm:mr-4">
          <Terminal size={20} />
        </div>
        <div className="min-w-0 flex-1">
          <h3 className="heading-subsection">No Appium logs yet</h3>
          <p className="mt-1 text-sm text-text-2">Logs appear here once the Appium node emits output.</p>
        </div>
      </div>
    </section>
  );
}
