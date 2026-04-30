declare global {
  interface Window {
    __GRIDFLEET_RENDER_CRASH_TARGET__?: string | null;
  }
}

export function useDevRenderCrashTrigger(target: string) {
  if (!import.meta.env.DEV || typeof window === 'undefined') {
    return;
  }

  if (window.__GRIDFLEET_RENDER_CRASH_TARGET__ === target) {
    throw new Error(`Forced render crash for ${target}`);
  }
}
