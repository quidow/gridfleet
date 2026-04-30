import { composeDeviceStatusNarrative, type StatusActionKind } from './deviceStatusNarrative';
import Card from '../../components/ui/Card';
import type { DeviceRead } from '../../types';

type Handlers = {
  onRetry: () => void;
  onMaintenance: () => void;
  onSetup: () => void;
  onVerify: () => void;
  onExitMaintenance: () => void;
};

type Props = { device: DeviceRead } & Handlers;

const HANDLER_BY_KIND: Record<StatusActionKind, keyof Handlers> = {
  retry: 'onRetry',
  maintenance: 'onMaintenance',
  setup: 'onSetup',
  verify: 'onVerify',
  'exit-maintenance': 'onExitMaintenance',
};

export default function DeviceStatusCard({ device, ...handlers }: Props) {
  const { text, actions } = composeDeviceStatusNarrative(device);
  return (
    <Card padding="lg">
      <h2 className="heading-section mb-2">Status &amp; recovery</h2>
      <p className="text-sm text-text-1">{text}</p>
      {actions.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {actions.map((action) => {
            const handler = handlers[HANDLER_BY_KIND[action.kind]];
            return (
              <button
                key={action.kind}
                type="button"
                onClick={handler}
                className="rounded-md border border-border bg-surface-2 px-3 py-1.5 text-xs font-medium text-text-1 hover:border-border-strong hover:bg-surface-1"
              >
                {action.label}
              </button>
            );
          })}
        </div>
      ) : null}
    </Card>
  );
}
