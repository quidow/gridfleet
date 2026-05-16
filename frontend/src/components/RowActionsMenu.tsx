import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { MoreVertical } from 'lucide-react';
import type { CSSProperties, ReactNode } from 'react';
import { useAnchoredPlacement } from '../lib/useAnchoredPlacement';
import type { Placement } from '../lib/anchoredPlacement';

export type RowActionItem = {
  key: string;
  label: string;
  icon: ReactNode;
  onSelect: () => void;
  disabled?: boolean;
  title?: string;
  tone?: 'default' | 'danger';
};

type Props = {
  label: string;
  items: RowActionItem[];
};

const PREFERENCES: Placement[] = ['bottom-end', 'bottom-start', 'top-end', 'top-start'];

function findScrollContainer(element: HTMLElement | null): HTMLElement | null {
  let node: HTMLElement | null = element?.parentElement ?? null;
  while (node) {
    if (node.tagName === 'MAIN') return node;
    node = node.parentElement;
  }
  return document.querySelector('main');
}

export function RowActionsMenu({ label, items }: Props) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [container, setContainer] = useState<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setContainer(findScrollContainer(buttonRef.current));
  }, [open]);

  const placement = useAnchoredPlacement({
    triggerRef: buttonRef,
    menuRef,
    open,
    preferences: PREFERENCES,
    container,
  });

  useEffect(() => {
    if (!open) return undefined;

    function handlePointerDown(event: MouseEvent) {
      if (
        !buttonRef.current?.contains(event.target as Node) &&
        !menuRef.current?.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    }

    function handleEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setOpen(false);
      }
    }

    function handleScroll(event: Event) {
      if (menuRef.current?.contains(event.target as Node)) return;
      setOpen(false);
    }

    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleEscape);
    const scrollTimer = window.setTimeout(() => {
      window.addEventListener('scroll', handleScroll, true);
    }, 100);
    return () => {
      window.clearTimeout(scrollTimer);
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleEscape);
      window.removeEventListener('scroll', handleScroll, true);
    };
  }, [open]);

  function toggleMenu() {
    setOpen((prev) => !prev);
  }

  const menuStyle: CSSProperties = placement
    ? {
        position: 'fixed',
        top: placement.top,
        left: placement.left,
        maxHeight: placement.maxHeight,
        maxWidth: placement.maxWidth,
        transformOrigin: placement.transformOrigin,
        overflowY: 'auto',
        zIndex: 50,
      }
    : {
        position: 'fixed',
        top: 0,
        left: 0,
        maxWidth: 224,
        visibility: 'hidden',
        zIndex: 50,
      };

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={toggleMenu}
        className="rounded p-1.5 text-text-3 hover:bg-surface-2 hover:text-text-2"
      >
        <MoreVertical size={16} />
      </button>

      {open
        ? createPortal(
            <div
              ref={menuRef}
              role="menu"
              aria-label={label}
              className="min-w-52 rounded-lg border border-border bg-surface-1 py-1 shadow-lg"
              style={menuStyle}
            >
              {items.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    if (item.disabled) return;
                    setOpen(false);
                    item.onSelect();
                  }}
                  disabled={item.disabled}
                  title={item.title}
                  className={`flex min-h-[40px] w-full items-center gap-2 px-3 py-2.5 text-left text-sm ${
                    item.disabled
                      ? 'cursor-not-allowed text-text-3'
                      : item.tone === 'danger'
                        ? 'text-danger-foreground hover:bg-danger-soft'
                        : 'text-text-1 hover:bg-surface-2'
                  }`}
                >
                  <span className="shrink-0">{item.icon}</span>
                  <span>{item.label}</span>
                </button>
              ))}
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
