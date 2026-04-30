/* eslint-disable react-refresh/only-export-components -- intentional mixed module: exports component + helper functions */
import { Checkbox, Field, TextField } from '../../components/ui';
import type { PlatformDeviceField } from '../../types';

type DeviceConfigValue = string | number | boolean;
export type DeviceConfigDraft = Record<string, DeviceConfigValue>;

export function defaultsForDeviceFields(fields: PlatformDeviceField[]): DeviceConfigDraft {
  const defaults: DeviceConfigDraft = {};
  for (const field of fields) {
    if (field.id === 'no_os_version') continue;
    if (field.default !== undefined) {
      defaults[field.id] = field.default;
    }
  }
  return defaults;
}

export function setDeviceConfigField(
  current: DeviceConfigDraft,
  fieldId: string,
  value: DeviceConfigValue | '',
): DeviceConfigDraft {
  const next = { ...current };
  if (value === '') {
    delete next[fieldId];
  } else {
    next[fieldId] = value;
  }
  return next;
}

type Props = {
  fields: PlatformDeviceField[];
  value: DeviceConfigDraft;
  onChange: (next: DeviceConfigDraft) => void;
  idPrefix?: string;
};

export default function DeviceManifestFields({ fields, value, onChange, idPrefix = 'device-field' }: Props) {
  const visibleFields = fields.filter((field) => field.id !== 'no_os_version');
  if (visibleFields.length === 0) return null;

  return (
    <div className="space-y-4">
      {visibleFields.map((field) => {
        if (field.type === 'bool') {
          return (
            <Checkbox
              key={field.id}
              checked={Boolean(value[field.id] ?? field.default)}
              onChange={(checked) => onChange(setDeviceConfigField(value, field.id, checked))}
              label={field.label}
            />
          );
        }
        return (
          <Field key={field.id} label={field.label} htmlFor={`${idPrefix}-${field.id}`}>
            <TextField
              id={`${idPrefix}-${field.id}`}
              type={field.sensitive ? 'password' : 'text'}
              required={field.required_for_session || field.required_for_discovery}
              value={String(value[field.id] ?? '')}
              onChange={(nextValue) => onChange(setDeviceConfigField(value, field.id, nextValue))}
            />
          </Field>
        );
      })}
    </div>
  );
}
