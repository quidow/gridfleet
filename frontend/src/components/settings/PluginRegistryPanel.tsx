import { Pencil, RefreshCw, Trash2 } from 'lucide-react';
import ConfirmDialog from '../ui/ConfirmDialog';
import LoadingSpinner from '../LoadingSpinner';
import Modal from '../ui/Modal';
import { usePlugins } from '../../hooks/usePlugins';
import { usePluginRegistryAdmin } from './usePluginRegistryAdmin';
import SettingsPanelLayout from './SettingsPanelLayout';

export default function PluginRegistryPanel() {
  const { data: plugins, isLoading } = usePlugins();
  const admin = usePluginRegistryAdmin();

  if (isLoading) {
    return <LoadingSpinner />;
  }

  return (
    <SettingsPanelLayout
      title="Plugin Registry"
      description="Appium plugins registered for use across all hosts."
      actions={
        <>
          <button
            onClick={() => admin.handleSyncAll()}
            disabled={admin.syncAllPluginsMut.isPending}
            className="inline-flex items-center gap-1.5 rounded-md border border-border-strong bg-surface-1 px-4 py-2 text-sm font-medium text-text-2 hover:bg-surface-2 disabled:opacity-50"
          >
            <RefreshCw size={14} className={admin.syncAllPluginsMut.isPending ? 'animate-spin' : ''} />
            {admin.syncAllPluginsMut.isPending ? 'Syncing...' : 'Sync All Hosts'}
          </button>
          <button
            onClick={admin.openCreatePlugin}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-on hover:bg-accent-hover"
          >
            Add Plugin
          </button>
        </>
      }
    >

      {!plugins?.length ? (
        <p className="py-12 text-center text-text-3">No plugins configured.</p>
      ) : (
        <div className="overflow-hidden rounded-lg bg-surface-1 shadow">
          <table className="min-w-full divide-y divide-border">
            <thead className="bg-surface-2">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-text-3">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-text-3">Version</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-text-3">Source</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-text-3">Enabled</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-text-3">Notes</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-text-3">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {plugins.map((plugin) => (
                <tr key={plugin.id} className="hover:bg-surface-2">
                  <td className="px-4 py-3 text-sm font-medium text-text-1">{plugin.name}</td>
                  <td className="px-4 py-3 font-mono text-sm text-text-2">{plugin.version}</td>
                  <td className="px-4 py-3 text-sm text-text-3">{plugin.source}</td>
                  <td className="px-4 py-3 text-sm text-text-2">{plugin.enabled ? 'Yes' : 'No'}</td>
                  <td className="max-w-xs truncate px-4 py-3 text-sm text-text-3" title={plugin.notes}>
                    {plugin.notes || '-'}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <button onClick={() => admin.openEditPlugin(plugin)} className="text-text-3 hover:text-accent-hover" title="Edit">
                        <Pencil size={16} />
                      </button>
                      <button onClick={() => admin.setDeletePluginTarget(plugin)} className="text-text-3 hover:text-danger-foreground" title="Delete">
                        <Trash2 size={16} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Modal
        isOpen={admin.showPluginModal}
        onClose={() => admin.setShowPluginModal(false)}
        title={admin.editingPluginId ? 'Edit Plugin' : 'Add Plugin'}
      >
        <form onSubmit={admin.handlePluginSubmit} className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-text-2">Name</label>
            <input
              value={admin.pluginForm.name}
              onChange={(event) => admin.setPluginForm({ ...admin.pluginForm, name: event.target.value })}
              required
              className="w-full rounded-md border border-border-strong px-3 py-2 text-sm"
              placeholder="e.g. execute-driver"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="mb-1 block text-sm font-medium text-text-2">Version</label>
              <input
                value={admin.pluginForm.version}
                onChange={(event) => admin.setPluginForm({ ...admin.pluginForm, version: event.target.value })}
                required
                className="w-full rounded-md border border-border-strong px-3 py-2 text-sm"
                placeholder="e.g. 1.0.0"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium text-text-2">Source</label>
              <input
                value={admin.pluginForm.source}
                onChange={(event) => admin.setPluginForm({ ...admin.pluginForm, source: event.target.value })}
                required
                className="w-full rounded-md border border-border-strong px-3 py-2 text-sm"
                placeholder="npm:@appium/execute-driver-plugin"
              />
            </div>
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-text-2">Package</label>
            <input
              value={admin.pluginForm.package ?? ''}
              onChange={(event) => admin.setPluginForm({ ...admin.pluginForm, package: event.target.value })}
              className="w-full rounded-md border border-border-strong px-3 py-2 text-sm"
              placeholder="Required for some git/github sources"
            />
          </div>
          <label className="flex items-center gap-2 text-sm text-text-2">
            <input
              type="checkbox"
              checked={admin.pluginForm.enabled ?? true}
              onChange={(event) => admin.setPluginForm({ ...admin.pluginForm, enabled: event.target.checked })}
              className="rounded border-border-strong"
            />
            Install during plugin sync
          </label>
          <div>
            <label className="mb-1 block text-sm font-medium text-text-2">Notes</label>
            <input
              value={admin.pluginForm.notes ?? ''}
              onChange={(event) => admin.setPluginForm({ ...admin.pluginForm, notes: event.target.value })}
              className="w-full rounded-md border border-border-strong px-3 py-2 text-sm"
              placeholder="Optional notes"
            />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={() => admin.setShowPluginModal(false)} className="px-4 py-2 text-sm text-text-2 hover:text-text-1">
              Cancel
            </button>
            <button type="submit" className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-on hover:bg-accent-hover">
              {admin.editingPluginId ? 'Save' : 'Create'}
            </button>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        isOpen={!!admin.deletePluginTarget}
        title="Delete Plugin"
        message={`Are you sure you want to delete "${admin.deletePluginTarget?.name}"?`}
        variant="danger"
        confirmLabel="Delete"
        onConfirm={async () => {
          if (admin.deletePluginTarget) {
            await admin.deletePluginMut.mutateAsync(admin.deletePluginTarget.id);
          }
          admin.setDeletePluginTarget(null);
        }}
        onClose={() => admin.setDeletePluginTarget(null)}
      />
    </SettingsPanelLayout>
  );
}
