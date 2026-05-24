import { useMemo, useState } from "react";

import { downloadInventory, type InventoryFormat } from "../../api/devicesInventory";
import { Button } from "../ui/Button";
import { Checkbox } from "../ui/Checkbox";
import { Modal } from "../ui/Modal";

const STORAGE_KEY = "gridfleet:inventory-export-columns";

const COLUMN_GROUPS: { label: string; columns: string[] }[] = [
  {
    label: "Identity",
    columns: [
      "name",
      "identity.scheme",
      "identity.scope",
      "identity.value",
      "pack_id",
      "platform_id",
      "device_type",
      "connection_type",
      "connection_target",
    ],
  },
  { label: "Host", columns: ["host.id", "host.hostname"] },
  { label: "State", columns: ["operational_state", "hold", "review_required"] },
  {
    label: "Hardware",
    columns: [
      "os_version",
      "manufacturer",
      "model",
      "model_number",
      "hardware.battery_level_percent",
      "hardware.battery_temperature_c",
      "hardware.charging_state",
      "hardware.health_status",
      "hardware.telemetry_reported_at",
      "software_versions",
    ],
  },
  {
    label: "Verification",
    columns: [
      "verification.verified_at",
      "verification.session_viability_status",
      "verification.device_checks_healthy",
      "verification.device_checks_checked_at",
    ],
  },
  { label: "Tags / Config", columns: ["tags", "device_config", "test_data"] },
  { label: "Timestamps", columns: ["id", "created_at", "updated_at"] },
];

const DEFAULT_COLUMNS = [
  "name",
  "host.hostname",
  "identity.value",
  "pack_id",
  "platform_id",
  "os_version",
  "operational_state",
  "hold",
  "verification.verified_at",
];

interface Props {
  isOpen: boolean;
  onClose: () => void;
  filters: Record<string, string | string[] | undefined>;
}

export function DeviceInventoryExportModal({ isOpen, onClose, filters }: Props) {
  const [format, setFormat] = useState<InventoryFormat>("csv");
  const [selected, setSelected] = useState<Set<string>>(() => loadStored() ?? new Set(DEFAULT_COLUMNS));
  const [busy, setBusy] = useState(false);

  const toggle = (col: string): void => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(col)) next.delete(col);
      else next.add(col);
      return next;
    });
  };

  const onDownload = async (): Promise<void> => {
    setBusy(true);
    try {
      const columns = Array.from(selected);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(columns));
      await downloadInventory({ format, columns, filters });
      onClose();
    } finally {
      setBusy(false);
    }
  };

  const summary = useMemo(() => `${selected.size} columns selected`, [selected]);

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Export Inventory"
      size="lg"
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={busy || selected.size === 0} onClick={onDownload}>
            {busy ? "Downloading…" : "Download"}
          </Button>
        </div>
      }
    >
      <div className="space-y-4">
        <div className="flex items-center gap-4 text-sm">
          <label className="inline-flex items-center gap-1">
            <input
              type="radio"
              name="inventory-format"
              checked={format === "csv"}
              onChange={() => setFormat("csv")}
              aria-label="CSV"
            />
            CSV
          </label>
          <label className="inline-flex items-center gap-1">
            <input
              type="radio"
              name="inventory-format"
              checked={format === "json"}
              onChange={() => setFormat("json")}
              aria-label="JSON"
            />
            JSON
          </label>
          <span className="ml-auto text-sm text-text-2">{summary}</span>
        </div>
        <div className="max-h-80 overflow-y-auto pr-2">
          {COLUMN_GROUPS.map((group) => (
            <fieldset key={group.label} className="mb-3">
              <legend className="text-xs font-semibold uppercase text-text-3">{group.label}</legend>
              <div className="mt-1 grid grid-cols-2 gap-1">
                {group.columns.map((col) => (
                  <Checkbox
                    key={col}
                    checked={selected.has(col)}
                    onChange={() => toggle(col)}
                    label={<span className="font-mono">{col}</span>}
                    aria-label={col}
                  />
                ))}
              </div>
            </fieldset>
          ))}
        </div>
      </div>
    </Modal>
  );
}

function loadStored(): Set<string> | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return null;
    return new Set(parsed.filter((v): v is string => typeof v === "string"));
  } catch {
    return null;
  }
}
