import { AlertTriangle, Check, HelpCircle, Search, X } from 'lucide-react';
import type { HostRead } from '../../types';

type AgentVersionProps = {
  version: string | null;
  status: 'disabled' | 'ok' | 'outdated' | 'unknown';
  requiredVersion: string | null;
  recommendedVersion?: string | null;
  updateAvailable?: boolean;
};

type HostActionButtonsProps = {
  status: HostRead['status'];
  onApprove: () => void;
  onReject: () => void;
  onDiscover: () => void;
  approvePending?: boolean;
  rejectPending?: boolean;
  discoverPending?: boolean;
  variant?: 'table' | 'detail';
};

export function HostAgentVersionIndicator({ version, status, requiredVersion, recommendedVersion, updateAvailable }: AgentVersionProps) {
  if (status === 'outdated') {
    return (
      <div className="flex items-center gap-2">
        <span className="font-mono text-text-3">{version ?? '-'}</span>
        <span className="inline-flex items-center gap-1 rounded-full bg-warning-soft px-2 py-0.5 text-xs font-medium text-warning-foreground">
          <AlertTriangle size={12} />
          Outdated
        </span>
      </div>
    );
  }

  if (status === 'unknown') {
    return (
      <div className="flex items-center gap-2">
        <span className="font-mono text-text-3">{version ?? '-'}</span>
        <span
          className="inline-flex items-center gap-1 rounded-full bg-neutral-soft px-2 py-0.5 text-xs font-medium text-neutral-foreground"
          title={requiredVersion ? `Minimum supported version is ${requiredVersion}` : undefined}
        >
          <HelpCircle size={12} />
          Unknown
        </span>
      </div>
    );
  }

  if (updateAvailable) {
    return (
      <div className="flex items-center gap-2">
        <span className="font-mono text-text-3">{version ?? '-'}</span>
        <span
          className="inline-flex items-center gap-1 rounded-full bg-neutral-soft px-2 py-0.5 text-xs font-medium text-neutral-foreground"
          title={`Recommended version is ${recommendedVersion}`}
        >
          <AlertTriangle size={12} />
          Update available
        </span>
      </div>
    );
  }

  return <span className="font-mono text-text-3">{version ?? '-'}</span>;
}

export function HostAgentVersionNotice({ version, status, requiredVersion, recommendedVersion, updateAvailable }: AgentVersionProps) {
  if (status === 'outdated') {
    return (
      <div className="mt-4 rounded-md border border-warning-strong/30 bg-warning-soft p-3 text-sm text-warning-foreground">
        <div className="flex items-center gap-2 font-medium">
          <AlertTriangle size={16} />
          Agent update recommended
        </div>
        <p className="mt-1 text-warning-foreground">
          This host is running {version ?? 'an unknown version'}, below the configured minimum of {requiredVersion ?? '-'}.
        </p>
      </div>
    );
  }

  if (status === 'unknown') {
    return (
      <div className="mt-4 rounded-md border border-border bg-neutral-soft p-3 text-sm text-neutral-foreground">
        <div className="flex items-center gap-2 font-medium">
          <HelpCircle size={16} />
          Agent version could not be verified
        </div>
        <p className="mt-1 text-neutral-foreground">
          The manager requires at least {requiredVersion ?? '-'}, but this host did not report a parseable version string.
        </p>
      </div>
    );
  }

  if (updateAvailable) {
    return (
      <div className="mt-4 rounded-md border border-border bg-neutral-soft p-3 text-sm text-neutral-foreground">
        <div className="flex items-center gap-2 font-medium">
          <AlertTriangle size={16} />
          Agent update available
        </div>
        <p className="mt-1 text-neutral-foreground">
          This host is running {version ?? 'an unknown version'}, below the recommended version of {recommendedVersion ?? '-'}.
        </p>
      </div>
    );
  }

  return null;
}

export function HostActionButtons({
  status,
  onApprove,
  onReject,
  onDiscover,
  approvePending = false,
  rejectPending = false,
  discoverPending = false,
  variant = 'table',
}: HostActionButtonsProps) {
  if (status === 'pending') {
    if (variant === 'detail') {
      return (
        <>
          <button
            onClick={onApprove}
            disabled={approvePending}
            className="inline-flex items-center gap-2 rounded-md border border-success-strong/30 bg-success-soft px-4 py-2 text-sm font-medium text-success-foreground hover:bg-success-soft disabled:opacity-50"
          >
            <Check size={16} />
            {approvePending ? 'Approving...' : 'Approve Host'}
          </button>
          <button
            onClick={onReject}
            disabled={rejectPending}
            className="inline-flex items-center gap-2 rounded-md border border-danger-strong/30 bg-danger-soft px-4 py-2 text-sm font-medium text-danger-foreground hover:bg-danger-soft disabled:opacity-50"
          >
            <X size={16} />
            {rejectPending ? 'Rejecting...' : 'Reject Host'}
          </button>
        </>
      );
    }

    return (
      <>
        <button
          onClick={onApprove}
          disabled={approvePending}
          className="rounded p-1.5 text-text-3 hover:text-success-foreground"
          title="Approve Host"
        >
          <Check size={16} />
        </button>
        <button
          onClick={onReject}
          disabled={rejectPending}
          className="rounded p-1.5 text-text-3 hover:text-danger-foreground"
          title="Reject Host"
        >
          <X size={16} />
        </button>
      </>
    );
  }

  if (variant === 'detail') {
    return (
      <button
        onClick={onDiscover}
        disabled={discoverPending}
        className="inline-flex items-center gap-2 rounded-md border border-accent/30 bg-accent-soft px-4 py-2 text-sm font-medium text-accent hover:bg-accent-soft disabled:opacity-50"
      >
        <Search size={16} />
        {discoverPending ? 'Discovering...' : 'Discover Devices'}
      </button>
    );
  }

  return (
    <button
      onClick={onDiscover}
      disabled={discoverPending}
      className="rounded p-1.5 text-text-3 hover:text-accent-hover"
      title="Discover Devices"
    >
      <Search size={16} />
    </button>
  );
}
