import type { components } from '../api/openapi';

type Schemas = components['schemas'];

export type SessionSummaryRow = Schemas['SessionSummaryRow'];
export type DeviceUtilizationRow = Schemas['DeviceUtilizationRow'];
export type DeviceReliabilityRow = Schemas['DeviceReliabilityRow'];
export type FleetDeviceSummary = Schemas['FleetDeviceSummary'];
export type FleetOverview = Schemas['FleetOverview'];
export type FleetCapacityTimelinePoint = Schemas['FleetCapacityTimelinePoint'];
export type FleetCapacityTimeline = Schemas['FleetCapacityTimeline'];
