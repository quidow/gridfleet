import Card from '../../components/ui/Card';
import { Badge, DefinitionList } from '../../components/ui';
import type { DriverPackPlatform } from '../../types/driverPacks';
import { objectEntries, scalarValue } from './driverDetailFormat';

function listValue(values: string[]): string {
  return values.length > 0 ? values.join(', ') : 'None';
}

function PlatformCard({ platform }: { platform: DriverPackPlatform }) {
  const sessionRequired = Array.isArray(platform.capabilities?.session_required)
    ? (platform.capabilities.session_required as string[])
    : [];
  const defaultCapabilities = objectEntries(platform.default_capabilities);
  const connectionBehavior = objectEntries(platform.connection_behavior);
  const ports = platform.parallel_resources?.ports ?? [];
  const items = [
    { term: 'Automation', definition: platform.automation_name },
    { term: 'Appium Platform', definition: platform.appium_platform_name },
    { term: 'Device Types', definition: listValue(platform.device_types) },
    { term: 'Connection Types', definition: listValue(platform.connection_types) },
    { term: 'Grid Slots', definition: listValue(platform.grid_slots) },
    { term: 'Identity', definition: `${platform.identity_scheme} (${platform.identity_scope})` },
  ];

  return (
    <Card padding="md">
      <div className="mb-3 flex items-start justify-between gap-3">
        <h3 className="font-semibold text-text-1">{platform.display_name}</h3>
        <span className="break-all text-right text-xs text-text-3">{platform.id}</span>
      </div>

      <DefinitionList items={items} />

      {platform.lifecycle_actions && platform.lifecycle_actions.length > 0 && (
        <div className="mt-3">
          <span className="text-xs font-medium text-text-3">Lifecycle Actions</span>
          <div className="mt-1 flex flex-wrap gap-1">
            {platform.lifecycle_actions.map((action) => (
              <Badge key={action.id} tone="neutral">
                {action.label ?? action.id}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {platform.health_checks && platform.health_checks.length > 0 && (
        <div className="mt-3">
          <span className="text-xs font-medium text-text-3">Health Checks</span>
          <div className="mt-1 flex flex-wrap gap-1">
            {platform.health_checks.map((check) => (
              <Badge key={check.id} tone="neutral">
                {check.label}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {platform.device_fields_schema.length > 0 && (
        <div className="mt-3">
          <span className="text-xs font-medium text-text-3">Device Fields</span>
          <div className="mt-1 flex flex-wrap gap-1">
            {platform.device_fields_schema.map((field) => (
              <Badge key={field.id} tone={field.required_for_session ? 'warning' : 'neutral'}>
                {field.label}
                {field.required_for_session ? ' *' : ''}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {sessionRequired.length > 0 && (
        <div className="mt-3">
          <span className="text-xs font-medium text-text-3">Session Required Capabilities</span>
          <div className="mt-1 flex flex-wrap gap-1">
            {sessionRequired.map((capability) => (
              <Badge key={capability} tone="neutral">
                {capability}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {defaultCapabilities.length > 0 && (
        <div className="mt-3">
          <span className="text-xs font-medium text-text-3">Default Capabilities</span>
          <div className="mt-1 flex flex-wrap gap-1">
            {defaultCapabilities.map(([key, value]) => (
              <Badge key={key} tone="neutral">
                {key}: {scalarValue(value)}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {connectionBehavior.length > 0 && (
        <div className="mt-3">
          <span className="text-xs font-medium text-text-3">Connection Behavior</span>
          <DefinitionList
            className="mt-1"
            items={connectionBehavior.map(([key, value]) => ({
              term: key,
              definition: scalarValue(value),
              dense: true,
            }))}
          />
        </div>
      )}

      {(ports.length > 0 || platform.parallel_resources?.derived_data_path) && (
        <div className="mt-3">
          <span className="text-xs font-medium text-text-3">Parallel Resources</span>
          <div className="mt-1 flex flex-wrap gap-1">
            {ports.map((port) => (
              <Badge key={port.capability_name} tone="neutral">
                {port.capability_name}: {port.start}
              </Badge>
            ))}
            {platform.parallel_resources?.derived_data_path && <Badge tone="neutral">derived data path</Badge>}
          </div>
        </div>
      )}
    </Card>
  );
}

export default function DriverPlatformCards({ platforms }: { platforms: DriverPackPlatform[] }) {
  if (platforms.length === 0) {
    return <p className="py-4 text-center text-text-3">No platforms defined.</p>;
  }

  return (
    <div className="grid gap-4 md:grid-cols-2">
      {platforms.map((platform) => (
        <PlatformCard key={platform.id} platform={platform} />
      ))}
    </div>
  );
}
