import type { DeviceRead } from '../types';

export function isLifecycleSummaryActive(summary: DeviceRead['lifecycle_policy_summary'] | null | undefined): boolean {
  return !!summary && summary.state !== 'idle';
}
