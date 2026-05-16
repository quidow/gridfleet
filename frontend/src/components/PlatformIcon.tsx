import { Smartphone, Tv } from 'lucide-react';
import { resolvePlatformLabel } from '../lib/labels';
import { usePlatformDescriptor } from '../hooks/usePlatformDescriptor';
import type { PlatformIconKind } from '../types';

type IconDef = { icon: typeof Smartphone; testId: string };

const PLATFORM_COLOR_CLASSES = [
  'text-platform-0',
  'text-platform-1',
  'text-platform-2',
  'text-platform-3',
  'text-platform-4',
  'text-platform-5',
  'text-platform-6',
  'text-platform-7',
] as const;

function resolveIconDef(iconKind: PlatformIconKind): IconDef {
  switch (iconKind) {
    case 'tv':
      return { icon: Tv, testId: 'platform-icon-tv' };
    case 'set_top':
      return { icon: Tv, testId: 'platform-icon-tv' };
    case 'mobile':
      return { icon: Smartphone, testId: 'platform-icon-mobile' };
    default:
      return { icon: Smartphone, testId: 'platform-icon-generic' };
  }
}

function stableColorClass(key: string | null | undefined): string {
  if (!key) return 'text-platform-generic';
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return PLATFORM_COLOR_CLASSES[hash % PLATFORM_COLOR_CLASSES.length];
}

export function PlatformIcon({
  platformId,
  platformLabel,
  showLabel = true,
}: {
  platformId: string | null | undefined;
  platformLabel?: string | null;
  showLabel?: boolean;
}) {
  const descriptor = usePlatformDescriptor(platformId);
  const { icon: Icon, testId } = resolveIconDef(descriptor?.iconKind ?? 'generic');
  const color = stableColorClass(descriptor ? `${descriptor.packId}:${descriptor.platformId}` : platformId);
  const label = platformId ? resolvePlatformLabel(platformId, platformLabel ?? descriptor?.displayName) : (platformLabel ?? '-');

  if (!showLabel) {
    return (
      <span className={`inline-flex items-center ${color}`} data-testid={testId} aria-hidden="true">
        <Icon size={16} />
      </span>
    );
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`inline-flex items-center ${color}`} data-testid={testId} aria-hidden="true">
        <Icon size={16} />
      </span>
      <span className="text-sm">{label}</span>
    </span>
  );
}
