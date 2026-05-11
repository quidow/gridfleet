import type { RunRead, RunState } from '../../types';

const RUNNING_STATES: ReadonlySet<RunState> = new Set(['active', 'completing']);
const QUEUED_STATES: ReadonlySet<RunState> = new Set(['pending', 'preparing']);

export type RunsSummary = {
  running: number;
  queued: number;
  passed24h: number;
  failed24h: number;
};

export function deriveRunsSummary(runs: ReadonlyArray<RunRead>, now: Date = new Date()): RunsSummary {
  const cutoff = now.getTime() - 24 * 60 * 60 * 1000;
  let running = 0;
  let queued = 0;
  let passed24h = 0;
  let failed24h = 0;

  for (const run of runs) {
    if (RUNNING_STATES.has(run.state)) running += 1;
    if (QUEUED_STATES.has(run.state)) queued += 1;

    if (run.completed_at) {
      const completed = Date.parse(run.completed_at);
      if (Number.isFinite(completed) && completed >= cutoff) {
        passed24h += run.session_counts.passed;
        failed24h += run.session_counts.failed + run.session_counts.error;
      }
    }
  }

  return { running, queued, passed24h, failed24h };
}
