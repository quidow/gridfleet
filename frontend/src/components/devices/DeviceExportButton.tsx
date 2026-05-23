import { Download } from "lucide-react";
import { useState } from "react";

import { downloadExportBundle } from "../../api/devicesPortability";
import Button from "../ui/Button";

export function DeviceExportButton(): JSX.Element {
  const [busy, setBusy] = useState(false);
  const handleClick = async (): Promise<void> => {
    setBusy(true);
    try {
      await downloadExportBundle();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button
      variant="secondary"
      onClick={handleClick}
      disabled={busy}
      leadingIcon={<Download size={16} />}
    >
      {busy ? "Exporting…" : "Export Config"}
    </Button>
  );
}
