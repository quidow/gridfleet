import { Terminal } from 'lucide-react';
import { DashedEmptyPanel } from '../ui/DashedEmptyPanel';

export function DeviceLogsEmptyPanel() {
  return (
    <DashedEmptyPanel
      icon={Terminal}
      title="No Appium logs yet"
      description="Logs appear here once the Appium node emits output."
    />
  );
}
