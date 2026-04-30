import { RefreshCw } from 'lucide-react';
import { useHostPlugins, useSyncHostPlugins } from '../../hooks/usePlugins';
import { DataTable } from '../ui';
import type { DataTableColumn } from '../ui';
import type { HostPluginStatus } from '../../types';

const PLUGIN_COLUMNS: DataTableColumn<HostPluginStatus>[] = [
  {
    key: 'name',
    header: 'Plugin',
    render: (p) => <span className="text-sm font-medium text-text-1">{p.name}</span>,
  },
  {
    key: 'required_version',
    header: 'Required',
    render: (p) => <span className="font-mono text-sm text-text-2">{p.required_version}</span>,
  },
  {
    key: 'installed_version',
    header: 'Installed',
    render: (p) => <span className="font-mono text-sm text-text-2">{p.installed_version ?? '-'}</span>,
  },
  {
    key: 'status',
    header: 'Status',
    render: (p) => (
      <span
        className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${
          p.status === 'ok'
            ? 'bg-success-soft text-success-foreground'
            : p.status === 'mismatch'
              ? 'bg-warning-soft text-warning-foreground'
              : 'bg-danger-soft text-danger-foreground'
        }`}
      >
        {p.status === 'ok' ? 'OK' : p.status === 'mismatch' ? 'Mismatch' : 'Missing'}
      </span>
    ),
  },
];

type Props = {
  hostId: string;
};

export default function HostPluginsPanel({ hostId }: Props) {
  const { data: hostPlugins, isLoading: pluginsLoading } = useHostPlugins(hostId);
  const syncPluginsMut = useSyncHostPlugins();

  return (
    <div className="rounded-lg border border-border bg-surface-1">
      <div className="flex items-center justify-between border-b border-border px-5 py-4">
        <h2 className="text-sm font-medium text-text-2">Appium Plugins</h2>
        <button
          onClick={() => syncPluginsMut.mutate(hostId)}
          disabled={syncPluginsMut.isPending}
          className="inline-flex items-center gap-1.5 rounded-md border border-accent/30 bg-accent-soft px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent-soft disabled:opacity-50"
        >
          <RefreshCw size={12} className={syncPluginsMut.isPending ? 'animate-spin' : ''} />
          {syncPluginsMut.isPending ? 'Syncing...' : 'Sync Plugins'}
        </button>
      </div>
      <DataTable<HostPluginStatus>
        columns={PLUGIN_COLUMNS}
        rows={hostPlugins ?? []}
        rowKey={(p) => p.name}
        loading={pluginsLoading}
        emptyState={
          <p className="px-5 py-8 text-center text-sm text-text-3">
            No plugins configured. Add plugins in Settings.
          </p>
        }
      />
    </div>
  );
}
