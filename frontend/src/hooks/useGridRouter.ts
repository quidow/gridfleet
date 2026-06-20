import { useQuery } from '@tanstack/react-query';

import { fetchGridRouter } from '../api/grid';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { qk } from '../lib/queryKeys';
import { POLL_FAST_MS, sseAdaptivePolling } from './polling';

export function useGridRouter() {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.gridRouter.root,
    queryFn: fetchGridRouter,
    ...sseAdaptivePolling(connected, POLL_FAST_MS),
  });
}
