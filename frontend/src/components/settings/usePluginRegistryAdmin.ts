import { type FormEvent, useState } from 'react';
import { toast } from 'sonner';
import { useCreatePlugin, useDeletePlugin, useSyncAllPlugins, useUpdatePlugin } from '../../hooks/usePlugins';
import type { AppiumPlugin, AppiumPluginCreate, AppiumPluginUpdate } from '../../types';

const EMPTY_PLUGIN_FORM: AppiumPluginCreate = {
  name: '',
  version: '',
  source: 'npm:',
  package: '',
  enabled: true,
  notes: '',
};

export function usePluginRegistryAdmin() {
  const createPluginMut = useCreatePlugin();
  const updatePluginMut = useUpdatePlugin();
  const deletePluginMut = useDeletePlugin();
  const syncAllPluginsMut = useSyncAllPlugins();
  const [showPluginModal, setShowPluginModal] = useState(false);
  const [editingPluginId, setEditingPluginId] = useState<string | null>(null);
  const [pluginForm, setPluginForm] = useState<AppiumPluginCreate>(EMPTY_PLUGIN_FORM);
  const [deletePluginTarget, setDeletePluginTarget] = useState<AppiumPlugin | null>(null);

  function openCreatePlugin() {
    setEditingPluginId(null);
    setPluginForm(EMPTY_PLUGIN_FORM);
    setShowPluginModal(true);
  }

  function openEditPlugin(plugin: AppiumPlugin) {
    setEditingPluginId(plugin.id);
    setPluginForm({
      name: plugin.name,
      version: plugin.version,
      source: plugin.source,
      package: plugin.package ?? '',
      enabled: plugin.enabled,
      notes: plugin.notes,
    });
    setShowPluginModal(true);
  }

  async function handlePluginSubmit(event: FormEvent) {
    event.preventDefault();
    const body = {
      ...pluginForm,
      package: pluginForm.package?.trim() ? pluginForm.package : null,
    };
    if (editingPluginId) {
      await updatePluginMut.mutateAsync({ id: editingPluginId, body: body as AppiumPluginUpdate });
    } else {
      await createPluginMut.mutateAsync(body);
    }
    setShowPluginModal(false);
  }

  async function handleSyncAll() {
    const result = await syncAllPluginsMut.mutateAsync();
    if (result.online_hosts.length === 0) {
      toast.error('No online hosts to sync');
      return;
    }

    const successes = result.synced_hosts.length;
    const failures = result.failed_hosts.length;
    if (failures === 0) {
      toast.success(`Plugins synced on ${successes} host(s)`);
      return;
    }

    toast.error(`Synced ${successes}, failed ${failures} host(s)`);
  }

  return {
    createPluginMut,
    deletePluginMut,
    deletePluginTarget,
    editingPluginId,
    handlePluginSubmit,
    handleSyncAll,
    openCreatePlugin,
    openEditPlugin,
    pluginForm,
    setDeletePluginTarget,
    setPluginForm,
    setShowPluginModal,
    showPluginModal,
    syncAllPluginsMut,
  };
}
