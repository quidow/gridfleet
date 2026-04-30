/**
 * Returns true when the state indicates the virtual device is active
 * (running or booted). Used to disable Launch/Boot buttons.
 */
export function isEmulatorRunning(state: string | null | undefined): boolean {
  return state === 'running' || state === 'booted';
}

/**
 * Returns true when the state indicates the virtual device is inactive
 * (stopped or shutdown). Used to disable Shutdown buttons.
 */
export function isEmulatorStopped(state: string | null | undefined): boolean {
  return state === 'stopped' || state === 'shutdown';
}
