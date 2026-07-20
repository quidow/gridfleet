import { useState } from 'react';
import { ChevronDown, ChevronUp, Search, SlidersHorizontal, X } from 'lucide-react';
import type {
  ConnectionType,
  DeviceType,
  HealthVerdictStatus,
} from '../../types';
import {
  CONNECTION_TYPES,
  CONNECTION_TYPE_LABELS,
  DEVICE_TYPES,
  DEVICE_TYPE_LABELS,
  HEALTH_VERDICT_FILTER_LABELS,
  HEALTH_VERDICT_STATUSES,
} from './devicePageHelpers';
import { resolvePlatformLabel } from '../../lib/labels';
import { useDriverPackCatalog } from '../../hooks/useDriverPacks';
import { Select } from '../../components/ui/Select';
import { TextField } from '../../components/ui/TextField';

type Props = {
  packIdFilter: string;
  onPackIdFilterChange: (value: string) => void;
  platformFilter: string;
  onPlatformFilterChange: (value: string) => void;
  deviceTypeFilter: DeviceType | '';
  onDeviceTypeFilterChange: (value: DeviceType | '') => void;
  connectionTypeFilter: ConnectionType | '';
  onConnectionTypeFilterChange: (value: ConnectionType | '') => void;
  deviceHealthFilter: HealthVerdictStatus | '';
  onDeviceHealthFilterChange: (value: HealthVerdictStatus | '') => void;
  nodeHealthFilter: HealthVerdictStatus | '';
  onNodeHealthFilterChange: (value: HealthVerdictStatus | '') => void;
  viabilityFilter: HealthVerdictStatus | '';
  onViabilityFilterChange: (value: HealthVerdictStatus | '') => void;
  osVersionFilter: string;
  onOsVersionFilterChange: (value: string) => void;
  osVersions: string[];
  search: string;
  onSearchChange: (value: string) => void;
  /** Pass `undefined` when no filters are active. */
  onClear?: () => void;
};

const SELECT_CLASS = 'h-9 min-w-[9.5rem]';
const CHIP_CLASS =
  'inline-flex items-center gap-1.5 rounded-md border border-border bg-surface-1 px-2.5 py-1.5 text-xs font-medium text-text-2';

export function DevicesFiltersBar({
  packIdFilter,
  onPackIdFilterChange,
  platformFilter,
  onPlatformFilterChange,
  deviceTypeFilter,
  onDeviceTypeFilterChange,
  connectionTypeFilter,
  onConnectionTypeFilterChange,
  deviceHealthFilter,
  onDeviceHealthFilterChange,
  nodeHealthFilter,
  onNodeHealthFilterChange,
  viabilityFilter,
  onViabilityFilterChange,
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
    connectionTypeFilter || deviceHealthFilter ||
    nodeHealthFilter || viabilityFilter || osVersionFilter,
  );

  const [advancedOpen, setAdvancedOpen] = useState(() => hasAdvancedFilters);
  const activeAdvancedFilters = [
    connectionTypeFilter
      ? { label: `Connection: ${CONNECTION_TYPE_LABELS[connectionTypeFilter]}`, onRemove: () => onConnectionTypeFilterChange('') }
      : null,
    osVersionFilter ? { label: `OS: ${osVersionFilter}`, onRemove: () => onOsVersionFilterChange('') } : null,
    deviceHealthFilter
      ? {
          label: `Device: ${HEALTH_VERDICT_FILTER_LABELS[deviceHealthFilter]}`,
          onRemove: () => onDeviceHealthFilterChange(''),
        }
      : null,
    nodeHealthFilter
      ? {
          label: `Node: ${HEALTH_VERDICT_FILTER_LABELS[nodeHealthFilter]}`,
          onRemove: () => onNodeHealthFilterChange(''),
        }
      : null,
    viabilityFilter
      ? {
          label: `Viability: ${HEALTH_VERDICT_FILTER_LABELS[viabilityFilter]}`,
          onRemove: () => onViabilityFilterChange(''),
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
          <TextField
            placeholder="Search by name, identity, or target..."
            value={search}
            onChange={onSearchChange}
            className="h-9 pl-9 pr-3"
          />
        </label>

        <div className="flex flex-wrap items-center gap-2">
          <Select
            ariaLabel="Driver pack"
            value={packIdFilter}
            onChange={(nextPackId) => {
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
            options={[{ value: '', label: 'All packs' }, ...packOptions.map((pack) => ({ value: pack.id, label: pack.label }))]}
          />
          <Select
            ariaLabel="Platform"
            value={platformFilter}
            onChange={(next) => onPlatformFilterChange(next)}
            className={SELECT_CLASS}
            options={[{ value: '', label: 'All platforms' }, ...platformOptions.map((p) => ({ value: p.id, label: p.label }))]}
          />
          <Select
            ariaLabel="Device type"
            value={deviceTypeFilter}
            onChange={(next) => onDeviceTypeFilterChange(next as DeviceType | '')}
            className={SELECT_CLASS}
            options={[
              { value: '', label: 'All types' },
              ...DEVICE_TYPES.map((deviceType) => ({ value: deviceType, label: DEVICE_TYPE_LABELS[deviceType] })),
            ]}
          />
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
            <Select
              ariaLabel="Connection type"
              value={connectionTypeFilter}
              onChange={(next) => onConnectionTypeFilterChange(next as ConnectionType | '')}
              className={SELECT_CLASS}
              options={[
                { value: '', label: 'All connections' },
                ...CONNECTION_TYPES.map((connectionType) => ({
                  value: connectionType,
                  label: CONNECTION_TYPE_LABELS[connectionType],
                })),
              ]}
            />
            <Select
              ariaLabel="OS version"
              value={osVersionFilter}
              onChange={(next) => onOsVersionFilterChange(next)}
              className={SELECT_CLASS}
              options={[
                { value: '', label: 'All OS versions' },
                ...osVersions.map((osVersion) => ({ value: osVersion, label: osVersion })),
              ]}
            />
            <Select
              ariaLabel="Filter by device health"
              value={deviceHealthFilter}
              onChange={(next) => onDeviceHealthFilterChange(next as HealthVerdictStatus | '')}
              className={SELECT_CLASS}
              options={[
                { value: '', label: 'Any device health' },
                ...HEALTH_VERDICT_STATUSES.map((s) => ({ value: s, label: HEALTH_VERDICT_FILTER_LABELS[s] })),
              ]}
            />
            <Select
              ariaLabel="Filter by node health"
              value={nodeHealthFilter}
              onChange={(next) => onNodeHealthFilterChange(next as HealthVerdictStatus | '')}
              className={SELECT_CLASS}
              options={[
                { value: '', label: 'Any node state' },
                ...HEALTH_VERDICT_STATUSES.map((s) => ({ value: s, label: HEALTH_VERDICT_FILTER_LABELS[s] })),
              ]}
            />
            <Select
              ariaLabel="Filter by viability"
              value={viabilityFilter}
              onChange={(next) => onViabilityFilterChange(next as HealthVerdictStatus | '')}
              className={SELECT_CLASS}
              options={[
                { value: '', label: 'Any viability' },
                ...HEALTH_VERDICT_STATUSES.map((s) => ({ value: s, label: HEALTH_VERDICT_FILTER_LABELS[s] })),
              ]}
            />
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
