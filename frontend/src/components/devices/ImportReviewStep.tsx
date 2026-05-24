import { Badge, type BadgeTone } from '../ui/Badge';
import { Button } from '../ui/Button';
import { Checkbox } from '../ui/Checkbox';
import { DataTable, type DataTableColumn } from '../ui/DataTable';
import { SectionHeader } from '../ui/SectionHeader';
import { Select } from '../ui/Select';
import type { ImportPreview } from '../../api/devicesPortability';

interface Mapping {
  target_host_id: string;
  included: boolean;
}

type PreviewRow = ImportPreview['rows'][number];

interface Props {
  preview: ImportPreview;
  mappings: Record<number, Mapping>;
  onSetMapping: (index: number, target_host_id: string) => void;
  onToggleIncluded: (index: number) => void;
  onCommit: () => void;
}

const STATUS_TONE: Record<string, BadgeTone> = {
  valid_new: 'success',
  conflict_skip: 'warning',
  duplicate_in_bundle: 'warning',
  invalid: 'critical',
};

const STATUS_LABEL: Record<string, string> = {
  valid_new: 'valid',
  conflict_skip: 'conflict (skip)',
  duplicate_in_bundle: 'duplicate in bundle',
  invalid: 'invalid',
};

export function ImportReviewStep({
  preview,
  mappings,
  onSetMapping,
  onToggleIncluded,
  onCommit,
}: Props) {
  const includedRows = preview.rows.filter(
    (r) => r.status === 'valid_new' && mappings[r.index]?.included,
  );
  const canCommit =
    includedRows.length > 0 && includedRows.every((r) => mappings[r.index]?.target_host_id);

  const counts = preview.rows.reduce<Record<string, number>>((acc, r) => {
    acc[r.status] = (acc[r.status] ?? 0) + 1;
    return acc;
  }, {});

  const columns: DataTableColumn<PreviewRow>[] = [
    {
      key: 'include',
      header: '',
      width: '2.5rem',
      render: (row) => {
        const includable = row.status === 'valid_new';
        const mapping = mappings[row.index];
        return (
          <Checkbox
            disabled={!includable}
            checked={mapping?.included ?? false}
            onChange={() => onToggleIncluded(row.index)}
            label=""
            aria-label={`include-${row.index}`}
          />
        );
      },
    },
    {
      key: 'device',
      header: 'Device',
      render: (row) => row.device.name,
    },
    {
      key: 'original-host',
      header: 'Original host',
      render: (row) => row.device.original_host?.hostname ?? '—',
    },
    {
      key: 'target-host',
      header: 'Target host',
      render: (row) => {
        const includable = row.status === 'valid_new';
        const mapping = mappings[row.index];
        return (
          <Select
            disabled={!includable}
            value={mapping?.target_host_id ?? ''}
            onChange={(value) => onSetMapping(row.index, value)}
            ariaLabel={`host-${row.index}`}
            size="sm"
          >
            <option value="">—</option>
            {preview.available_hosts.map((h) => (
              <option key={h.id} value={h.id}>
                {h.hostname}
              </option>
            ))}
          </Select>
        );
      },
    },
    {
      key: 'status',
      header: 'Status',
      render: (row) => (
        <Badge tone={STATUS_TONE[row.status] ?? 'neutral'} size="sm">
          {STATUS_LABEL[row.status] ?? row.status}
        </Badge>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <SectionHeader
        level={3}
        title="Step 2 · Review and map hosts"
        description="Pick which rows to import and which host each should land on."
      />
      <div className="flex flex-wrap gap-2">
        <Badge tone="success" size="sm">{counts.valid_new ?? 0} new</Badge>
        <Badge tone="warning" size="sm">{counts.conflict_skip ?? 0} skip</Badge>
        <Badge tone="warning" size="sm">{counts.duplicate_in_bundle ?? 0} duplicates</Badge>
        <Badge tone="critical" size="sm">{counts.invalid ?? 0} invalid</Badge>
      </div>
      <DataTable<PreviewRow>
        columns={columns}
        rows={preview.rows}
        rowKey={(row) => row.index}
        rowClassName={(row) => (row.status === 'valid_new' ? undefined : 'opacity-50')}
        caption="Import preview rows"
      />
      <div className="flex justify-end">
        <Button disabled={!canCommit} onClick={onCommit}>
          Commit import
        </Button>
      </div>
    </div>
  );
}
