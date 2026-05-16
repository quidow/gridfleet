import { Link } from 'react-router-dom';
import { Pencil } from 'lucide-react';
import { deviceChipStatus } from '../../lib/deviceState';
import { CONNECTION_TYPE_LABELS, DEVICE_STATUS_LABELS, DEVICE_TYPE_LABELS, resolvePlatformLabel } from '../../lib/labels';
import type { DeviceDetail } from '../../types';
import { formatDate } from './utils';
import DefinitionList from '../ui/DefinitionList';
import { EMPTY_GLYPH } from '../../utils/emptyValue';

type Props = {
  device: DeviceDetail;
  hostLabel?: string;
  onEdit?: () => void;
};

const SOFTWARE_VERSION_LABELS: Record<string, string> = {
  android: 'Android',
  build: 'Build',
  build_number: 'Build Number',
  fire_os: 'Fire OS',
  fire_os_compat: 'Fire OS Routing',
  roku_os: 'Roku OS',
  sdk: 'SDK',
};

function formatSoftwareVersionKey(key: string): string {
  return SOFTWARE_VERSION_LABELS[key] ?? key
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
    .join(' ');
}

function softwareVersionsList(versions: Record<string, unknown> | null | undefined) {
  const entries = Object.entries(versions ?? {})
    .filter((entry): entry is [string, string | number | boolean] =>
      ['string', 'number', 'boolean'].includes(typeof entry[1]) && String(entry[1]).length > 0,
    )
    .sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) return EMPTY_GLYPH;
  return (
    <dl className="space-y-1">
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-baseline justify-between gap-3">
          <dt className="text-text-3">{formatSoftwareVersionKey(key)}</dt>
          <dd className="text-right font-mono text-xs tabular-nums text-text-1">{String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function tagsList(tags: DeviceDetail['tags']) {
  const entries = Object.entries(tags ?? {}).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) return EMPTY_GLYPH;
  return (
    <div className="flex flex-wrap gap-1.5">
      {entries.map(([key, value]) => (
        <span key={key} className="rounded bg-surface-2 px-2 py-0.5 text-xs text-text-2">
          {key}: {String(value)}
        </span>
      ))}
    </div>
  );
}

export default function DeviceInfoPanel({ device, hostLabel, onEdit }: Props) {
  const reservation = device.reservation;
  const reservationLabel = reservation
    ? `${reservation.run_name}${reservation.excluded ? ' (excluded)' : ''}`
    : EMPTY_GLYPH;
  const status = deviceChipStatus(device);

  return (
    <div className="p-5">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-text-1">Device Info</h2>
          <p className="mt-1 text-xs text-text-2">Stable identity, active connection target, and platform details.</p>
        </div>
        {onEdit ? (
          <button
            type="button"
            onClick={onEdit}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border bg-surface-2 px-2.5 py-1 text-xs font-medium text-text-2 hover:bg-surface-1"
          >
            <Pencil size={12} />
            Edit
          </button>
        ) : null}
      </div>
      <DefinitionList
        layout="stacked"
        items={[
          { term: 'Identity', definition: device.identity_value },
          { term: 'Connection Target', definition: device.connection_target ?? EMPTY_GLYPH },
          { term: 'Platform', definition: resolvePlatformLabel(device.platform_id, device.platform_label) },
          { term: 'Device Type', definition: DEVICE_TYPE_LABELS[device.device_type] ?? device.device_type },
          { term: 'Manufacturer', definition: device.manufacturer ?? EMPTY_GLYPH },
          { term: 'Model Name', definition: device.model ?? EMPTY_GLYPH },
          { term: 'Model Number', definition: device.model_number ?? EMPTY_GLYPH },
          { term: 'Connection', definition: CONNECTION_TYPE_LABELS[device.connection_type] ?? device.connection_type },
          { term: 'IP Address', definition: device.ip_address ?? EMPTY_GLYPH },
          { term: 'OS Version', definition: device.os_version_display ?? device.os_version },
          { term: 'Software Versions', definition: softwareVersionsList(device.software_versions) },
          { term: 'Tags', definition: tagsList(device.tags) },
          {
            term: 'Availability',
            definition: DEVICE_STATUS_LABELS[status] ?? status,
          },
          { term: 'Reserved By', definition: reservationLabel },
          {
            term: 'Reservation Issue',
            definition: reservation?.excluded ? (reservation.exclusion_reason ?? 'Excluded') : EMPTY_GLYPH,
          },
          { term: 'Created', definition: formatDate(device.created_at) },
          { term: 'Updated', definition: formatDate(device.updated_at) },
          {
            term: 'Host',
            definition: (
              <Link to={`/hosts/${device.host_id}`} className="font-medium text-accent hover:underline">
                {hostLabel ?? device.host_id}
              </Link>
            ),
          },
        ]}
      />
    </div>
  );
}
