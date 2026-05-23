import { Card } from '../ui/Card';
import { SectionHeader } from '../ui/SectionHeader';
import { SettingsPanelLayout } from './SettingsPanelLayout';
import { DeviceExportButton } from './DeviceExportButton';
import { DeviceImportPanel } from './DeviceImportPanel';

export function BackupRestorePanel() {
  return (
    <SettingsPanelLayout
      title="Backup & Restore"
      description="Export the device configuration bundle or import devices from another GridFleet instance."
    >
      <div className="space-y-6">
        <Card padding="md" className="space-y-3">
          <SectionHeader
            level={3}
            title="Export configuration"
            description="Download a portability bundle of every device, ready to import into another GridFleet instance."
          />
          <DeviceExportButton />
        </Card>

        <Card padding="md" className="space-y-3">
          <SectionHeader
            level={3}
            title="Import devices"
            description="Upload a portability bundle, review the rows, choose host mappings, then commit."
          />
          <DeviceImportPanel />
        </Card>
      </div>
    </SettingsPanelLayout>
  );
}
