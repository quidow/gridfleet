import type {
  ConnectionType,
  DeviceGroupFilters,
  DeviceChipStatus,
  DeviceType,
} from '../types';
import { CONNECTION_TYPE_LABELS, DEVICE_TYPE_LABELS } from '../pages/devices/devicePageHelpers';
import { DEVICE_STATUS_LABELS, resolvePlatformLabel } from './labels';

export type DeviceGroupTagDraft = {
  key: string;
  value: string;
};

export type DeviceGroupFilterDraft = {
  pack_id: string;
  platform_id: string;
  status: DeviceChipStatus | '';
  host_id: string;
  identity_value: string;
  connection_target: string;
  device_type: DeviceType | '';
  connection_type: ConnectionType | '';
  os_version: string;
  needs_attention: boolean;
  tags: DeviceGroupTagDraft[];
};

type HostNameMap = Map<string, string>;

type FilterSummaryItem = {
  key: string;
  label: string;
  value: string;
};

export function createEmptyDeviceGroupFilterDraft(): DeviceGroupFilterDraft {
  return {
    pack_id: '',
    platform_id: '',
    status: '',
    host_id: '',
    identity_value: '',
    connection_target: '',
    device_type: '',
    connection_type: '',
    os_version: '',
    needs_attention: false,
    tags: [],
  };
}

export function draftFromDeviceGroupFilters(filters: DeviceGroupFilters | null | undefined): DeviceGroupFilterDraft {
  if (!filters) {
    return createEmptyDeviceGroupFilterDraft();
  }

  return {
    pack_id: filters.pack_id ?? '',
    platform_id: filters.platform_id ?? '',
    status: filters.status ?? '',
    host_id: filters.host_id ?? '',
    identity_value: filters.identity_value ?? '',
    connection_target: filters.connection_target ?? '',
    device_type: filters.device_type ?? '',
    connection_type: filters.connection_type ?? '',
    os_version: filters.os_version ?? '',
    needs_attention: filters.needs_attention ?? false,
    tags: Object.entries(filters.tags ?? {}).map(([key, value]) => ({ key, value })),
  };
}

export function draftToDeviceGroupFilters(draft: DeviceGroupFilterDraft): DeviceGroupFilters {
  const filters: DeviceGroupFilters = {};

  if (draft.pack_id) filters.pack_id = draft.pack_id;
  if (draft.platform_id) filters.platform_id = draft.platform_id;
  if (draft.status) filters.status = draft.status;
  if (draft.host_id.trim()) filters.host_id = draft.host_id.trim();
  if (draft.identity_value.trim()) filters.identity_value = draft.identity_value.trim();
  if (draft.connection_target.trim()) filters.connection_target = draft.connection_target.trim();
  if (draft.device_type) filters.device_type = draft.device_type;
  if (draft.connection_type) filters.connection_type = draft.connection_type;
  if (draft.os_version.trim()) filters.os_version = draft.os_version.trim();
  if (draft.needs_attention) filters.needs_attention = true;

  const tags = Object.fromEntries(
    draft.tags
      .map(({ key, value }) => ({ key: key.trim(), value: value.trim() }))
      .filter(({ key, value }) => key && value)
      .map(({ key, value }) => [key, value]),
  );
  if (Object.keys(tags).length > 0) {
    filters.tags = tags;
  }

  return filters;
}

export function describeDeviceGroupFilters(
  filters: DeviceGroupFilters | null | undefined,
  hostNames: HostNameMap,
  platformLabels?: Map<string, string>,
): FilterSummaryItem[] {
  if (!filters) {
    return [];
  }

  const items: FilterSummaryItem[] = [];
  if (filters.pack_id) {
    items.push({ key: 'pack_id', label: 'Pack', value: filters.pack_id });
  }
  if (filters.platform_id) {
    const catalogLabel = platformLabels?.get(filters.platform_id) ?? null;
    items.push({ key: 'platform_id', label: 'Platform', value: resolvePlatformLabel(filters.platform_id, catalogLabel) });
  }
  if (filters.status) {
    items.push({
      key: 'status',
      label: 'Availability',
      value: DEVICE_STATUS_LABELS[filters.status],
    });
  }
  if (filters.host_id) {
    items.push({ key: 'host_id', label: 'Host', value: hostNames.get(filters.host_id) ?? filters.host_id });
  }
  if (filters.identity_value) {
    items.push({ key: 'identity_value', label: 'Identity', value: filters.identity_value });
  }
  if (filters.connection_target) {
    items.push({ key: 'connection_target', label: 'Target', value: filters.connection_target });
  }
  if (filters.device_type) {
    items.push({ key: 'device_type', label: 'Type', value: DEVICE_TYPE_LABELS[filters.device_type] });
  }
  if (filters.connection_type) {
    items.push({
      key: 'connection_type',
      label: 'Connection',
      value: CONNECTION_TYPE_LABELS[filters.connection_type],
    });
  }
  if (filters.os_version) items.push({ key: 'os_version', label: 'OS Version', value: filters.os_version });
  if (filters.needs_attention) {
    items.push({ key: 'needs_attention', label: 'Attention', value: 'Needs attention' });
  }
  for (const [key, value] of Object.entries(filters.tags ?? {})) {
    items.push({ key: `tag:${key}`, label: `Tag ${key}`, value });
  }
  return items;
}
