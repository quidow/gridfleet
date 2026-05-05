import { Plus, Trash2 } from 'lucide-react';
import type {
  ConnectionType,
  DeviceChipStatus,
  DeviceType,
} from '../../types';
import type { DeviceGroupFilterDraft } from '../../lib/deviceGroupFilters';
import {
  CHIP_STATUSES,
  CONNECTION_TYPES,
  CONNECTION_TYPE_LABELS,
  DEVICE_TYPES,
  DEVICE_TYPE_LABELS,
} from './devicePageHelpers';
import { DEVICE_STATUS_LABELS, resolvePlatformLabel } from '../../lib/labels';
import { useDriverPackCatalog } from '../../hooks/useDriverPacks';

interface Props {
  filters: DeviceGroupFilterDraft;
  onChange: (filters: DeviceGroupFilterDraft) => void;
  hostOptions: Array<{ id: string; name: string }>;
  osVersionOptions: string[];
  showLabel?: boolean;
}

function updateOptionalField<K extends keyof Omit<DeviceGroupFilterDraft, 'tags'>>(
  filters: DeviceGroupFilterDraft,
  onChange: (filters: DeviceGroupFilterDraft) => void,
  field: K,
  value: DeviceGroupFilterDraft[K],
) {
  onChange({ ...filters, [field]: value });
}

export default function FilterBuilder({
  filters,
  onChange,
  hostOptions,
  osVersionOptions,
  showLabel = true,
}: Props) {
  function addTagRow() {
    onChange({ ...filters, tags: [...filters.tags, { key: '', value: '' }] });
  }

  function updateTagRow(index: number, patch: Partial<DeviceGroupFilterDraft['tags'][number]>) {
    onChange({
      ...filters,
      tags: filters.tags.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row)),
    });
  }

  function removeTagRow(index: number) {
    onChange({ ...filters, tags: filters.tags.filter((_, rowIndex) => rowIndex !== index) });
  }

  const availableOsVersions = Array.from(new Set([filters.os_version, ...osVersionOptions].filter(Boolean)));
  const { data: catalog = [] } = useDriverPackCatalog();
  const packOptions = catalog.map((pack) => ({ id: pack.id, label: pack.display_name ?? pack.id }));
  const platformSource = filters.pack_id ? catalog.filter((pack) => pack.id === filters.pack_id) : catalog;
  const platformOptions = platformSource.flatMap((pack) =>
    (pack.platforms ?? []).map((p) => ({ id: p.id, label: resolvePlatformLabel(p.id, p.display_name) })),
  ).filter((p, idx, arr) => arr.findIndex((q) => q.id === p.id) === idx);

  return (
    <div className="space-y-4">
      {showLabel && <label className="block text-sm font-medium text-text-2">Filters</label>}
      <div className="grid gap-3 md:grid-cols-2">
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Pack</span>
          <select
            value={filters.pack_id}
            onChange={(event) => {
              const nextPackId = event.target.value;
              const nextPack = catalog.find((pack) => pack.id === nextPackId);
              const platformStillValid =
                !nextPackId || !filters.platform_id || nextPack?.platforms?.some((platform) => platform.id === filters.platform_id);
              onChange({
                ...filters,
                pack_id: nextPackId,
                platform_id: platformStillValid ? filters.platform_id : '',
              });
            }}
            className="w-full rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          >
            <option value="">Any pack</option>
            {packOptions.map((pack) => (
              <option key={pack.id} value={pack.id}>{pack.label}</option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Platform</span>
          <select
            value={filters.platform_id}
            onChange={(event) => updateOptionalField(filters, onChange, 'platform_id', event.target.value)}
            className="w-full rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          >
            <option value="">Any platform</option>
            {platformOptions.map((p) => (
              <option key={p.id} value={p.id}>{p.label}</option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Availability</span>
          <select
            value={filters.status}
            onChange={(event) =>
              updateOptionalField(filters, onChange, 'status', event.target.value as DeviceChipStatus | '')
            }
            className="w-full rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          >
            <option value="">Any availability</option>
            {CHIP_STATUSES.map((status) => (
              <option key={status} value={status}>{DEVICE_STATUS_LABELS[status]}</option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Host</span>
          <select
            value={filters.host_id}
            onChange={(event) => updateOptionalField(filters, onChange, 'host_id', event.target.value)}
            className="w-full rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          >
            <option value="">Any host</option>
            {hostOptions.map((host) => (
              <option key={host.id} value={host.id}>{host.name}</option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Device Type</span>
          <select
            value={filters.device_type}
            onChange={(event) => updateOptionalField(filters, onChange, 'device_type', event.target.value as DeviceType | '')}
            className="w-full rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          >
            <option value="">Any type</option>
            {DEVICE_TYPES.map((deviceType) => (
              <option key={deviceType} value={deviceType}>{DEVICE_TYPE_LABELS[deviceType]}</option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Connection Type</span>
          <select
            value={filters.connection_type}
            onChange={(event) =>
              updateOptionalField(filters, onChange, 'connection_type', event.target.value as ConnectionType | '')
            }
            className="w-full rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          >
            <option value="">Any connection</option>
            {CONNECTION_TYPES.map((connectionType) => (
              <option key={connectionType} value={connectionType}>{CONNECTION_TYPE_LABELS[connectionType]}</option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">OS Version</span>
          <select
            value={filters.os_version}
            onChange={(event) => updateOptionalField(filters, onChange, 'os_version', event.target.value)}
            className="w-full rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          >
            <option value="">Any OS version</option>
            {availableOsVersions.map((osVersion) => (
              <option key={osVersion} value={osVersion}>{osVersion}</option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Identity Value</span>
          <input
            value={filters.identity_value}
            onChange={(event) => updateOptionalField(filters, onChange, 'identity_value', event.target.value)}
            placeholder="Exact identity match"
            className="w-full rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          />
        </label>
        <label className="space-y-1 md:col-span-2">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Connection Target</span>
          <input
            value={filters.connection_target}
            onChange={(event) => updateOptionalField(filters, onChange, 'connection_target', event.target.value)}
            placeholder="Exact connection target match"
            className="w-full rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          />
        </label>
      </div>

      <div className="space-y-2 rounded-lg border border-dashed border-border-strong p-3">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm font-medium text-text-2">Tags</div>
            <div className="text-xs text-text-3">All listed tags must match exactly.</div>
          </div>
          <button
            type="button"
            onClick={addTagRow}
            className="inline-flex items-center gap-1 text-sm text-accent hover:text-accent-hover"
          >
            <Plus size={14} /> Add tag
          </button>
        </div>

        {filters.tags.length === 0 ? (
          <p className="text-sm text-text-3">No tag filters.</p>
        ) : (
          filters.tags.map((row, index) => (
            <div key={index} className="flex items-center gap-2">
              <input
                value={row.key}
                onChange={(event) => updateTagRow(index, { key: event.target.value })}
                placeholder="Tag key"
                className="w-40 rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
              />
              <input
                value={row.value}
                onChange={(event) => updateTagRow(index, { value: event.target.value })}
                placeholder="Tag value"
                className="flex-1 rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
              />
              <button type="button" onClick={() => removeTagRow(index)} className="p-1 text-text-3 hover:text-danger-foreground">
                <Trash2 size={16} />
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
