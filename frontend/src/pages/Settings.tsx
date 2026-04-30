import { useState } from 'react';
import { Trash2 } from 'lucide-react';
import { ConfirmDialog, Button, PageHeader, Tabs, useTabParam } from '../components/ui';
import DriverPackPanel from '../components/settings/DriverPackPanel';
import PluginRegistryPanel from '../components/settings/PluginRegistryPanel';
import SettingsCategoryPanel from '../components/settings/SettingsCategoryPanel';
import WebhookRegistryPanel from '../components/settings/WebhookRegistryPanel';
import { useResetAllSettings } from '../hooks/useSettings';
import { usePageTitle } from '../hooks/usePageTitle';

const TABS = [
  { id: 'general', label: 'General', section: 'System' },
  { id: 'grid', label: 'Appium & Grid', section: 'System' },
  { id: 'agent', label: 'Agent', section: 'System' },
  { id: 'devices', label: 'Device Defaults', section: 'System' },
  { id: 'reservations', label: 'Reservations', section: 'System' },
  { id: 'retention', label: 'Data Retention', section: 'System' },
  { id: 'notifications', label: 'Notifications', section: 'Integrations' },
  { id: 'webhooks', label: 'Webhooks', section: 'Integrations' },
  { id: 'plugins', label: 'Appium Plugins', section: 'Extensions' },
  { id: 'driver-packs', label: 'Drivers', section: 'Extensions' },
];

const TAB_IDS = TABS.map((t) => t.id);
const REGISTRY_TABS = new Set(['plugins', 'webhooks']);
const CUSTOM_PANEL_TABS = new Set(['driver-packs']);

export default function Settings() {
  usePageTitle('Settings');
  const [tab, setTab] = useTabParam('tab', TAB_IDS, 'general');
  const [showResetAll, setShowResetAll] = useState(false);
  const resetAllMut = useResetAllSettings();

  return (
    <div>
      <PageHeader
        title="Settings"
        subtitle="Runtime configuration and integrations"
        actions={
          <Button variant="danger" size="sm" leadingIcon={<Trash2 size={14} />} onClick={() => setShowResetAll(true)}>
            Reset All Settings
          </Button>
        }
      />

      <Tabs tabs={TABS} activeId={tab} onChange={setTab} className="mb-6" />

      <div className="fade-in-stagger">
        {!REGISTRY_TABS.has(tab) && !CUSTOM_PANEL_TABS.has(tab) ? (
          <SettingsCategoryPanel category={tab} />
        ) : null}
        {tab === 'plugins' ? <PluginRegistryPanel /> : null}
        {tab === 'webhooks' ? <WebhookRegistryPanel /> : null}
        {tab === 'driver-packs' ? <DriverPackPanel /> : null}
      </div>

      <ConfirmDialog
        isOpen={showResetAll}
        title="Reset All Settings"
        message="This will reset all settings to their default values. This cannot be undone."
        variant="danger"
        confirmLabel="Reset All"
        onConfirm={async () => {
          await resetAllMut.mutateAsync();
          setShowResetAll(false);
        }}
        onClose={() => setShowResetAll(false)}
      />
    </div>
  );
}
