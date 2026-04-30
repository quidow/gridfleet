import { useEffect, useState } from 'react';
import { RefreshCw, AlertTriangle } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  useEnsureHostTools,
  useHostToolEnsureJob,
  useHostToolStatus,
} from '../../hooks/useHosts';
import { useSettings } from '../../hooks/useSettings';
import { describeHostPrerequisite } from '../../lib/hostPrerequisites';
import type { HostRead } from '../../types';

function formatToolValue(value: string | null | undefined) {
  return value && value.trim() ? value : '-';
}

function isEnsureResultItem(value: unknown): value is { success?: boolean; action?: string; error?: string } {
  return !!value && typeof value === 'object';
}

function summarizeEnsureResult(result: unknown) {
  if (!result || typeof result !== 'object') {
    return '';
  }
  const entries = Object.entries(result as Record<string, unknown>).flatMap(([name, value]) => {
    return [[name, value]];
  }) as [string, { success?: boolean; action?: string; error?: string } | boolean][];
  const failed = entries.filter(([, value]) => isEnsureResultItem(value) && value.success === false);
  if (failed.length > 0) {
    return failed.map(([name, value]) => `${name}: ${isEnsureResultItem(value) ? value.error ?? 'failed' : 'failed'}`).join('; ');
  }
  const changed = entries
    .filter(([, value]) => isEnsureResultItem(value) && value.action && value.action !== 'none' && value.action !== 'skipped')
    .map(([name, value]) => `${name}: ${isEnsureResultItem(value) ? value.action : ''}`);
  return changed.length > 0 ? changed.join('; ') : 'Versions already match.';
}

function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return fallback;
}

type Props = {
  host: HostRead;
};

export default function HostToolVersionsPanel({ host }: Props) {
  const hostId = host.id;
  const hostOnline = host.status === 'online';
  const missingPrerequisites = host.missing_prerequisites ?? [];
  const queryClient = useQueryClient();
  const { data: settingsGroups } = useSettings();
  const ensureToolsMut = useEnsureHostTools();
  const [ensureJobId, setEnsureJobId] = useState<string | null>(null);
  const { data: ensureJob } = useHostToolEnsureJob(hostId, ensureJobId);
  const { data: toolStatus, isLoading: toolsLoading, error: toolsError } = useHostToolStatus(hostId, hostOnline);

  const appiumTargetVersion = settingsGroups
    ?.flatMap((group) => group.settings)
    .find((setting) => setting.key === 'appium.target_version')?.value;
  const managedAppiumTarget =
    typeof appiumTargetVersion === 'string' && appiumTargetVersion.trim() ? appiumTargetVersion.trim() : null;

  useEffect(() => {
    if (!ensureJob) return;
    if (ensureJob.status === 'completed') {
      const summary = summarizeEnsureResult(ensureJob.result);
      toast.success(summary || 'Tool versions checked');
      void queryClient.invalidateQueries({ queryKey: ['host-tools-status', hostId] });
      void queryClient.invalidateQueries({ queryKey: ['host', hostId] });
    }
    if (ensureJob.status === 'failed') {
      toast.error(ensureJob.error || 'Tool ensure failed');
    }
  }, [ensureJob, hostId, queryClient]);

  async function handleEnsureTools() {
    try {
      const job = await ensureToolsMut.mutateAsync(hostId);
      setEnsureJobId(job.job_id);
      toast.success('Tool version check started');
    } catch (ensureError) {
      toast.error(getErrorMessage(ensureError, 'Failed to ensure tool versions'));
    }
  }

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-border bg-surface-1">
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <div>
            <h2 className="text-sm font-medium text-text-2">Tool Versions</h2>
            {missingPrerequisites.includes('appium') && managedAppiumTarget ? (
              <p className="mt-1 text-xs text-warning-foreground">
                Appium is missing and will be installed as {managedAppiumTarget}.
              </p>
            ) : null}
          </div>
          <button
            onClick={handleEnsureTools}
            disabled={
              !hostOnline ||
              ensureToolsMut.isPending ||
              ensureJob?.status === 'pending' ||
              ensureJob?.status === 'running'
            }
            className="inline-flex items-center gap-1.5 rounded-md border border-accent/30 bg-accent-soft px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent-soft disabled:opacity-50"
          >
            <RefreshCw
              size={12}
              className={
                ensureToolsMut.isPending || ensureJob?.status === 'pending' || ensureJob?.status === 'running'
                  ? 'animate-spin'
                  : ''
              }
            />
            {ensureToolsMut.isPending || ensureJob?.status === 'pending' || ensureJob?.status === 'running'
              ? 'Ensuring...'
              : 'Ensure Versions'}
          </button>
        </div>
        {!hostOnline ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Host must be online to read tool versions.</p>
        ) : toolsLoading ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Loading tool versions...</p>
        ) : toolsError || !toolStatus ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Tool versions are currently unavailable.</p>
        ) : (
          <div className="grid grid-cols-1 divide-y divide-border md:grid-cols-5 md:divide-x md:divide-y-0">
            {[
              ['Appium', toolStatus.appium],
              ['Node', toolStatus.node],
              ['Node Provider', toolStatus.node_provider ?? toolStatus.node_error],
              ['go-ios', toolStatus.go_ios],
              ['Selenium JAR', toolStatus.selenium_jar],
            ].map(([label, value]) => (
              <div key={label} className="px-5 py-4">
                <div className="text-xs font-medium uppercase text-text-3">{label}</div>
                <div className="mt-1 font-mono text-sm text-text-1">{formatToolValue(value)}</div>
              </div>
            ))}
            <div className="px-5 py-4 md:col-span-5 md:border-t md:border-border">
              <div className="text-xs font-medium uppercase text-text-3">Selenium JAR Path</div>
              <div className="mt-1 break-all font-mono text-sm text-text-2">{toolStatus.selenium_jar_path}</div>
            </div>
          </div>
        )}
        {ensureJob ? (
          <div className="border-t border-border px-5 py-3 text-sm text-text-2">
            {ensureJob.status === 'pending' || ensureJob.status === 'running'
              ? 'Tool version check is running.'
              : ensureJob.status === 'failed'
                ? ensureJob.error || 'Tool ensure failed.'
                : summarizeEnsureResult(ensureJob.result)}
          </div>
        ) : null}
      </div>

      {missingPrerequisites.length > 0 ? (
        <div className="rounded-lg border border-warning-strong/30 bg-warning-soft">
          <div className="flex items-center gap-2 border-b border-warning-strong/30 px-5 py-4">
            <AlertTriangle size={16} className="text-warning-foreground" />
            <h2 className="text-sm font-medium text-warning-foreground">Missing Prerequisites</h2>
          </div>
          <div className="divide-y divide-warning-strong/30">
            {missingPrerequisites.map((name) => (
              <div
                key={name}
                className="flex flex-col gap-1 px-5 py-3 text-sm sm:flex-row sm:items-center sm:justify-between"
              >
                <span className="font-mono font-medium text-warning-foreground">{name}</span>
                <span className="text-warning-foreground">{describeHostPrerequisite(name)}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
