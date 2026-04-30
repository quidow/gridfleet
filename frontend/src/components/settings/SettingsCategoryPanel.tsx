import LoadingSpinner from '../LoadingSpinner';
import SettingsSection from '../SettingsSection';
import { useBulkUpdateSettings, useResetSetting, useSettings } from '../../hooks/useSettings';
import { buildSettingsSections } from './settingsSections';
import SettingsPanelLayout from './SettingsPanelLayout';

const CATEGORY_LABELS: Record<string, { title: string; description?: string }> = {
  general: { title: 'General', description: 'Core health-check, session, and recovery timing.' },
  grid: { title: 'Appium & Grid', description: 'Grid routing and Appium node configuration.' },
  notifications: { title: 'Notifications', description: 'Toast events and delivery settings.' },
  devices: { title: 'Device Defaults', description: 'Default values applied when registering devices.' },
  agent: { title: 'Agent', description: 'Agent enrollment and heartbeat behavior.' },
  reservations: { title: 'Reservations', description: 'Run-default reservation settings.' },
  retention: { title: 'Data Retention', description: 'How long sessions, runs, and events are kept.' },
};

type Props = {
  category: string;
};

export default function SettingsCategoryPanel({ category }: Props) {
  const { data: settingsData, isLoading } = useSettings();
  const bulkUpdateMut = useBulkUpdateSettings();
  const resetSettingMut = useResetSetting();
  const currentCategory = settingsData?.find((group) => group.category === category);
  const sections = currentCategory ? buildSettingsSections(category, currentCategory.settings) : [];
  const meta = CATEGORY_LABELS[category] ?? { title: category };

  if (isLoading) {
    return <LoadingSpinner />;
  }

  if (!currentCategory) {
    return null;
  }

  return (
    <SettingsPanelLayout title={meta.title} description={meta.description}>
      <SettingsSection
        sections={sections}
        onSave={async (updates) => {
          await bulkUpdateMut.mutateAsync(updates);
        }}
        onReset={async (key) => {
          await resetSettingMut.mutateAsync(key);
        }}
      />
    </SettingsPanelLayout>
  );
}
