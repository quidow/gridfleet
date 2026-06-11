import { useQuery } from '@tanstack/react-query';
import { fetchEventCatalog } from '../api/events';
import { qk } from '../lib/queryKeys';

export function useEventCatalog() {
  return useQuery({
    queryKey: qk.eventCatalog.root,
    queryFn: fetchEventCatalog,
    // Catalog is static at runtime — regenerated only on backend deploy.
    refetchInterval: false,
    staleTime: Infinity,
  });
}
