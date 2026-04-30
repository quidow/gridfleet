import DateInput from '../ui/DateInput';

export type Preset = '24h' | '7d' | '30d' | 'custom';

interface Props {
  dateFrom: string;
  dateTo: string;
  activePreset: Preset;
  onChange: (from: string, to: string, preset: Preset) => void;
}

function getPresetRange(preset: '24h' | '7d' | '30d'): [string, string] {
  const now = new Date();
  const to = now.toISOString();
  const from = new Date(now);
  if (preset === '24h') from.setDate(from.getDate() - 1);
  else if (preset === '7d') from.setDate(from.getDate() - 7);
  else from.setDate(from.getDate() - 30);
  return [from.toISOString(), to];
}

export default function DateRangePicker({ dateFrom, dateTo, activePreset, onChange }: Props) {
  const presets: { key: Preset; label: string }[] = [
    { key: '24h', label: 'Last 24h' },
    { key: '7d', label: 'Last 7 days' },
    { key: '30d', label: 'Last 30 days' },
    { key: 'custom', label: 'Custom' },
  ];

  return (
    <div className="flex flex-wrap items-center gap-3">
      {presets.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          onClick={() => {
            if (key === 'custom') {
              onChange(dateFrom, dateTo, 'custom');
              return;
            }

            const [f, t] = getPresetRange(key);
            onChange(f, t, key);
          }}
          className={`px-3 py-1.5 text-sm rounded-md border transition-colors ${
            activePreset === key
              ? 'bg-accent text-accent-on border-accent'
              : 'bg-surface-1 text-text-2 border-border-strong hover:bg-surface-2'
          }`}
        >
          {label}
        </button>
      ))}

      {activePreset === 'custom' && (
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-2">
            <label className="text-sm text-text-3" htmlFor="drp-date-from">
              From
            </label>
            <DateInput
              id="drp-date-from"
              ariaLabel="Analytics date from"
              value={dateFrom}
              onChange={(nextValue) => {
                if (!nextValue) {
                  onChange('', dateTo, 'custom');
                  return;
                }
                onChange(new Date(`${nextValue}T00:00:00`).toISOString(), dateTo, 'custom');
              }}
            />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-sm text-text-3" htmlFor="drp-date-to">
              To
            </label>
            <DateInput
              id="drp-date-to"
              ariaLabel="Analytics date to"
              value={dateTo}
              onChange={(nextValue) => {
                if (!nextValue) {
                  onChange(dateFrom, '', 'custom');
                  return;
                }
                const d = new Date(`${nextValue}T23:59:59.999`);
                onChange(dateFrom, d.toISOString(), 'custom');
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}
