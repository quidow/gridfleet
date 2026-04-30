import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import SettingField from './SettingField';
import type { SettingsSectionGroup } from './settings/settingsSections';
import type { SettingRead } from '../types';

interface Props {
  sections: SettingsSectionGroup[];
  onSave: (updates: Record<string, unknown>) => Promise<void>;
  onReset: (key: string) => Promise<void>;
}

interface EditorProps extends Props {
  settings: SettingRead[];
}

function buildLocalValues(settings: SettingRead[]): Record<string, unknown> {
  const initial: Record<string, unknown> = {};
  for (const setting of settings) {
    initial[setting.key] = setting.value;
  }
  return initial;
}

function buildSettingsKey(settings: SettingRead[]): string {
  return settings.map((setting) => `${setting.key}:${JSON.stringify(setting.value)}`).join('|');
}

export default function SettingsSection({ sections, onSave, onReset }: Props) {
  const settings = useMemo(() => sections.flatMap((section) => section.settings), [sections]);
  const settingsKey = useMemo(() => buildSettingsKey(settings), [settings]);

  return (
    <SettingsSectionEditor
      key={settingsKey}
      sections={sections}
      settings={settings}
      onSave={onSave}
      onReset={onReset}
    />
  );
}

function SettingsSectionEditor({ sections, settings, onSave, onReset }: EditorProps) {
  const [localValues, setLocalValues] = useState<Record<string, unknown>>(() => buildLocalValues(settings));
  const [saving, setSaving] = useState(false);

  function handleChange(key: string, value: unknown) {
    setLocalValues((prev) => ({ ...prev, [key]: value }));
  }

  function getChangedValues(): Record<string, unknown> {
    const changes: Record<string, unknown> = {};
    for (const s of settings) {
      const local = localValues[s.key];
      if (local !== undefined && JSON.stringify(local) !== JSON.stringify(s.value)) {
        changes[s.key] = local;
      }
    }
    return changes;
  }

  async function handleSave() {
    const changes = getChangedValues();
    if (Object.keys(changes).length === 0) {
      toast.info('No changes to save');
      return;
    }
    setSaving(true);
    try {
      await onSave(changes);
      toast.success('Settings saved');
    } catch {
      toast.error('Failed to save settings');
    } finally {
      setSaving(false);
    }
  }

  async function handleReset(key: string) {
    try {
      await onReset(key);
      toast.success('Setting reset to default');
    } catch {
      toast.error('Failed to reset setting');
    }
  }

  const hasChanges = Object.keys(getChangedValues()).length > 0;

  return (
    <div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {sections.map((section) => (
          <section key={section.id} className="rounded-lg border border-border bg-surface-1 p-5">
            <div className="mb-3">
              <h2 className="text-sm font-semibold text-text-1">{section.title}</h2>
              {section.description ? (
                <p className="mt-1 text-sm text-text-3">{section.description}</p>
              ) : null}
            </div>
            <div>
              {section.settings.map((setting) => (
                <SettingField
                  key={setting.key}
                  setting={setting}
                  value={localValues[setting.key] ?? setting.value}
                  onChange={(v) => handleChange(setting.key, v)}
                  onReset={() => handleReset(setting.key)}
                />
              ))}
            </div>
          </section>
        ))}
      </div>
      <div className="flex justify-end mt-4">
        <button
          onClick={handleSave}
          disabled={!hasChanges || saving}
          className="bg-accent text-accent-on px-4 py-2 rounded-md text-sm font-medium hover:bg-accent-hover disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save Changes'}
        </button>
      </div>
    </div>
  );
}
