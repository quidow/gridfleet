// Single source of truth for react-query keys. Key literals must match the
// EVENT_QUERY_MAP roots in hooks/useEventStream.ts, which now also builds from here.
export const qk = {
  devices: {
    root: ['devices'] as const,
    list: (params: unknown) => ['devices', params] as const,
  },
  device: {
    root: ['device'] as const,
    detail: (id: string) => ['device', id] as const,
  },
  deviceHealth: {
    root: ['device-health'] as const,
    byDevice: (id: string) => ['device-health', id] as const,
  },
  deviceConfig: {
    root: ['device-config'] as const,
    byDevice: (id: string) => ['device-config', id] as const,
  },
  configHistory: {
    root: ['config-history'] as const,
    byDevice: (id: string) => ['config-history', id] as const,
  },
  deviceTestData: {
    root: ['device-test-data'] as const,
    byDevice: (id: string) => ['device-test-data', id] as const,
  },
  testDataHistory: {
    root: ['test-data-history'] as const,
    byDevice: (id: string) => ['test-data-history', id] as const,
  },
  deviceLogs: {
    byDevice: (id: string, lines: number) => ['device-logs', id, lines] as const,
  },
  deviceCapabilities: {
    root: ['device-capabilities'] as const,
    byDevice: (id: string) => ['device-capabilities', id] as const,
  },
  deviceSessionOutcomeHeatmap: {
    byDevice: (id: string, days: number) => ['device-session-outcome-heatmap', id, days] as const,
  },
  deviceGroups: { root: ['device-groups'] as const },
  deviceGroup: {
    root: ['device-group'] as const,
    detail: (id: string) => ['device-group', id] as const,
  },
  driverPackCatalog: { root: ['driver-pack-catalog'] as const },
  driverPack: {
    root: ['driver-pack'] as const,
    detail: (packId: string) => ['driver-pack', packId] as const,
  },
  driverPackReleases: {
    byPack: (packId: string) => ['driver-pack-releases', packId] as const,
  },
  driverPackHosts: {
    root: ['driver-pack-hosts'] as const,
    byPack: (packId: string) => ['driver-pack-hosts', packId] as const,
  },
  hostDriverPacks: {
    root: ['host-driver-packs'] as const,
    byHost: (hostId: string) => ['host-driver-packs', hostId] as const,
  },
  runs: {
    root: ['runs'] as const,
    cursorList: (params: unknown) => ['runs', 'cursor', params] as const,
  },
  run: {
    root: ['run'] as const,
    detail: (id: string) => ['run', id] as const,
  },
  sessions: {
    root: ['sessions'] as const,
    cursorList: (params: unknown) => ['sessions', 'cursor', params] as const,
  },
  gridQueue: { root: ['grid-queue'] as const },
  gridStatus: { root: ['grid-status'] as const },
  health: { root: ['health'] as const },
  lifecycleIncidents: {
    list: (params: unknown) => ['lifecycle', 'incidents', params] as const,
    recent: (params: unknown) => ['lifecycle', 'incidents', 'recent', params] as const,
  },
  webhooks: {
    root: ['webhooks'] as const,
    deliveries: (id: string, limit: number) => ['webhooks', id, 'deliveries', limit] as const,
  },
  analytics: {
    root: ['analytics'] as const,
    sessionsSummary: (params: unknown) => ['analytics', 'sessions-summary', params] as const,
    deviceUtilization: (params: unknown) => ['analytics', 'device-utilization', params] as const,
    deviceReliability: (params: unknown) => ['analytics', 'device-reliability', params] as const,
    fleetOverview: (params: unknown) => ['analytics', 'fleet-overview', params] as const,
    fleetCapacityTimeline: (params: unknown) => ['analytics', 'fleet-capacity-timeline', params] as const,
  },
  hosts: { root: ['hosts'] as const },
  gridRouter: {
    root: ['grid-router'] as const,
  },
  host: {
    root: ['host'] as const,
    detail: (id: string) => ['host', id] as const,
  },
  hostDiagnostics: { byHost: (id: string) => ['host-diagnostics', id] as const },
  hostResourceTelemetry: { byHost: (id: string) => ['host-resource-telemetry', id] as const },
  hostToolsStatus: { byHost: (id: string) => ['host-tools-status', id] as const },
  hostAgentLogs: { list: (hostId: string, filters: unknown) => ['host-agent-logs', hostId, filters] as const },
  hostEvents: { list: (hostId: string, filters: unknown) => ['host-events', hostId, filters] as const },
  intakeCandidates: {
    root: ['intake-candidates'] as const,
    byHost: (hostId: string | null) => ['intake-candidates', hostId] as const,
  },
  hostToolEnv: { byHost: (hostId: string) => ['host-tool-env', hostId] as const },
  notifications: {
    root: ['notifications'] as const,
    list: (params: unknown) => ['notifications', params] as const,
  },
  settings: { root: ['settings'] as const },
  plugins: { root: ['plugins'] as const },
  hostPlugins: {
    root: ['host-plugins'] as const,
    byHost: (hostId: string) => ['host-plugins', hostId] as const,
  },
  eventCatalog: { root: ['event-catalog'] as const },
} as const;
