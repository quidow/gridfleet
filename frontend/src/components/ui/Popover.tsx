import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useAnchoredPlacement } from '../../lib/useAnchoredPlacement';
import type { Placement } from '../../lib/anchoredPlacement';

type Props = {
  trigger: ReactNode;
  children: ReactNode;
  ariaLabel: string;
  placement?: Placement[];
  triggerClassName?: string;
  contentClassName?: string;
  disabled?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
};

const DEFAULT_PLACEMENT: Placement[] = ['bottom-start', 'bottom-end', 'top-start', 'top-end'];

function findScrollContainer(element: HTMLElement | null): HTMLElement | null {
  let node: HTMLElement | null = element?.parentElement ?? null;
  while (node) {
    if (node.tagName === 'MAIN') return node;
    node = node.parentElement;
  }
  return document.querySelector('main');
}

export default function Popover({
  trigger,
  children,
  ariaLabel,
  placement = DEFAULT_PLACEMENT,
  triggerClassName,
  contentClassName,
  disabled = false,
  open: controlledOpen,
  onOpenChange,
}: Props) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const open = controlledOpen ?? uncontrolledOpen;
  const buttonRef = useRef<HTMLButtonElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [container, setContainer] = useState<HTMLElement | null>(null);

  const setOpen = useCallback(
    (nextOpen: boolean) => {
      if (controlledOpen === undefined) {
        setUncontrolledOpen(nextOpen);
      }
      onOpenChange?.(nextOpen);
    },
    [controlledOpen, onOpenChange],
  );

  useEffect(() => {
    if (!open) return;
    setContainer(findScrollContainer(buttonRef.current));
  }, [open]);

  const placementResult = useAnchoredPlacement({
    triggerRef: buttonRef,
    menuRef: contentRef,
    open,
    preferences: placement,
    container,
  });

  useEffect(() => {
    if (!open) return undefined;

    function handlePointerDown(event: MouseEvent) {
      if (
        !buttonRef.current?.contains(event.target as Node) &&
        !contentRef.current?.contains(event.target as Node)
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
      if (contentRef.current?.contains(event.target as Node)) return;
      setOpen(false);
    }

    function handleFocusOut(event: FocusEvent) {
      const related = event.relatedTarget as Node | null;
      if (
        related === null ||
        buttonRef.current?.contains(related) ||
        contentRef.current?.contains(related)
      ) {
        return;
      }
      setOpen(false);
    }

    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleEscape);
    document.addEventListener('focusout', handleFocusOut);
    const scrollTimer = window.setTimeout(() => {
      window.addEventListener('scroll', handleScroll, true);
    }, 100);
    return () => {
      window.clearTimeout(scrollTimer);
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleEscape);
      document.removeEventListener('focusout', handleFocusOut);
      window.removeEventListener('scroll', handleScroll, true);
    };
  }, [open, setOpen]);

  function toggle() {
    if (disabled) return;
    setOpen(!open);
  }

  const contentStyle: CSSProperties = placementResult
    ? {
        position: 'fixed',
        top: placementResult.top,
        left: placementResult.left,
        maxHeight: placementResult.maxHeight,
        maxWidth: placementResult.maxWidth,
        transformOrigin: placementResult.transformOrigin,
        overflowY: 'auto',
        zIndex: 50,
      }
    : {
        position: 'fixed',
        top: 0,
        left: 0,
        maxWidth: 320,
        opacity: 0,
        pointerEvents: 'none',
        zIndex: 50,
      };

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        aria-label={ariaLabel}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={toggle}
        disabled={disabled}
        className={triggerClassName ?? 'inline-flex items-center gap-1 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent-ring disabled:cursor-not-allowed'}
      >
        {trigger}
      </button>

      {open
        ? createPortal(
            <div
              ref={contentRef}
              role="dialog"
              aria-label={ariaLabel}
              style={contentStyle}
              className={
                contentClassName
                ?? 'min-w-48 max-w-sm rounded-lg border border-border bg-surface-1 p-3 text-sm shadow-lg'
              }
            >
              {children}
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
