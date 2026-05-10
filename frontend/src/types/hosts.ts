import type { components } from '../api/openapi';
import type { DeviceRead } from './devices';

type Schemas = components['schemas'];

export type HostRead = Schemas['HostRead'];
export type HostDetail = Omit<Schemas['HostDetail'], 'devices'> & {
  devices: DeviceRead[];
};
export type HostCircuitBreaker = Schemas['HostCircuitBreakerRead'];
export type HostDiagnosticsNode = Schemas['HostDiagnosticsNodeRead'];
export type HostAppiumProcesses = Schemas['HostAppiumProcessesRead'];
export type HostRecoveryEvent = Schemas['HostRecoveryEventRead'];
export type HostDiagnostics = Schemas['HostDiagnosticsRead'];
export type HostResourceSample = Schemas['HostResourceSampleRead'];
export type HostResourceTelemetry = Schemas['HostResourceTelemetryResponse'];
export type HostToolStatus = Schemas['HostToolStatusRead'];
export type ToolEnsureResultItem = Schemas['ToolEnsureResultItemRead'];
export type HostToolEnsureResult = Schemas['HostToolEnsureResultRead'];
export type HostToolEnsureJob = Schemas['HostToolEnsureJobRead'];
export type HostCreate = Schemas['HostCreate'];
export type DiscoveredDevice = Schemas['DiscoveredDevice'];
export type DiscoveryResult = Schemas['DiscoveryResult'];
export type DiscoveryConfirm = Schemas['DiscoveryConfirm'];
export type DiscoveryConfirmResult = Schemas['DiscoveryConfirmResult'];
export type IntakeCandidate = Schemas['IntakeCandidateRead'];
