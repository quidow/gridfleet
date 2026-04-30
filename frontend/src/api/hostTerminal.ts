export interface LocationLike {
  protocol: string;
  host: string;
}

export function buildTerminalWebSocketUrl(hostId: string, loc: LocationLike = window.location): string {
  const scheme = loc.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${loc.host}/api/hosts/${hostId}/terminal`;
}
