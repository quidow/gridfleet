import { Link } from 'react-router-dom';
import { Check } from 'lucide-react';

type Props = {
  runsHref?: string;
  devicesHref?: string;
};

export default function OperationsAllIdleStrip({ runsHref, devicesHref }: Props) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="grid grid-cols-1 gap-3 border-t border-border p-5 sm:grid-cols-2"
    >
      <div className="flex items-center justify-between gap-3 rounded-md border border-dashed border-border bg-surface-2 px-3 py-3">
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-success-soft text-success-foreground">
            <Check size={14} />
          </span>
          <div>
            <p className="text-sm font-medium text-text-1">No active runs</p>
            <p className="text-xs text-text-2">Fleet is idle right now.</p>
          </div>
        </div>
        {runsHref ? (
          <Link to={runsHref} className="text-xs font-medium text-accent hover:text-accent-hover">View runs</Link>
        ) : null}
      </div>
      <div className="flex items-center justify-between gap-3 rounded-md border border-dashed border-border bg-surface-2 px-3 py-3">
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-success-soft text-success-foreground">
            <Check size={14} />
          </span>
          <div>
            <p className="text-sm font-medium text-text-1">No busy devices</p>
            <p className="text-xs text-text-2">Every device is free for a session.</p>
          </div>
        </div>
        {devicesHref ? (
          <Link to={devicesHref} className="text-xs font-medium text-accent hover:text-accent-hover">View busy</Link>
        ) : null}
      </div>
    </div>
  );
}
