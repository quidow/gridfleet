import { Copy } from 'lucide-react';
import type { DeviceDetail } from '../../types';

type Props = {
  capabilities: Record<string, unknown> | null | undefined;
  device?: DeviceDetail;
};

const ROUTING_KEYS = ['platformName', 'appium:udid', 'appium:deviceName', 'appium:gridfleet:deviceId'];

function expectedUdid(device: DeviceDetail | undefined): string | null {
  if (!device) {
    return null;
  }
  return device.appium_node?.active_connection_target ?? device.connection_target ?? device.identity_value;
}

export default function DeviceCapabilitiesPanel({ capabilities, device }: Props) {
  if (!capabilities) {
    return null;
  }

  const expectedCapabilityTarget = expectedUdid(device);
  const actualCapabilityTarget = capabilities['appium:udid'];
  const showTargetWarning =
    typeof actualCapabilityTarget === 'string'
    && expectedCapabilityTarget !== null
    && actualCapabilityTarget !== expectedCapabilityTarget;

  return (
    <div className="p-5">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-text-1">Appium Capabilities</h2>
          <p className="mt-1 text-xs text-text-2">Effective capabilities after manager-owned routing fields are applied.</p>
        </div>
        <button
          type="button"
          onClick={() => navigator.clipboard.writeText(JSON.stringify(capabilities, null, 2))}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border bg-surface-2 px-2.5 py-1 text-xs font-medium text-text-2 hover:bg-surface-1"
        >
          <Copy size={12} />
          Copy
        </button>
      </div>

      <div className="mb-3 border-y border-border py-3">
        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-text-3">Managed routing</p>
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2 xl:grid-cols-4">
        {ROUTING_KEYS.map((key) => (
          <div key={key} className="min-w-0">
            <dt className="font-mono text-[11px] text-text-3">{key}</dt>
            <dd className="mt-1 truncate font-mono text-xs font-medium text-text-1">
              {String(capabilities[key] ?? '-')}
            </dd>
          </div>
        ))}
        </dl>
      </div>

      {showTargetWarning ? (
        <div className="mb-3 rounded-md border border-warning-strong/30 bg-warning-soft px-3 py-2 text-xs text-warning-foreground">
          Capability target differs from manager target: expected {expectedCapabilityTarget}, got {actualCapabilityTarget}.
        </div>
      ) : null}

      <pre className="max-h-48 overflow-auto rounded-md border border-border bg-surface-2 p-4 font-mono text-xs text-text-1">
        {JSON.stringify(capabilities, null, 2)}
      </pre>
    </div>
  );
}
