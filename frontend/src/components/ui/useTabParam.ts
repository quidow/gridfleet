import { useSearchParams } from 'react-router-dom';

/**
 * URL-synced tab state hook.
 *
 * Reads `?<paramName>=<id>` from the URL, falling back to `defaultId` when
 * the param is absent or holds an unknown value. Uses the spread-and-update
 * pattern so unrelated query params survive tab changes.
 *
 * @param paramName - URL query-param name (e.g. "tab")
 * @param allowedIds - Set of valid tab ids; unknown values fall back to defaultId
 * @param defaultId  - Tab id to use when no valid value is in the URL
 */
export function useTabParam(
  paramName: string,
  allowedIds: string[],
  defaultId: string,
): [string, (id: string) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const raw = searchParams.get(paramName);
  const activeId = raw !== null && allowedIds.includes(raw) ? raw : defaultId;

  function setActive(id: string) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set(paramName, id);
      return next;
    });
  }

  return [activeId, setActive];
}
