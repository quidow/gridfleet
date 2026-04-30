import { useEffect } from 'react';

export function usePageTitle(title: string) {
  useEffect(() => {
    document.title = title ? `GridFleet - ${title}` : 'GridFleet';
  }, [title]);
}
