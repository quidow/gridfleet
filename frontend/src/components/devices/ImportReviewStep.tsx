import Button from "../ui/Button";
import Select from "../ui/Select";
import type { ImportPreview } from "../../api/devicesPortability";

interface Mapping {
  target_host_id: string;
  included: boolean;
}

interface Props {
  preview: ImportPreview;
  mappings: Record<number, Mapping>;
  onSetMapping: (index: number, target_host_id: string) => void;
  onToggleIncluded: (index: number) => void;
  onCommit: () => void;
}

const STATUS_LABELS: Record<string, string> = {
  valid_new: "valid",
  conflict_skip: "conflict (skip)",
  duplicate_in_bundle: "duplicate in bundle",
  invalid: "invalid",
};

export function ImportReviewStep({
  preview,
  mappings,
  onSetMapping,
  onToggleIncluded,
  onCommit,
}: Props): JSX.Element {
  const includedRows = preview.rows.filter(
    (r) => r.status === "valid_new" && mappings[r.index]?.included,
  );
  const canCommit =
    includedRows.length > 0 && includedRows.every((r) => mappings[r.index]?.target_host_id);

  const counts = preview.rows.reduce<Record<string, number>>((acc, r) => {
    acc[r.status] = (acc[r.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="space-y-4">
      <dl className="flex gap-4 text-sm text-text-2">
        <div>
          <dt className="inline">New: </dt>
          <dd className="inline font-medium">{counts.valid_new ?? 0}</dd>
        </div>
        <div>
          <dt className="inline">Skip: </dt>
          <dd className="inline font-medium">{counts.conflict_skip ?? 0}</dd>
        </div>
        <div>
          <dt className="inline">Dupes: </dt>
          <dd className="inline font-medium">{counts.duplicate_in_bundle ?? 0}</dd>
        </div>
        <div>
          <dt className="inline">Bad: </dt>
          <dd className="inline font-medium">{counts.invalid ?? 0}</dd>
        </div>
      </dl>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left">
            <th></th>
            <th>Device</th>
            <th>Original host</th>
            <th>Target host</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {preview.rows.map((row) => {
            const includable = row.status === "valid_new";
            const mapping = mappings[row.index];
            return (
              <tr key={row.index} className={includable ? undefined : "opacity-50"}>
                <td>
                  <input
                    type="checkbox"
                    disabled={!includable}
                    checked={mapping?.included ?? false}
                    onChange={() => onToggleIncluded(row.index)}
                    aria-label={`include-${row.index}`}
                  />
                </td>
                <td>{row.device.name}</td>
                <td>{row.device.original_host?.hostname ?? "—"}</td>
                <td>
                  <Select
                    disabled={!includable}
                    value={mapping?.target_host_id ?? ""}
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
                </td>
                <td>{STATUS_LABELS[row.status] ?? row.status}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="flex justify-end">
        <Button disabled={!canCommit} onClick={onCommit}>
          Commit import
        </Button>
      </div>
    </div>
  );
}
