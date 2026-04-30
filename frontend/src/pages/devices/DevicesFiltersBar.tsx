import { useState } from 'react';
import { ChevronDown, ChevronUp, Search, SlidersHorizontal, X } from 'lucide-react';
import type {
  ConnectionType,
  DeviceType,
  HardwareHealthStatus,
  HardwareTelemetryState,
} from '../../types';
import {
  CONNECTION_TYPES,
  CONNECTION_TYPE_LABELS,
  DEVICE_TYPES,
  DEVICE_TYPE_LABELS,
  HARDWARE_HEALTH_STATUSES,
  HARDWARE_HEALTH_STATUS_LABELS,
  HARDWARE_TELEMETRY_STATES,
  HARDWARE_TELEMETRY_STATE_LABELS,
} from './devicePageHelpers';
import { resolvePlatformLabel } from '../../lib/labels';
import { useDriverPackCatalog } from '../../hooks/useDriverPacks';

type Props = {
  packIdFilter: string;
  onPackIdFilterChange: (value: string) => void;
  platformFilter: string;
  onPlatformFilterChange: (value: string) => void;
  deviceTypeFilter: DeviceType | '';
  onDeviceTypeFilterChange: (value: DeviceType | '') => void;
  connectionTypeFilter: ConnectionType | '';
  onConnectionTypeFilterChange: (value: ConnectionType | '') => void;
  hardwareHealthStatusFilter: HardwareHealthStatus | '';
  onHardwareHealthStatusFilterChange: (value: HardwareHealthStatus | '') => void;
  hardwareTelemetryStateFilter: HardwareTelemetryState | '';
  onHardwareTelemetryStateFilterChange: (value: HardwareTelemetryState | '') => void;
  osVersionFilter: string;
  onOsVersionFilterChange: (value: string) => void;
  osVersions: string[];
  search: string;
  onSearchChange: (value: string) => void;
  /** Pass `undefined` when no filters are active. */
  onClear?: () => void;
};

const SELECT_CLASS =
  'h-9 min-w-[9.5rem] rounded-md border border-border bg-surface-1 px-3 text-sm text-text-2 outline-none transition focus:border-accent focus:ring-2 focus:ring-accent';
const CHIP_CLASS =
  'inline-flex items-center gap-1.5 rounded-md border border-border bg-surface-1 px-2.5 py-1.5 text-xs font-medium text-text-2';

