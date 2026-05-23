import { Link } from 'react-router-dom';
import { Badge } from '../ui/Badge';
import { Button } from '../ui/Button';
import { DataTable, type DataTableColumn } from '../ui/DataTable';
import { SectionHeader } from '../ui/SectionHeader';
import type { ImportCommitResult } from '../../api/devicesPortability';

type CreatedRow = ImportCommitResult['created'][number];
type SkippedRow = ImportCommitResult['skipped'][number];
type FailedRow = ImportCommitResult['failed'][number];

interface Props {
  result: ImportCommitResult;
  onReset: () => void;
}

export function ImportResultsStep({ result, onReset }: Props) {
  const createdColumns: DataTableColumn<CreatedRow>[] = [
    { key: 'index', header: 'Row', width: '4rem', render: (row) => row.index },
    {
      key: 'device',
      header: 'Device',
      render: (row) => (
        <Link className="text-accent hover:underline" to={`/devices/${row.device_id}`}>
          {row.device_id}
        </Link>
      ),
    },
  ];

  const reasonColumns = <Row extends { index: number; reason: string }>(): DataTableColumn<Row>[] => [
    { key: 'index', header: 'Row', width: '4rem', render: (row) => row.index },
    { key: 'reason', header: 'Reason', render: (row) => row.reason },
  ];

  return (
    <div className="space-y-4">
      <SectionHeader
        level={3}
        title="Step 3 · Results"
        description="Commit complete. Review what changed."
      />
      <div className="flex flex-wrap gap-2">
        <Badge tone="success" size="sm">{result.created.length} created</Badge>
        <Badge tone="warning" size="sm">{result.skipped.length} skipped</Badge>
        <Badge tone="critical" size="sm">{result.failed.length} failed</Badge>
      </div>

      <section className="space-y-2">
        <SectionHeader level={3} title="Created" />
        <DataTable<CreatedRow>
          columns={createdColumns}
          rows={result.created}
          rowKey={(row) => row.index}
          caption="Created devices"
          emptyState={<p className="px-5 py-4 text-sm text-text-3">No devices created.</p>}
        />
      </section>

      <section className="space-y-2">
        <SectionHeader level={3} title="Skipped" />
        <DataTable<SkippedRow>
          columns={reasonColumns<SkippedRow>()}
          rows={result.skipped}
          rowKey={(row) => row.index}
          caption="Skipped rows"
          emptyState={<p className="px-5 py-4 text-sm text-text-3">Nothing skipped.</p>}
        />
      </section>

      <section className="space-y-2">
        <SectionHeader level={3} title="Failed" />
        <DataTable<FailedRow>
          columns={reasonColumns<FailedRow>()}
          rows={result.failed}
          rowKey={(row) => row.index}
          caption="Failed rows"
          emptyState={<p className="px-5 py-4 text-sm text-text-3">No failures.</p>}
        />
      </section>

      <div className="flex gap-2">
        <Button variant="secondary" onClick={onReset}>
          Import another bundle
        </Button>
        <Link
          to="/devices"
          className="inline-flex items-center justify-center rounded-md border border-border px-4 py-2 text-sm text-text-2 hover:bg-surface-2"
        >
          Back to devices
        </Link>
      </div>
    </div>
  );
}
