import type { SessionRead } from '../../types/devices';

const DAY_MS = 24 * 60 * 60 * 1000;
const WEEK_MS = 7 * DAY_MS;

// Inclusive: sessions that started exactly `ms` milliseconds ago are included.
function within(ms: number, session: SessionRead, now: Date): boolean {
  const started = Date.parse(session.started_at);
  if (Number.isNaN(started)) {
    return false;
  }
  const delta = now.getTime() - started;
  return delta >= 0 && delta <= ms;
}

export function sessions24h(sessions: SessionRead[], now: Date = new Date()): number {
  return sessions.filter((s) => within(DAY_MS, s, now)).length;
}

export function passRate7d(sessions: SessionRead[], now: Date = new Date()): number | null {
  const withinWindow = sessions.filter((s) => within(WEEK_MS, s, now));
  if (withinWindow.length === 0) {
    return null;
  }
  const passed = withinWindow.filter((s) => s.status === 'passed').length;
  return Math.round((passed / withinWindow.length) * 100);
}

export function failures7d(sessions: SessionRead[], now: Date = new Date()): number {
  return sessions.filter(
    (s) => within(WEEK_MS, s, now) && (s.status === 'failed' || s.status === 'error'),
  ).length;
}

export function lastSession(sessions: SessionRead[]): string | null {
  if (sessions.length === 0) {
    return null;
  }
  let best: string | null = null;
  let bestMs = -Infinity;
  for (const s of sessions) {
    const ms = Date.parse(s.started_at);
    if (!Number.isNaN(ms) && ms > bestMs) {
      best = s.started_at;
      bestMs = ms;
    }
  }
  return best;
}

export interface DeviceStatSummary {
  sessions24h: number | null;
  passRate7d: number | null;
  failures7d: number | null;
  lastSession: string | null;
}

export function buildDeviceStatSummary(
  sessions: SessionRead[],
  now: Date = new Date(),
): DeviceStatSummary {
  if (sessions.length === 0) {
    return { sessions24h: null, passRate7d: null, failures7d: null, lastSession: null };
  }
  return {
    sessions24h: sessions24h(sessions, now),
    passRate7d: passRate7d(sessions, now),
    failures7d: failures7d(sessions, now),
    lastSession: lastSession(sessions),
  };
}
