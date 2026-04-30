import { Loader2 } from 'lucide-react';

export default function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center py-12" role="status" aria-live="polite">
      <Loader2 className="animate-spin text-text-3" size={32} />
      <span className="sr-only">Loading…</span>
    </div>
  );
}
