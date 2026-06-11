import { useEffect, useMemo, useRef, useState } from 'react';
import { useIntakeCandidates } from '../../hooks/useHosts';
import { filterIntakeCandidates } from './deviceVerificationWorkflow';
import type { ConnectionType, DeviceType, PlatformDescriptor } from '../../types';

const CANDIDATE_UPDATE_FLASH_MS = 3_500;

type Args = {
  hostId: string;
  activePlatformKey: string;
  activeDescriptor: PlatformDescriptor | null;
  activeDeviceType: DeviceType;
  activeConnectionType: ConnectionType;
};

export function useCandidateDiscovery({
  hostId,
  activePlatformKey,
  activeDescriptor,
  activeDeviceType,
  activeConnectionType,
}: Args) {
  const { data: candidates = [], isFetching: isFetchingCandidates = false } = useIntakeCandidates(hostId || null);
  const [selectedCandidateKey, setSelectedCandidateKey] = useState<string>('');
  const [candidateUpdate, setCandidateUpdate] = useState<{ context: string; signature: string } | null>(null);
  const previousCandidateSignatureRef = useRef<string | null>(null);
  const previousCandidateContextRef = useRef<string | null>(null);

  const filteredCandidates = useMemo(
    () => filterIntakeCandidates(candidates, activeDescriptor, activeDeviceType, activeConnectionType),
    [candidates, activeDescriptor, activeDeviceType, activeConnectionType],
  );
  const candidateSignature = useMemo(
    () => filteredCandidates
      .map((candidate) => `${candidate.identity_value}:${candidate.connection_target ?? ''}:${candidate.already_registered}`)
      .toSorted()
      .join('|'),
    [filteredCandidates],
  );
  const candidateContext = `${hostId}:${activePlatformKey}:${activeDeviceType}:${activeConnectionType}`;
  const selectedCandidate = filteredCandidates.find(
    (candidate) => `${candidate.identity_value}:${candidate.connection_target ?? ''}` === selectedCandidateKey,
  );
  const observedDeviceCountLabel = `${filteredCandidates.length} ${filteredCandidates.length === 1 ? 'device' : 'devices'} observed`;
  const showCandidateUpdate =
    candidateUpdate?.context === candidateContext && candidateUpdate.signature === candidateSignature;

  useEffect(() => {
    if (!hostId) {
      previousCandidateContextRef.current = null;
      previousCandidateSignatureRef.current = null;
      return;
    }

    if (previousCandidateContextRef.current !== candidateContext) {
      previousCandidateContextRef.current = candidateContext;
      previousCandidateSignatureRef.current = candidateSignature;
      return;
    }

    if (
      previousCandidateSignatureRef.current !== null &&
      previousCandidateSignatureRef.current !== candidateSignature
    ) {
      previousCandidateSignatureRef.current = candidateSignature;
      const showTimeout = window.setTimeout(() => {
        setCandidateUpdate({ context: candidateContext, signature: candidateSignature });
      }, 0);
      const hideTimeout = window.setTimeout(() => setCandidateUpdate(null), CANDIDATE_UPDATE_FLASH_MS);
      return () => {
        window.clearTimeout(showTimeout);
        window.clearTimeout(hideTimeout);
      };
    }

    previousCandidateSignatureRef.current = candidateSignature;
  }, [candidateContext, candidateSignature, hostId]);

  return {
    filteredCandidates,
    isFetchingCandidates,
    selectedCandidate,
    selectedCandidateKey,
    setSelectedCandidateKey,
    clearSelection: () => setSelectedCandidateKey(''),
    showCandidateUpdate,
    observedDeviceCountLabel,
  };
}
