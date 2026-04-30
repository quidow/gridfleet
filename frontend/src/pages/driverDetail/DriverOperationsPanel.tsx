import Card from '../../components/ui/Card';
import { Badge } from '../../components/ui';
import type { DriverPack } from '../../types/driverPacks';

export default function DriverOperationsPanel({ pack }: { pack: DriverPack }) {
  const entries = Object.entries(pack.features ?? {});
  if (entries.length === 0) {
    return <p className="py-4 text-center text-text-3">No operations declared.</p>;
  }

  return (
    <div className="grid gap-3">
      {entries.map(([featureId, feature]) => (
        <Card key={featureId} padding="md">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-text-1">{feature.display_name}</h2>
            <Badge tone="neutral">{featureId}</Badge>
          </div>
          {feature.description_md && <p className="text-sm text-text-3">{feature.description_md}</p>}
          {feature.actions.length > 0 && (
            <div className="mt-3">
              <span className="text-xs font-medium text-text-3">Actions</span>
              <div className="mt-1 flex flex-wrap gap-1">
                {feature.actions.map((action) => (
                  <Badge key={action.id} tone="neutral">
                    {action.label}
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </Card>
      ))}
    </div>
  );
}
