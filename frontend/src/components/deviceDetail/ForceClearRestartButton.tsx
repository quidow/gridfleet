import { RotateCcw } from 'lucide-react';
import { useClearAppiumNodeTransition } from '../../hooks/useDevices';
import Button from '../ui/Button';

type Props = {
  nodeId: string;
  transitionToken: string | null | undefined;
};

export default function ForceClearRestartButton({ nodeId, transitionToken }: Props) {
  const mutation = useClearAppiumNodeTransition();

  if (!transitionToken) {
    return null;
  }

  return (
    <Button
      size="sm"
      variant="secondary"
      leadingIcon={<RotateCcw className="h-4 w-4" aria-hidden="true" />}
      onClick={() => mutation.mutate({ nodeId })}
      loading={mutation.isPending}
      title="Clear stuck restart lease"
    >
      Force-clear restart
    </Button>
  );
}
