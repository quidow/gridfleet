import { AlertCircle } from 'lucide-react';

interface FetchErrorProps {
  /** Human-readable description of what failed. Defaults to a generic message. */
  message?: string;
  /** Called when the user activates the Retry button. Pass the React Query `refetch` function. */
  onRetry: () => void;
  className?: string;
}

/**
 * Inline error banner shown in place of a content area when a fetch fails.
 * Always includes a Retry affordance so the user is never stuck on a blank page.
 */
export default function FetchError({ message = 'Something went wrong while loading this data.', onRetry, className = '' }: FetchErrorProps) {
  return (
    <div
      role="alert"
      className={[
        'flex items-start gap-3 rounded-md bg-danger-soft p-4 text-sm text-danger-foreground',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <AlertCircle size={16} className="mt-0.5 shrink-0 text-danger-strong" aria-hidden />
      <div className="flex-1 text-sm text-danger-foreground">
        <span>{message}</span>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="shrink-0 rounded text-sm font-medium text-danger-foreground underline underline-offset-2 hover:text-danger-foreground focus:outline-none focus:ring-2 focus:ring-accent"
      >
        Retry
      </button>
    </div>
  );
}
