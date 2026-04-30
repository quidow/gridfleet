export function buildLocationTarget(locationLike: {
  pathname: string;
  search: string;
  hash: string;
}): string {
  const target = `${locationLike.pathname}${locationLike.search}${locationLike.hash}`;
  return target || '/';
}

export function normalizeNextTarget(next: string | null | undefined): string {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/login')) {
    return '/';
  }
  return next;
}
