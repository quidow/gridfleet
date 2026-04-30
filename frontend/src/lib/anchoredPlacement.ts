export type Placement = 'bottom-end' | 'bottom-start' | 'top-end' | 'top-start';

export type PlacementRect = {
  top: number;
  left: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
};

export type PlacementResult = {
  top: number;
  left: number;
  maxHeight: number;
  maxWidth: number;
  transformOrigin: string;
  placement: Placement;
};

type ComputePlacementArgs = {
  trigger: PlacementRect;
  menu: { width: number; height: number };
  container: PlacementRect;
  preferences: Placement[];
  gap?: number;
  edgePadding?: number;
};

const TRANSFORM_ORIGINS: Record<Placement, string> = {
  'bottom-end': 'top right',
  'bottom-start': 'top left',
  'top-end': 'bottom right',
  'top-start': 'bottom left',
};

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  if (value < min) return min;
  if (value > max) return max;
  return value;
}

function computeCandidate(
  placement: Placement,
  trigger: PlacementRect,
  menu: { width: number; height: number },
  container: PlacementRect,
  gap: number,
  edgePadding: number,
): { top: number; left: number; availableHeight: number; availableWidth: number; fits: boolean } {
  const minLeft = container.left + edgePadding;
  const maxLeft = container.right - edgePadding - menu.width;
  const minTop = container.top + edgePadding;
  const maxTop = container.bottom - edgePadding - menu.height;

  const isBottom = placement.startsWith('bottom');
  const isEnd = placement.endsWith('end');

  const rawTop = isBottom ? trigger.bottom + gap : trigger.top - gap - menu.height;
  const rawLeft = isEnd ? trigger.right - menu.width : trigger.left;

  const availableHeight = isBottom
    ? container.bottom - edgePadding - (trigger.bottom + gap)
    : trigger.top - gap - (container.top + edgePadding);

  const availableWidth = isEnd
    ? trigger.right - (container.left + edgePadding)
    : container.right - edgePadding - trigger.left;

  const top = clamp(rawTop, minTop, maxTop);
  const left = clamp(rawLeft, minLeft, maxLeft);

  const fits = availableHeight >= menu.height && availableWidth >= menu.width;

  return { top, left, availableHeight, availableWidth, fits };
}

export function computeAnchoredPlacement(args: ComputePlacementArgs): PlacementResult {
  const { trigger, menu, container, preferences } = args;
  const gap = args.gap ?? 4;
  const edgePadding = args.edgePadding ?? 8;

  for (const placement of preferences) {
    const candidate = computeCandidate(placement, trigger, menu, container, gap, edgePadding);
    if (candidate.fits) {
      return {
        top: candidate.top,
        left: candidate.left,
        maxHeight: menu.height,
        maxWidth: menu.width,
        transformOrigin: TRANSFORM_ORIGINS[placement],
        placement,
      };
    }
  }

  const fallback = preferences[preferences.length - 1] ?? 'bottom-end';
  const containerHeight = Math.max(0, container.bottom - container.top - edgePadding * 2);
  const containerWidth = Math.max(0, container.right - container.left - edgePadding * 2);
  const maxHeight = Math.max(0, Math.min(menu.height, containerHeight, Math.floor(0.7 * containerHeight)));
  const maxWidth = Math.max(0, Math.min(menu.width, containerWidth));
  const shrunk = computeCandidate(
    fallback,
    trigger,
    { width: maxWidth, height: maxHeight },
    container,
    gap,
    edgePadding,
  );
  return {
    top: shrunk.top,
    left: shrunk.left,
    maxHeight,
    maxWidth,
    transformOrigin: TRANSFORM_ORIGINS[fallback],
    placement: fallback,
  };
}