export default function DevicesFiltersBar({
  packIdFilter,
  onPackIdFilterChange,
  platformFilter,
  onPlatformFilterChange,
  deviceTypeFilter,
  onDeviceTypeFilterChange,
  connectionTypeFilter,
  onConnectionTypeFilterChange,
  hardwareHealthStatusFilter,
  onHardwareHealthStatusFilterChange,
  hardwareTelemetryStateFilter,
  onHardwareTelemetryStateFilterChange,
  osVersionFilter,
  onOsVersionFilterChange,
  osVersions,
  search,
  onSearchChange,
  onClear,
}: Props) {
  const { data: catalog = [] } = useDriverPackCatalog();
  const packOptions = catalog.map((pack) => ({ id: pack.id, label: pack.display_name ?? pack.id }));
  const platformSource = packIdFilter ? catalog.filter((pack) => pack.id === packIdFilter) : catalog;
  const platformOptions = platformSource.flatMap((pack) =>
    (pack.platforms ?? []).map((p) => ({ id: p.id, label: resolvePlatformLabel(p.id, p.display_name) })),
  ).filter((p, idx, arr) => arr.findIndex((q) => q.id === p.id) === idx);

  const hasAdvancedFilters = Boolean(
    connectionTypeFilter || hardwareHealthStatusFilter || hardwareTelemetryStateFilter || osVersionFilter,
  );

  const [advancedOpen, setAdvancedOpen] = useState(() => hasAdvancedFilters);
  const activeAdvancedFilters = [
    connectionTypeFilter
      ? { label: `Connection: ${CONNECTION_TYPE_LABELS[connectionTypeFilter]}`, onRemove: () => onConnectionTypeFilterChange('') }
      : null,
    osVersionFilter ? { label: `OS: ${osVersionFilter}`, onRemove: () => onOsVersionFilterChange('') } : null,
    hardwareHealthStatusFilter
      ? {
          label: `Hardware: ${HARDWARE_HEALTH_STATUS_LABELS[hardwareHealthStatusFilter]}`,
          onRemove: () => onHardwareHealthStatusFilterChange(''),
        }
      : null,
    hardwareTelemetryStateFilter
      ? {
          label: `Telemetry: ${HARDWARE_TELEMETRY_STATE_LABELS[hardwareTelemetryStateFilter]}`,
          onRemove: () => onHardwareTelemetryStateFilterChange(''),
        }
      : null,
  ].filter((chip): chip is { label: string; onRemove: () => void } => chip !== null);

  return (
    <section className="mb-3 rounded-lg border border-border bg-surface-2 p-2.5">
      <div className="flex flex-col gap-2.5 xl:flex-row xl:items-center">
        <label className="relative min-w-0 flex-1">
          <span className="sr-only">Search devices</span>
          <Search
            size={16}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-3"
            aria-hidden="true"
          />
          <input
            type="text"
            placeholder="Search by name, identity, or target..."
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
            className="h-9 w-full rounded-md border border-border bg-surface-1 pl-9 pr-3 text-sm text-text-1 outline-none transition placeholder:text-text-3 focus:border-accent focus:ring-2 focus:ring-accent"
          />
        </label>

        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label="Driver pack"
            value={packIdFilter}
            onChange={(event) => {
              const nextPackId = event.target.value;
              onPackIdFilterChange(nextPackId);
              if (
                platformFilter &&
                nextPackId &&
                !catalog.find((pack) => pack.id === nextPackId)?.platforms?.some((platform) => platform.id === platformFilter)
              ) {
                onPlatformFilterChange('');
              }
            }}
            className={SELECT_CLASS}
          >
            <option value="">All packs</option>
            {packOptions.map((pack) => (
              <option key={pack.id} value={pack.id}>
                {pack.label}
              </option>
            ))}
          </select>
          <select
            aria-label="Platform"
            value={platformFilter}
            onChange={(event) => onPlatformFilterChange(event.target.value)}
            className={SELECT_CLASS}
          >
            <option value="">All platforms</option>
            {platformOptions.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
          <select
            aria-label="Device type"
            value={deviceTypeFilter}
            onChange={(event) => onDeviceTypeFilterChange(event.target.value as DeviceType | '')}
            className={SELECT_CLASS}
          >
            <option value="">All types</option>
            {DEVICE_TYPES.map((deviceType) => (
              <option key={deviceType} value={deviceType}>
                {DEVICE_TYPE_LABELS[deviceType]}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => setAdvancedOpen((open) => !open)}
            className="inline-flex h-9 items-center gap-2 rounded-md border border-border bg-surface-1 px-3 text-sm font-medium text-text-2 transition hover:border-border-strong focus:outline-none focus:ring-2 focus:ring-accent"
            aria-expanded={advancedOpen}
          >
            <SlidersHorizontal size={14} />
            More filters{activeAdvancedFilters.length > 0 ? ` (${activeAdvancedFilters.length})` : ''}
            {advancedOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </button>
          {onClear ? (
            <button
              type="button"
              onClick={onClear}
              className="inline-flex h-9 items-center rounded-md px-2 text-sm font-medium text-text-2 transition hover:text-text-1 focus:outline-none focus:ring-2 focus:ring-accent"
            >
              Clear filters
            </button>
          ) : null}
        </div>
      </div>

      {advancedOpen ? (
        <div className="mt-2.5 border-t border-border pt-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="Connection type"
              value={connectionTypeFilter}
              onChange={(event) => onConnectionTypeFilterChange(event.target.value as ConnectionType | '')}
              className={SELECT_CLASS}
            >
              <option value="">All connections</option>
              {CONNECTION_TYPES.map((connectionType) => (
                <option key={connectionType} value={connectionType}>
                  {CONNECTION_TYPE_LABELS[connectionType]}
                </option>
              ))}
            </select>
            <select
              aria-label="OS version"
              value={osVersionFilter}
              onChange={(event) => onOsVersionFilterChange(event.target.value)}
              className={SELECT_CLASS}
            >
              <option value="">All OS versions</option>
              {osVersions.map((osVersion) => (
                <option key={osVersion} value={osVersion}>
                  {osVersion}
                </option>
              ))}
            </select>
            <select
              aria-label="Hardware health"
              value={hardwareHealthStatusFilter}
              onChange={(event) => onHardwareHealthStatusFilterChange(event.target.value as HardwareHealthStatus | '')}
              className={SELECT_CLASS}
            >
              <option value="">All hardware health</option>
              {HARDWARE_HEALTH_STATUSES.map((status) => (
                <option key={status} value={status}>
                  {HARDWARE_HEALTH_STATUS_LABELS[status]}
                </option>
              ))}
            </select>
            <select
              aria-label="Telemetry state"
              value={hardwareTelemetryStateFilter}
              onChange={(event) =>
                onHardwareTelemetryStateFilterChange(event.target.value as HardwareTelemetryState | '')
              }
              className={SELECT_CLASS}
            >
              <option value="">All telemetry states</option>
              {HARDWARE_TELEMETRY_STATES.map((state) => (
                <option key={state} value={state}>
                  {HARDWARE_TELEMETRY_STATE_LABELS[state]}
                </option>
              ))}
            </select>
          </div>
        </div>
      ) : activeAdvancedFilters.length > 0 ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {activeAdvancedFilters.map((chip) => (
            <span key={chip.label} className={CHIP_CLASS}>
              {chip.label}
              <button
                type="button"
                onClick={chip.onRemove}
                className="rounded-sm text-text-3 transition hover:text-text-1 focus:outline-none focus:ring-2 focus:ring-accent"
                aria-label={`Remove filter ${chip.label}`}
              >
                <X size={12} />
              </button>
            </span>
          ))}
        </div>
      ) : null}
    </section>
  );
}
