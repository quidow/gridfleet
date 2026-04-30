import { useCallback, useLayoutEffect, useState, type RefObject } from 'react';
import {
  computeAnchoredPlacement,
  type Placement,
  type PlacementRect,
  type PlacementResult,
} from './anchoredPlacement';

type UseAnchoredPlacementArgs = {
  triggerRef: RefObject<HTMLElement | null>;
  menuRef: RefObject<HTMLElement | null>;
  open: boolean;
  preferences?: Placement[];
  container?: HTMLElement | null;
  gap?: number;
  edgePadding?: number;
};

const DEFAULT_PREFERENCES: Placement[] = ['bottom-end', 'bottom-start', 'top-end', 'top-start'];

function viewportRect(): PlacementRect {
  const width = window.innerWidth;
  const height = window.innerHeight;
  return { top: 0, left: 0, right: width, bottom: height, width, height };
}

function toRect(element: Element): PlacementRect {
  const r = element.getBoundingClientRect();
  return { top: r.top, left: r.left, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
}

function intersect(a: PlacementRect, b: PlacementRect): PlacementRect {
  const top = Math.max(a.top, b.top);
  const left = Math.max(a.left, b.left);
  const right = Math.min(a.right, b.right);
  const bottom = Math.min(a.bottom, b.bottom);
  const width = Math.max(0, right - left);
  const height = Math.max(0, bottom - top);
  return { top, left, right, bottom, width, height };
}

export function useAnchoredPlacement({
  triggerRef,
  menuRef,
  open,
  preferences = DEFAULT_PREFERENCES,
  container,
  gap,
  edgePadding,
}: UseAnchoredPlacementArgs): PlacementResult | null {
  const [result, setResult] = useState<PlacementResult | null>(null);

  const measure = useCallback(() => {
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!trigger || !menu) return;
    const menuRect = menu.getBoundingClientRect();
    if (menuRect.width === 0 || menuRect.height === 0) return;
    const viewport = viewportRect();
    const containerRect = container ? intersect(viewport, toRect(container)) : viewport;
    const next = computeAnchoredPlacement({
      trigger: toRect(trigger),
      menu: { width: menuRect.width, height: menuRect.height },
      container: containerRect,
      preferences,
      gap,
      edgePadding,
    });
    setResult((prev) => {
      if (
        prev &&
        prev.top === next.top &&
        prev.left === next.left &&
        prev.maxHeight === next.maxHeight &&
        prev.maxWidth === next.maxWidth &&
        prev.placement === next.placement
      ) {
        return prev;
      }
      return next;
    });
  }, [triggerRef, menuRef, container, preferences, gap, edgePadding]);

  useLayoutEffect(() => {
    if (!open) return undefined;

    const frame = requestAnimationFrame(() => measure());

    const handleResize = () => measure();
    const handleScroll = () => measure();

    window.addEventListener('resize', handleResize);
    window.addEventListener('scroll', handleScroll, true);
    if (container) {
      container.addEventListener('scroll', handleScroll, { passive: true });
    }

    let observer: ResizeObserver | null = null;
    const menu = menuRef.current;
    if (menu && typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => measure());
      observer.observe(menu);
    }

    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener('resize', handleResize);
      window.removeEventListener('scroll', handleScroll, true);
      if (container) {
        container.removeEventListener('scroll', handleScroll);
      }
      observer?.disconnect();
    };
  }, [open, measure, container, menuRef]);

  return open ? result : null;
}
