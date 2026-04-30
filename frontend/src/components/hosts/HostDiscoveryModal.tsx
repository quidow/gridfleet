import Modal from '../ui/Modal';
import { Button, Checkbox } from '../ui';
import ReadinessBadge from '../ReadinessBadge';
import { getDiscoveryImportActionLabel } from '../../lib/deviceWorkflow';
import { resolvePlatformLabel } from '../../lib/labels';
import { missingSetupFieldLabel } from '../readiness';
import type { DiscoveryResult } from '../../types';

interface HostDiscoveryModalProps {
  discoveryResult: DiscoveryResult | null;
  isPending: boolean;
  onClose: () => void;
  onConfirm: () => void;
  onImportAndVerify: (identityValue: string) => void;
  onToggleAdd: (identityValue: string) => void;
  onToggleRemove: (identityValue: string) => void;
  selectedAddIdentities: Set<string>;
  selectedRemoveIdentities: Set<string>;
  setSelectedAddIdentities: (next: Set<string>) => void;
  setSelectedRemoveIdentities: (next: Set<string>) => void;
}

export default function HostDiscoveryModal({
  discoveryResult,
  isPending,
  onClose,
  onConfirm,
  onImportAndVerify,
  onToggleAdd,
  onToggleRemove,
  selectedAddIdentities,
  selectedRemoveIdentities,
  setSelectedAddIdentities,
  setSelectedRemoveIdentities,
}: HostDiscoveryModalProps) {
  return (
    <Modal
      isOpen={!!discoveryResult}
      onClose={onClose}
      title="Discovery Results"
      footer={
        discoveryResult ? (
          <>
            <Button variant="secondary" size="sm" onClick={onClose}>
              Cancel
            </Button>
            {(selectedAddIdentities.size > 0 || selectedRemoveIdentities.size > 0) && (
              <Button size="sm" onClick={onConfirm} disabled={isPending}>
                {isPending ? 'Applying...' : `Apply (${selectedAddIdentities.size} add, ${selectedRemoveIdentities.size} remove)`}
              </Button>
            )}
          </>
        ) : undefined
      }
    >
      {discoveryResult && (
        <div className="space-y-5">
          {discoveryResult.new_devices.length > 0 && (
            <div>
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-text-2">
                  New Devices ({selectedAddIdentities.size}/{discoveryResult.new_devices.length})
                </h3>
                <div className="flex gap-2 text-xs">
                  <button
                    onClick={() => setSelectedAddIdentities(new Set(discoveryResult.new_devices.map((device) => device.identity_value)))}
                    className="text-accent hover:underline"
                  >
                    Select all
                  </button>
                  <button onClick={() => setSelectedAddIdentities(new Set())} className="text-accent hover:underline">
                    Select none
                  </button>
                </div>
              </div>
              <ul className="space-y-1.5">
                {discoveryResult.new_devices.map((device) => (
                  <li
                    key={device.identity_value}
                    onClick={() => onToggleAdd(device.identity_value)}
                    className={`flex items-start gap-3 text-sm rounded p-2.5 border cursor-pointer transition-colors ${
                      selectedAddIdentities.has(device.identity_value)
                        ? 'border-success-strong/30 bg-success-soft'
                        : 'border-border bg-surface-1 opacity-60'
                    }`}
                  >
                    <div onClick={(event) => event.stopPropagation()}>
                    <Checkbox
                      checked={selectedAddIdentities.has(device.identity_value)}
                      onChange={() => onToggleAdd(device.identity_value)}
                      label={<span className="sr-only">Select {device.name}</span>}
                      className="mt-0.5"
                    />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <div className="font-medium text-text-1">{device.name}</div>
                        <ReadinessBadge state={device.readiness_state} />
                      </div>
                      <div className="text-xs text-text-3 mt-0.5">
                        {resolvePlatformLabel(device.platform_id, device.platform_label)} {device.os_version}
                        {device.manufacturer && ` · ${device.manufacturer}`}
                        {device.model && ` · ${device.model}`}
                      </div>
                      {device.missing_setup_fields.length > 0 && (
                        <div className="text-xs text-warning-foreground mt-0.5">
                          Missing: {device.missing_setup_fields.map((f) => missingSetupFieldLabel(f)).join(', ')}
                        </div>
                      )}
                      <div className="text-xs text-text-3 font-mono mt-0.5 truncate">{device.identity_value}</div>
                      <div className="text-xs text-text-3 font-mono mt-0.5 truncate">{device.connection_target ?? '-'}</div>
                    </div>
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      onClick={(event) => {
                        event.stopPropagation();
                        onImportAndVerify(device.identity_value);
                      }}
                      className="shrink-0"
                    >
                      {getDiscoveryImportActionLabel(device.readiness_state)}
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {discoveryResult.updated_devices.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-text-2 mb-2">Updated Devices (auto-applied)</h3>
              <ul className="space-y-1.5">
                {discoveryResult.updated_devices.map((device) => (
                  <li key={device.identity_value} className="text-sm border border-accent/20 rounded p-2.5 bg-accent-soft">
                    <div className="font-medium text-text-1">{device.name}</div>
                    <div className="text-xs text-text-3 mt-0.5">
                      {resolvePlatformLabel(device.platform_id, device.platform_label)} {device.os_version}
                      {device.manufacturer && ` · ${device.manufacturer}`}
                    </div>
                    <div className="text-xs text-text-3 font-mono mt-0.5 truncate">{device.identity_value}</div>
                    <div className="text-xs text-text-3 font-mono mt-0.5 truncate">{device.connection_target ?? '-'}</div>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {discoveryResult.removed_identity_values.length > 0 && (
            <div>
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-text-2">
                  Removed Devices ({selectedRemoveIdentities.size}/{discoveryResult.removed_identity_values.length})
                </h3>
                <div className="flex gap-2 text-xs">
                  <button
                    onClick={() => setSelectedRemoveIdentities(new Set(discoveryResult.removed_identity_values))}
                    className="text-accent hover:underline"
                  >
                    Select all
                  </button>
                  <button onClick={() => setSelectedRemoveIdentities(new Set())} className="text-accent hover:underline">
                    Select none
                  </button>
                </div>
              </div>
              <ul className="space-y-1.5">
                {discoveryResult.removed_identity_values.map((identityValue) => (
                  <li
                    key={identityValue}
                    onClick={() => onToggleRemove(identityValue)}
                    className={`flex items-center gap-3 text-sm rounded p-2.5 border cursor-pointer transition-colors ${
                      selectedRemoveIdentities.has(identityValue)
                        ? 'border-danger-strong/30 bg-danger-soft'
                        : 'border-border bg-surface-1 opacity-60'
                    }`}
                  >
                    <div onClick={(event) => event.stopPropagation()}>
                    <Checkbox
                      checked={selectedRemoveIdentities.has(identityValue)}
                      onChange={() => onToggleRemove(identityValue)}
                      label={<span className="sr-only">Select removed device {identityValue}</span>}
                    />
                    </div>
                    <span className="text-text-2 font-mono text-xs truncate">{identityValue}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {discoveryResult.new_devices.length === 0 &&
            discoveryResult.removed_identity_values.length === 0 &&
            discoveryResult.updated_devices.length === 0 && <p className="text-sm text-text-3">No changes detected.</p>}
        </div>
      )}
    </Modal>
  );
}
