const TOKEN_OVERRIDES: Record<string, string> = {
  adb: 'ADB',
  api: 'API',
  firetv: 'Fire TV',
  id: 'ID',
  ios: 'iOS',
  ip: 'IP',
  macos: 'macOS',
  os: 'OS',
  tvos: 'tvOS',
  ttl: 'TTL',
  usb: 'USB',
  wda: 'WDA',
};

function titleCaseToken(token: string): string {
  const override = TOKEN_OVERRIDES[token];
  if (override) return override;
  return token.charAt(0).toUpperCase() + token.slice(1);
}

export function formatStatus(value: string | null | undefined): string {
  const normalized = value?.trim().toLowerCase();
  if (!normalized) return '';

  return normalized
    .split('_')
    .filter((part) => part.length > 0)
    .map(titleCaseToken)
    .join(' ');
}
