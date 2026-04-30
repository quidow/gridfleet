import { useEffect, useEffectEvent, useId, useRef, type ReactNode, type RefObject } from 'react';
import { X } from 'lucide-react';

type ModalSize = 'sm' | 'md' | 'lg' | 'xl';

interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  size?: ModalSize;
  footer?: ReactNode;
  initialFocusRef?: RefObject<HTMLElement>;
  closeOnBackdropClick?: boolean;
  closeOnEscape?: boolean;
}

const SIZE_CLASSES: Record<ModalSize, string> = {
  sm: 'max-w-sm',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
};

export default function Modal({
  isOpen,
  onClose,
  title,
  children,
  size = 'md',
  footer,
  initialFocusRef,
  closeOnBackdropClick = true,
  closeOnEscape = true,
}: ModalProps) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const hasAppliedInitialFocusRef = useRef(false);
  const requestClose = useEffectEvent(() => {
    onClose();
  });

  // Focus trap + Escape handling
  useEffect(() => {
    if (!isOpen) {
      hasAppliedInitialFocusRef.current = false;
      return undefined;
    }

    // Body scroll lock
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    if (!hasAppliedInitialFocusRef.current) {
      const focusTarget = initialFocusRef?.current ?? closeButtonRef.current;
      focusTarget?.focus();
      hasAppliedInitialFocusRef.current = true;
    }

    function handleKeyDown(e: KeyboardEvent) {
      if (closeOnEscape && e.key === 'Escape') {
        requestClose();
        return;
      }

      if (e.key !== 'Tab' || !dialogRef.current) return;

      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          ':is(a,button,input,textarea,select,[tabindex]):not([tabindex="-1"]):not([disabled])',
        ),
      ).filter((el) => !el.closest('[hidden]') && getComputedStyle(el).display !== 'none');

      if (focusable.length === 0) return;

      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = prevOverflow;
    };
  }, [isOpen, closeOnEscape, initialFocusRef]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" role="presentation">
      <div
        className="fixed inset-0 bg-black/50"
        onClick={closeOnBackdropClick ? onClose : undefined}
        aria-hidden="true"
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={[
          'relative bg-surface-1 rounded-lg shadow-xl w-full mx-4 max-h-modal flex flex-col',
          SIZE_CLASSES[size],
        ].join(' ')}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b shrink-0">
          <h2 id={titleId} className="text-lg font-semibold text-text-1">
            {title}
          </h2>
          <button
            ref={closeButtonRef}
            onClick={onClose}
            aria-label="Close dialog"
            className="text-text-3 hover:text-text-2 focus:outline-none"
          >
            <X size={20} />
          </button>
        </div>

        {/* Body */}
        <div className="overflow-y-auto px-6 py-4 flex-1">{children}</div>

        {/* Footer (optional) */}
        {footer && (
          <div className="flex justify-end gap-2 border-t bg-surface-2 px-6 py-3 shrink-0">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
