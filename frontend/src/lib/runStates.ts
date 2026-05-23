import type { RunState } from '../types'

export const ACTIVE_RUN_STATES: ReadonlySet<RunState> = new Set<RunState>([
  'pending',
  'preparing',
  'active',
  'completing',
])
