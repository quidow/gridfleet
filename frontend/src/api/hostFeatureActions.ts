import api from './client';
import type { FeatureActionResult } from '../types/driverPacks';

/**
 * Invoke a driver-pack feature action on a specific host.
 *
 * POST /api/hosts/{host_id}/driver-packs/{pack_id}/features/{feature_id}/actions/{action_id}
 */
export async function invokeFeatureAction(
  hostId: string,
  packId: string,
  featureId: string,
  actionId: string,
  args: Record<string, unknown> = {},
): Promise<FeatureActionResult> {
  const { data } = await api.post<FeatureActionResult>(
    `/hosts/${hostId}/driver-packs/${packId}/features/${featureId}/actions/${actionId}`,
    { args },
  );
  return data;
}
