import { useState, useId, type ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';
import { usePersistedBoolean } from '../../hooks/usePersistedBoolean';

interface CollapsibleSectionProps {
  title: string;
  description?: string;
  /** Content shown next to the title even when collapsed — useful for status pills. */
  summary?: ReactNode;
  actions?: ReactNode;
  defaultOpen?: boolean;
  /** When set, open/closed state is persisted to localStorage under this key. */
  persistKey?: string;
  children: ReactNode;
  className?: string;
}

function useDisclosureState(persistKey: string | undefined, defaultOpen: boolean): [boolean, (next: boolean) => void] {
  const persisted = usePersistedBoolean(persistKey ?? '', defaultOpen);
  const local = useState<boolean>(defaultOpen);
  return persistKey ? persisted : local;
}

/**
 * A section card with a click/tap disclosure toggle.
 * Body is hidden (but kept in DOM) when collapsed for stable selector targeting.
 * Open/closed state can optionally be persisted to localStorage via `persistKey`.
 */
export default function CollapsibleSection({
  title,
  description,
  summary,
  actions,
  defaultOpen = false,
  persistKey,
  children,
  className = '',
}: CollapsibleSectionProps) {
  const bodyId = useId();
  const [open, setOpen] = useDisclosureState(persistKey, defaultOpen);

  return (
    <section className={['card card-padding', className].filter(Boolean).join(' ')}>
      <div className="flex items-center justify-between gap-4">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpen(!open)}
          className="flex flex-1 items-center gap-2 text-left min-w-0"
        >
          <ChevronDown
            size={16}
            aria-hidden
            className={[
              'shrink-0 text-text-3 transition-transform duration-150',
              open ? '' : '-rotate-90',
            ].join(' ')}
          />
          <div className="min-w-0 flex-1">
            <span className="text-sm font-medium text-text-2">{title}</span>
            {description && <p className="mt-0.5 text-xs text-text-3">{description}</p>}
          </div>
          {summary && <div className="flex shrink-0 items-center gap-2 ml-3">{summary}</div>}
        </button>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>

      <div id={bodyId} hidden={!open}>
        <div className="mt-4">{children}</div>
      </div>
    </section>
  );
}
