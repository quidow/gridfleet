import { useQuery } from '@tanstack/react-query';
import { fetchEventCatalog } from '../api/events';

const EVENT_CATALOG_POLL_MS = 60_000;

export function useEventCatalog() {
  return useQuery({
    queryKey: ['event-catalog'],
    queryFn: fetchEventCatalog,
    refetchInterval: EVENT_CATALOG_POLL_MS,
    staleTime: EVENT_CATALOG_POLL_MS / 2,
  });
}
