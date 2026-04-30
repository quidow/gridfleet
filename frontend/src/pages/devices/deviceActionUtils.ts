export function parseDeviceActionTags(raw: string): Record<string, string> {
  const parsed = JSON.parse(raw);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Tags must be a JSON object');
  }
  return Object.fromEntries(
    Object.entries(parsed).map(([key, value]) => [key, typeof value === 'string' ? value : JSON.stringify(value)]),
  );
}
