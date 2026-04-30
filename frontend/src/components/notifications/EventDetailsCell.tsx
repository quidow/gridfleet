import { useId, useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { formatEventDetails } from './eventRegistry';

interface EventDetailsCellProps {
  type: string;
  data: Record<string, unknown>;
}

const COLLAPSED_PREVIEW_MAX = 200;

function truncate(value: string, max: number): string {
  if (value.length <= max) return value;
  return `${value.slice(0, max - 1)}...`;
}

export default function EventDetailsCell({ type, data }: EventDetailsCellProps) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const formatted = formatEventDetails(type, data);

  if (formatted.kind === 'text') {
    return <span className="text-sm text-text-2">{formatted.text}</span>;
  }

  if (formatted.kind === 'empty') {
    return <span className="text-sm text-text-3 italic">{formatted.text}</span>;
  }

  const oneLine = JSON.stringify(data ?? {});
  const preview = truncate(oneLine, COLLAPSED_PREVIEW_MAX);

  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="flex min-w-0 items-center gap-2">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={bodyId}
          aria-label={open ? 'Hide raw details' : 'Show raw details'}
          onClick={() => setOpen((prev) => !prev)}
          className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-text-3 transition-colors hover:bg-surface-2 hover:text-text-2"
        >
          <ChevronRight
            size={14}
            aria-hidden
            className={['transition-transform duration-150', open ? 'rotate-90' : ''].join(' ')}
          />
        </button>
        <code className="max-w-[480px] truncate font-mono text-xs text-text-2" title={oneLine}>
          {preview}
        </code>
      </div>
      {open && (
        <pre
          id={bodyId}
          className="mt-1 ml-7 max-w-full overflow-x-auto rounded-md border border-border bg-surface-2 p-2 font-mono text-xs text-text-2"
        >
          {formatted.text}
        </pre>
      )}
    </div>
  );
}
