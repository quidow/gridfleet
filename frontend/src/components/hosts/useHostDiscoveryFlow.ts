import { useState } from 'react';
import { useConfirmDiscovery, useDiscoverDevices } from '../../hooks/useHosts';
import type { DeviceRead, DiscoveryResult } from '../../types';

function toggleSetValue(current: Set<string>, value: string): Set<string> {
  const next = new Set(current);
  if (next.has(value)) {
    next.delete(value);
  } else {
    next.add(value);
  }
  return next;
}

export function useHostDiscoveryFlow(hostId: string | null) {
  const discoverMut = useDiscoverDevices();
  const confirmMut = useConfirmDiscovery();
  const [discoveryResult, setDiscoveryResult] = useState<DiscoveryResult | null>(null);
  const [verifyDevice, setVerifyDevice] = useState<DeviceRead | null>(null);
  const [selectedAddIdentities, setSelectedAddIdentities] = useState<Set<string>>(new Set());
  const [selectedRemoveIdentities, setSelectedRemoveIdentities] = useState<Set<string>>(new Set());

  async function handleDiscover(targetHostId: string | null = hostId) {
    if (!targetHostId) {
      return;
    }
    const result = await discoverMut.mutateAsync(targetHostId);
    setDiscoveryResult(result);
    setSelectedAddIdentities(new Set(result.new_devices.map((device) => device.identity_value)));
    setSelectedRemoveIdentities(new Set(result.removed_identity_values));
  }

  function closeDiscovery() {
    setDiscoveryResult(null);
    setSelectedAddIdentities(new Set());
    setSelectedRemoveIdentities(new Set());
  }

  function toggleAdd(identityValue: string) {
    setSelectedAddIdentities((current) => toggleSetValue(current, identityValue));
  }

  function toggleRemove(identityValue: string) {
    setSelectedRemoveIdentities((current) => toggleSetValue(current, identityValue));
  }

  async function handleConfirm() {
    if (!hostId || !discoveryResult) {
      return;
    }
    await confirmMut.mutateAsync({
      hostId,
      body: {
        add_identity_values: [...selectedAddIdentities],
        remove_identity_values: [...selectedRemoveIdentities],
      },
    });
    closeDiscovery();
  }

  async function handleImportAndVerify(identityValue: string) {
    if (!hostId) {
      return;
    }
    const result = await confirmMut.mutateAsync({
      hostId,
      body: { add_identity_values: [identityValue], remove_identity_values: [] },
    });
    const addedDevice = result.added_devices[0];
    closeDiscovery();
    if (addedDevice) {
      setVerifyDevice(addedDevice);
    }
  }

  return {
    closeDiscovery,
    confirmMut,
    discoverMut,
    discoveryResult,
    handleConfirm,
    handleDiscover,
    handleImportAndVerify,
    selectedAddIdentities,
    selectedRemoveIdentities,
    setSelectedAddIdentities,
    setSelectedRemoveIdentities,
    setVerifyDevice,
    toggleAdd,
    toggleRemove,
    verifyDevice,
  };
}
