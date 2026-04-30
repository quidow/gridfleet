import { describe, expect, it } from 'vitest';
import { computeAnchoredPlacement, type PlacementRect } from './anchoredPlacement';

const CONTAINER: PlacementRect = {
  top: 0,
  left: 0,
  right: 1280,
  bottom: 720,
  width: 1280,
  height: 720,
};

function triggerAt(left: number, top: number, size = 32): PlacementRect {
  return {
    top,
    left,
    right: left + size,
    bottom: top + size,
    width: size,
    height: size,
  };
}

const MENU = { width: 208, height: 160 };
const PREFS = ['bottom-end', 'bottom-start', 'top-end', 'top-start'] as const;

describe('computeAnchoredPlacement', () => {
  it('picks bottom-end when trigger has room below and to the left', () => {
    const trigger = triggerAt(600, 300);
    const result = computeAnchoredPlacement({
      trigger,
      menu: MENU,
      container: CONTAINER,
      preferences: [...PREFS],
    });
    expect(result.placement).toBe('bottom-end');
    expect(result.top).toBe(trigger.bottom + 4);
    expect(result.left).toBe(trigger.right - MENU.width);
    expect(result.transformOrigin).toBe('top right');
  });

  it('flips to top-end when bottom space is insufficient', () => {
    const trigger = triggerAt(600, 680);
    const result = computeAnchoredPlacement({
      trigger,
      menu: MENU,
      container: CONTAINER,
      preferences: [...PREFS],
    });
    expect(result.placement).toBe('top-end');
    expect(result.top).toBe(trigger.top - 4 - MENU.height);
  });

  it('flips to bottom-start when right side lacks horizontal space', () => {
    const trigger = triggerAt(40, 40);
    const result = computeAnchoredPlacement({
      trigger,
      menu: MENU,
      container: CONTAINER,
      preferences: [...PREFS],
    });
    expect(result.placement).toBe('bottom-start');
    expect(result.left).toBe(trigger.left);
    expect(result.top).toBe(trigger.bottom + 4);
  });

  it('flips to top-start in the bottom-left corner', () => {
    const trigger = triggerAt(40, 680);
    const result = computeAnchoredPlacement({
      trigger,
      menu: MENU,
      container: CONTAINER,
      preferences: [...PREFS],
    });
    expect(result.placement).toBe('top-start');
    expect(result.transformOrigin).toBe('bottom left');
    expect(result.top).toBe(trigger.top - 4 - MENU.height);
  });

  it('shrinks maxHeight when the menu is taller than the container', () => {
    const trigger = triggerAt(600, 300);
    const result = computeAnchoredPlacement({
      trigger,
      menu: { width: 208, height: 2000 },
      container: CONTAINER,
      preferences: [...PREFS],
    });
    expect(result.maxHeight).toBeLessThanOrEqual(Math.floor(0.7 * (720 - 16)));
    expect(result.maxHeight).toBeGreaterThan(0);
    expect(result.top).toBeGreaterThanOrEqual(CONTAINER.top + 8);
    expect(result.top + result.maxHeight).toBeLessThanOrEqual(CONTAINER.bottom - 8);
  });
});
