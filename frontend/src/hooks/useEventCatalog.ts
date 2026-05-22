import { useQuery } from '@tanstack/react-query';
import { fetchEventCatalog } from '../api/events';

export function useEventCatalog() {
  return useQuery({
    queryKey: ['event-catalog'],
    queryFn: fetchEventCatalog,
    // Catalog is static at runtime — regenerated only on backend deploy.
    refetchInterval: false,
    staleTime: Infinity,
  });
}
