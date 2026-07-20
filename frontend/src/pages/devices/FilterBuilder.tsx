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
import { useDeviceGroups } from '../../hooks/useDeviceGroups';
import { Select } from '../../components/ui/Select';
import { TextField } from '../../components/ui/TextField';
import { Checkbox } from '../../components/ui/Checkbox';

interface Props {
  filters: DeviceGroupFilterDraft;
  onChange: (filters: DeviceGroupFilterDraft) => void;
  hostOptions: Array<{ id: string; name: string }>;
  osVersionOptions: string[];
  showLabel?: boolean;
}

function updateOptionalField<K extends keyof Omit<DeviceGroupFilterDraft, 'member_of'>>(
  filters: DeviceGroupFilterDraft,
  onChange: (filters: DeviceGroupFilterDraft) => void,
  field: K,
  value: DeviceGroupFilterDraft[K],
) {
  onChange({ ...filters, [field]: value });
}

export function FilterBuilder({
  filters,
  onChange,
  hostOptions,
  osVersionOptions,
  showLabel = true,
}: Props) {
  const availableOsVersions = Array.from(new Set([filters.os_version, ...osVersionOptions].filter(Boolean)));
  const { data: groups = [] } = useDeviceGroups();
  const staticGroups = groups.filter(g => g.group_type === 'static');
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
          <Select
            value={filters.pack_id}
            onChange={(nextPackId) => {
              const nextPack = catalog.find((pack) => pack.id === nextPackId);
              const platformStillValid =
                !nextPackId || !filters.platform_id || nextPack?.platforms?.some((platform) => platform.id === filters.platform_id);
              onChange({
                ...filters,
                pack_id: nextPackId,
                platform_id: platformStillValid ? filters.platform_id : '',
              });
            }}
            fullWidth
            options={[{ value: '', label: 'Any pack' }, ...packOptions.map((pack) => ({ value: pack.id, label: pack.label }))]}
          />
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Platform</span>
          <Select
            value={filters.platform_id}
            onChange={(next) => updateOptionalField(filters, onChange, 'platform_id', next)}
            fullWidth
            options={[{ value: '', label: 'Any platform' }, ...platformOptions.map((p) => ({ value: p.id, label: p.label }))]}
          />
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Availability</span>
          <Select
            value={filters.status}
            onChange={(next) => updateOptionalField(filters, onChange, 'status', next as DeviceChipStatus | '')}
            fullWidth
            options={[
              { value: '', label: 'Any availability' },
              ...CHIP_STATUSES.map((status) => ({ value: status, label: DEVICE_STATUS_LABELS[status] })),
            ]}
          />
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Host</span>
          <Select
            value={filters.host_id}
            onChange={(next) => updateOptionalField(filters, onChange, 'host_id', next)}
            fullWidth
            options={[{ value: '', label: 'Any host' }, ...hostOptions.map((host) => ({ value: host.id, label: host.name }))]}
          />
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Device Type</span>
          <Select
            value={filters.device_type}
            onChange={(next) => updateOptionalField(filters, onChange, 'device_type', next as DeviceType | '')}
            fullWidth
            options={[
              { value: '', label: 'Any type' },
              ...DEVICE_TYPES.map((deviceType) => ({ value: deviceType, label: DEVICE_TYPE_LABELS[deviceType] })),
            ]}
          />
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Connection Type</span>
          <Select
            value={filters.connection_type}
            onChange={(next) => updateOptionalField(filters, onChange, 'connection_type', next as ConnectionType | '')}
            fullWidth
            options={[
              { value: '', label: 'Any connection' },
              ...CONNECTION_TYPES.map((connectionType) => ({
                value: connectionType,
                label: CONNECTION_TYPE_LABELS[connectionType],
              })),
            ]}
          />
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">OS Version</span>
          <Select
            value={filters.os_version}
            onChange={(next) => updateOptionalField(filters, onChange, 'os_version', next)}
            fullWidth
            options={[
              { value: '', label: 'Any OS version' },
              ...availableOsVersions.map((osVersion) => ({ value: osVersion, label: osVersion })),
            ]}
          />
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Identity Value</span>
          <TextField
            value={filters.identity_value}
            onChange={(value) => updateOptionalField(filters, onChange, 'identity_value', value)}
            placeholder="Exact identity match"
          />
        </label>
        <label className="space-y-1 md:col-span-2">
          <span className="block text-xs font-medium uppercase tracking-wide text-text-3">Connection Target</span>
          <TextField
            value={filters.connection_target}
            onChange={(value) => updateOptionalField(filters, onChange, 'connection_target', value)}
            placeholder="Exact connection target match"
          />
        </label>
      </div>

      <div className="space-y-2 rounded-lg border border-dashed border-border-strong p-3">
        <div className="text-sm font-medium text-text-2 mb-2">Member of Groups</div>
        {staticGroups.length === 0 ? (
          <p className="text-sm text-text-3">No static groups available.</p>
        ) : (
          <div className="max-h-48 overflow-y-auto space-y-1">
            {staticGroups.map(group => (
              <Checkbox
                key={group.key}
                checked={filters.member_of.includes(group.key)}
                onChange={() => {
                  const next = new Set(filters.member_of);
                  if (next.has(group.key)) next.delete(group.key);
                  else next.add(group.key);
                  onChange({ ...filters, member_of: Array.from(next) });
                }}
                label={group.name}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
