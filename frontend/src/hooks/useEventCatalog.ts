import { useQuery } from '@tanstack/react-query';
import { fetchEventCatalog } from '../api/events';

export function useEventCatalog() {
  return useQuery({
    queryKey: ['event-catalog'],
    queryFn: fetchEventCatalog,
    staleTime: 60_000,
  });
}
