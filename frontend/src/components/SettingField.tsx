import { RotateCcw } from 'lucide-react';
import type { SettingRead } from '../types';

interface Props {
  setting: SettingRead;
  value: unknown;
  onChange: (value: unknown) => void;
  onReset: () => void;
}

export default function SettingField({ setting, value, onChange, onReset }: Props) {
  const hasError = validateField(setting, value);

  return (
    <div className="py-4 border-b border-border last:border-0">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <label className="text-sm font-medium text-text-1">
              {formatLabel(setting.key)}
            </label>
            {setting.is_overridden && (
              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-accent-soft text-accent">
                Modified
              </span>
            )}
          </div>
          <p className="text-xs text-text-3 mb-2">{setting.description}</p>
          {renderInput(setting, value, onChange)}
          {hasError && <p className="text-xs text-danger-foreground mt-1">{hasError}</p>}
          {setting.is_overridden && (
            <p className="text-xs text-text-3 mt-1">
              Default: {formatDefault(setting.default_value)}
            </p>
          )}
        </div>
        {setting.is_overridden && (
          <button
            onClick={onReset}
            className="mt-6 p-1.5 text-text-3 hover:text-accent-hover rounded hover:bg-surface-2"
            title="Reset to default"
          >
            <RotateCcw size={14} />
          </button>
        )}
      </div>
    </div>
  );
}

function formatLabel(key: string): string {
  // "general.heartbeat_interval_sec" -> "Heartbeat Interval Sec"
  const part = key.split('.').pop() || key;
  return part
    .split('_')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

function formatDefault(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function renderInput(setting: SettingRead, value: unknown, onChange: (v: unknown) => void) {
  switch (setting.type) {
    case 'int':
      return (
        <input
          type="number"
          name={setting.key}
          value={value as number}
          onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
          min={setting.validation?.min}
          max={setting.validation?.max}
          className="w-48 border border-border-strong rounded-md px-3 py-1.5 text-sm"
        />
      );
    case 'string':
      if (setting.validation?.allowed_values) {
        return (
          <select
            name={setting.key}
            value={value as string}
            onChange={(e) => onChange(e.target.value)}
            className="w-48 border border-border-strong rounded-md px-3 py-1.5 text-sm"
          >
            {setting.validation.allowed_values.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        );
      }
      return (
        <input
          type="text"
          name={setting.key}
          value={value as string}
          onChange={(e) => onChange(e.target.value)}
          className="w-80 border border-border-strong rounded-md px-3 py-1.5 text-sm"
        />
      );
    case 'bool':
      return (
        <button
          type="button"
          onClick={() => onChange(!value)}
          className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
            value ? 'bg-accent' : 'bg-border-strong'
          }`}
        >
          <span
            className={`inline-block h-3.5 w-3.5 rounded-full bg-surface-1 transition-transform ${
              value ? 'translate-x-4.5' : 'translate-x-0.5'
            }`}
          />
        </button>
      );
    case 'json':
      if (setting.validation?.item_allowed_values) {
        const selected = Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
        return (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {setting.validation.item_allowed_values.map((option) => (
              <label key={option} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={selected.includes(option)}
                  onChange={() => {
                    const nextValue = selected.includes(option)
                      ? selected.filter((entry) => entry !== option)
                      : [...selected, option];
                    onChange(nextValue);
                  }}
                  className="rounded border-border-strong"
                />
                <span className="font-mono text-xs text-text-2">{option}</span>
              </label>
            ))}
          </div>
        );
      }
      return (
        <textarea
          name={setting.key}
          value={typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
          onChange={(e) => {
            try {
              onChange(JSON.parse(e.target.value));
            } catch {
              onChange(e.target.value); // Keep raw string until valid JSON
            }
          }}
          rows={3}
          className="w-full max-w-lg border border-border-strong rounded-md px-3 py-1.5 text-sm font-mono"
        />
      );
    default:
      return null;
  }
}

function validateField(setting: SettingRead, value: unknown): string | null {
  if (setting.type === 'int') {
    if (typeof value !== 'number') return null;
    if (setting.validation?.min != null && value < setting.validation.min)
      return `Minimum value is ${setting.validation.min}`;
    if (setting.validation?.max != null && value > setting.validation.max)
      return `Maximum value is ${setting.validation.max}`;
  }
  if (setting.type === 'json' && typeof value === 'string') {
    try {
      JSON.parse(value);
    } catch {
      return 'Invalid JSON';
    }
  }
  if (setting.type === 'json' && setting.validation?.item_allowed_values && !Array.isArray(value)) {
    return 'Expected a list of event names';
  }
  return null;
}
