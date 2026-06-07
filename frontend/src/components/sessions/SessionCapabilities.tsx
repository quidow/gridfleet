import { Copy } from 'lucide-react';
import { toast } from 'sonner';
import type { SessionDetail } from '../../types';

async function copyJson(label: string, json: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(json);
    toast.success(`${label} copied`);
  } catch {
    toast.error(`Could not copy ${label.toLowerCase()}`);
  }
}

function CapabilitiesBlock({
  title,
  capabilities,
}: {
  title: string;
  capabilities: Record<string, unknown> | null | undefined;
}) {
  const json = capabilities ? JSON.stringify(capabilities, null, 2) : null;
  return (
    <div className="min-w-0 flex-1">
      <div className="flex items-center gap-2">
        <h4 className="text-xs font-medium uppercase tracking-wide text-text-3">{title}</h4>
        {json && (
          <button
            type="button"
            onClick={() => void copyJson(title, json)}
            className="rounded p-1 text-text-3 hover:bg-surface-1 hover:text-accent-hover"
            aria-label={`Copy ${title.toLowerCase()}`}
            title={`Copy ${title.toLowerCase()}`}
          >
            <Copy size={14} />
          </button>
        )}
      </div>
      {json ? (
        <pre className="mt-2 max-h-80 overflow-auto rounded-md bg-surface-1 p-3 font-mono text-xs text-text-2">{json}</pre>
      ) : (
        <p className="mt-2 text-sm text-text-3">Not captured</p>
      )}
    </div>
  );
}

export function SessionCapabilities({ session }: { session: SessionDetail }) {
  return (
    <div className="flex flex-col gap-4 md:flex-row">
      <CapabilitiesBlock title="Requested capabilities" capabilities={session.requested_capabilities} />
      <CapabilitiesBlock title="Actual capabilities" capabilities={session.actual_capabilities} />
    </div>
  );
}
