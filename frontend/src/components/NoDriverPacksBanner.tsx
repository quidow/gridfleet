import { AlertTriangle } from 'lucide-react';
import { Link } from 'react-router-dom';

export function NoDriverPacksBanner({ packCount }: { packCount: number }) {
  if (packCount > 0) return null;

  return (
    <div
      role="alert"
      className="flex items-center gap-3 rounded-lg border border-warning-strong bg-warning-soft px-4 py-3"
    >
      <AlertTriangle size={18} className="shrink-0 text-warning-foreground" />
      <div className="flex-1 text-sm text-warning-foreground">
        <span className="font-medium">No driver packs installed.</span>{' '}
        Devices require a driver pack to discover, configure, and start Appium sessions.
      </div>
      <Link
        to="/drivers"
        className="shrink-0 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-on hover:bg-accent-hover"
      >
        Upload Driver Pack
      </Link>
    </div>
  );
}
