import type { ReactNode } from 'react';
import { Badge } from '../../components/ui';
import type { AppiumInstallable, DriverPack } from '../../types/driverPacks';

export function objectEntries(value: Record<string, unknown> | undefined): Array<[string, unknown]> {
  return Object.entries(value ?? {});
}

export function scalarValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'None';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  return JSON.stringify(value);
}

export function installableSummary(spec: AppiumInstallable | null | undefined): ReactNode {
  if (!spec) return 'Not declared';
  return (
    <span className="flex flex-wrap items-center gap-1">
      <span className="font-mono">{spec.package}</span>
      <Badge tone="neutral" size="sm">
        {spec.source}
      </Badge>
      <span className="text-text-3">{spec.version}</span>
    </span>
  );
}

export function recommendedValue(spec: AppiumInstallable | null | undefined): string {
  return spec?.recommended ?? 'None';
}

export function hasPackOperations(pack: DriverPack): boolean {
  return Object.keys(pack.features ?? {}).length > 0;
}
