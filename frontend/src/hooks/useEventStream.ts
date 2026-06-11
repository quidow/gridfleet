import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient, type Query, type QueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { useAuth } from '../context/auth';
import { fetchSettings } from '../api/settings';
import { fetchEventCatalog } from '../api/events';
import type { SettingsGrouped } from '../types';
import { formatEventDetails } from '../components/notifications/eventRegistry';
import { qk } from '../lib/queryKeys';

const EVENT_QUERY_MAP: Record<string, ReadonlyArray<readonly string[]>> = {
  'device.operational_state_changed': [qk.devices.root, qk.device.root, qk.deviceCapabilities.root],
  'device.verification.updated': [qk.devices.root, qk.device.root],
  'node.state_changed': [qk.devices.root, qk.device.root, qk.deviceCapabilities.root],
  'node.crash': [qk.devices.root, qk.device.root],
  'device.health_changed': [qk.devices.root, qk.device.root, qk.deviceHealth.root, qk.health.root],
  'device.hardware_health_changed': [qk.devices.root, qk.device.root, qk.deviceHealth.root, qk.health.root, qk.deviceDiagnosticSnapshots.root],
  'device.crashed': [qk.devices.root, qk.device.root, qk.deviceHealth.root],
  'host.status_changed': [qk.hosts.root, qk.host.root, qk.devices.root, qk.health.root, qk.hostDriverPacks.root],
  'host.heartbeat_lost': [qk.hosts.root, qk.host.root, qk.devices.root, qk.health.root],
  'host.registered': [qk.hosts.root, qk.host.root, qk.health.root, qk.driverPackCatalog.root, qk.driverPackHosts.root, qk.hostDriverPacks.root],
  'host.discovery_completed': [qk.hosts.root, qk.host.root, qk.devices.root, qk.intakeCandidates.root, qk.deviceCapabilities.root, qk.driverPackCatalog.root, qk.driverPackHosts.root, qk.hostDriverPacks.root],
  'host.circuit_breaker.opened': [qk.hosts.root, qk.host.root, qk.health.root],
  'host.circuit_breaker.closed': [qk.hosts.root, qk.host.root, qk.health.root],
  'session.started': [qk.sessions.root, qk.gridQueue.root, qk.gridStatus.root, qk.devices.root, qk.device.root, qk.runs.root, qk.run.root],
  'session.ended': [qk.sessions.root, qk.gridQueue.root, qk.gridStatus.root, qk.devices.root, qk.device.root, qk.runs.root, qk.run.root],
  'run.created': [qk.runs.root, qk.run.root, qk.devices.root],
  'run.active': [qk.runs.root, qk.run.root, qk.devices.root],
  'run.completed': [qk.runs.root, qk.run.root, qk.devices.root, qk.sessions.root],
  'run.cancelled': [qk.runs.root, qk.run.root, qk.devices.root, qk.sessions.root],
  'run.expired': [qk.runs.root, qk.run.root, qk.devices.root, qk.sessions.root],
  'run.never_activated': [qk.runs.root, qk.run.root, qk.devices.root],
  'config.updated': [qk.deviceConfig.root, qk.configHistory.root, qk.device.root, qk.devices.root, qk.deviceCapabilities.root],
  'test_data.updated': [qk.deviceTestData.root, qk.testDataHistory.root, qk.device.root, qk.devices.root],
  'bulk.operation_completed': [qk.devices.root, qk.device.root, qk.deviceGroups.root, qk.deviceGroup.root],
  'device_group.updated': [qk.deviceGroups.root, qk.deviceGroup.root, qk.devices.root],
  'device_group.members_changed': [qk.deviceGroups.root, qk.deviceGroup.root, qk.devices.root],
  'settings.changed': [qk.settings.root],
  'system.cleanup_completed': [qk.sessions.root, qk.analytics.root],
  'pack_feature.degraded': [qk.driverPackCatalog.root, qk.driverPack.root, qk.driverPackHosts.root, qk.hostDriverPacks.root],
  'pack_feature.recovered': [qk.driverPackCatalog.root, qk.driverPack.root, qk.driverPackHosts.root, qk.hostDriverPacks.root],
  'webhook.test': [qk.webhooks.root],
};

type ToastResult = { type: 'success' | 'error' | 'warning' | 'info'; message: string } | null;
type ToastKind = NonNullable<ToastResult>['type'];
type ToastSeverity = 'info' | 'warning' | 'error';

const TOAST_EVENTS: Record<string, (data: Record<string, unknown>) => ToastResult> = {
  'device.operational_state_changed': (data) => {
    if (data.new_operational_state === 'offline')
      return { type: 'error', message: `${data.device_name} went offline` };
    if (data.new_operational_state === 'available' && data.old_operational_state === 'offline')
      return { type: 'success', message: `${data.device_name} is back online` };
    return null;
  },
  'node.crash': (data) => ({
    type: 'error',
    message: `Node crashed for ${data.device_name}: ${data.error}`,
  }),
  'host.heartbeat_lost': (data) => ({
    type: 'error',
    message: `Lost heartbeat for host ${data.hostname}`,
  }),
  'host.status_changed': (data) => {
    if (data.new_status === 'offline')
      return { type: 'error', message: `Host ${data.hostname} went offline` };
    if (data.new_status === 'online')
      return { type: 'success', message: `Host ${data.hostname} is back online` };
    return null;
  },
  'host.registered': (data) => ({
    type: 'success',
    message: `New host registered: ${data.hostname}`,
  }),
  'run.expired': (data) => ({
    type: 'error',
    message: `${data.name}: run expired${data.reason ? ` (${data.reason})` : ''}`,
  }),
  'bulk.operation_completed': (data) => ({
    type: (data.failed as number) > 0 ? 'warning' : 'success',
    message: `Bulk ${data.operation}: ${data.succeeded}/${data.total} succeeded`,
  }),
};

const INITIAL_RECONNECT_DELAY = 1_000;
const MAX_RECONNECT_DELAY = 30_000;
const HIGH_VOLUME_INVALIDATION_DELAY = 3_000;

const DEFAULT_TOAST_EVENTS = [
  'node.crash',
  'host.heartbeat_lost',
  'device.operational_state_changed',
  'run.expired',
];
const DEFAULT_TOAST_DISMISS_SEC = 5;
const DEFAULT_TOAST_THRESHOLD: ToastSeverity = 'warning';
type ToastConfig = {
  toastEvents: string[];
  dismissSec: number;
  toastThreshold: ToastSeverity;
};

function flattenSettings(groups: SettingsGrouped[] | undefined): Record<string, unknown> {
  const flattened: Record<string, unknown> = {};
  for (const group of groups ?? []) {
    for (const setting of group.settings) {
      flattened[setting.key] = setting.value;
    }
  }
  return flattened;
}

function toSeverity(type: ToastKind): ToastSeverity {
  if (type === 'error') return 'error';
  if (type === 'warning') return 'warning';
  return 'info';
}

function meetsSeverityThreshold(severity: ToastSeverity, threshold: ToastSeverity): boolean {
  const order: Record<ToastSeverity, number> = { info: 0, warning: 1, error: 2 };
  return order[severity] >= order[threshold];
}

function isHighVolumeEvent(eventType: string): boolean {
  return eventType.startsWith('session.') || eventType.startsWith('run.');
}

function isNewestCursorQuery(query: Query): boolean {
  if (query.queryKey[1] !== 'cursor') {
    return true;
  }
  const params = query.queryKey[2];
  return !params || typeof params !== 'object' || !('cursor' in params) || !params.cursor;
}

function invalidateQueryTarget(queryClient: QueryClient, key: readonly string[]) {
  if (key[0] === qk.sessions.root[0]) {
    void queryClient.invalidateQueries({
      predicate: (query) => query.queryKey[0] === qk.sessions.root[0] && isNewestCursorQuery(query),
    });
    return;
  }
  if (key[0] === qk.runs.root[0]) {
    void queryClient.invalidateQueries({
      predicate: (query) => query.queryKey[0] === qk.runs.root[0] && isNewestCursorQuery(query),
    });
    return;
  }
  void queryClient.invalidateQueries({ queryKey: key });
}

export function useEventStream() {
  const queryClient = useQueryClient();
  const auth = useAuth();
  const [connected, setConnected] = useState(false);
  const { data: eventCatalog } = useQuery({
    queryKey: qk.eventCatalog.root,
    queryFn: fetchEventCatalog,
    refetchInterval: false,
    staleTime: Infinity,
    throwOnError: false,
  });
  const { data: settingsGroups } = useQuery({
    queryKey: qk.settings.root,
    queryFn: fetchSettings,
    throwOnError: false,
    staleTime: Infinity,
    refetchInterval: false,
  });
  const eventTypes = useMemo(() => eventCatalog?.map((event) => event.name) ?? [], [eventCatalog]);
  const settings = useMemo(() => flattenSettings(settingsGroups), [settingsGroups]);
  const toastConfig = useMemo((): ToastConfig => {
    const toastEvents = Array.isArray(settings['notifications.toast_events'])
      ? settings['notifications.toast_events'].filter((value): value is string => typeof value === 'string')
      : DEFAULT_TOAST_EVENTS;
    const dismissSec = typeof settings['notifications.toast_auto_dismiss_sec'] === 'number'
      ? settings['notifications.toast_auto_dismiss_sec']
      : DEFAULT_TOAST_DISMISS_SEC;
    const thresholdSetting = settings['notifications.toast_severity_threshold'];
    const toastThreshold: ToastSeverity =
      thresholdSetting === 'info' || thresholdSetting === 'warning' || thresholdSetting === 'error'
        ? thresholdSetting
        : DEFAULT_TOAST_THRESHOLD;

    return { toastEvents, dismissSec, toastThreshold };
  }, [settings]);
  const toastConfigRef = useRef<ToastConfig>(toastConfig);
  const reconnectDelayRef = useRef(INITIAL_RECONNECT_DELAY);
  const pendingInvalidationsRef = useRef<Set<string>>(new Set());
  const invalidationTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    toastConfigRef.current = toastConfig;
  }, [toastConfig]);

  useEffect(() => {
    if (eventTypes.length === 0) {
      return;
    }

    const pendingInvalidations = pendingInvalidationsRef.current;
    let eventSource: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;

    function flushPendingInvalidations() {
      const pending = Array.from(pendingInvalidationsRef.current);
      pendingInvalidationsRef.current.clear();
      invalidationTimerRef.current = null;

      for (const serializedKey of pending) {
        invalidateQueryTarget(queryClient, JSON.parse(serializedKey) as string[]);
      }
    }

    function scheduleHighVolumeInvalidation(queryKeys: ReadonlyArray<readonly string[]>) {
      for (const key of queryKeys) {
        pendingInvalidationsRef.current.add(JSON.stringify(key));
      }
      pendingInvalidationsRef.current.add(JSON.stringify(qk.notifications.root));

      if (invalidationTimerRef.current) {
        return;
      }
      invalidationTimerRef.current = setTimeout(flushPendingInvalidations, HIGH_VOLUME_INVALIDATION_DELAY);
    }

    function connect() {
      if (disposed) {
        return;
      }
      if (eventSource) {
        eventSource.close();
      }

      const es = new EventSource('/api/events');
      eventSource = es;

      es.onopen = () => {
        reconnectDelayRef.current = INITIAL_RECONNECT_DELAY;
        setConnected(true);
      };

      es.onerror = () => {
        if (disposed) {
          return;
        }
        setConnected(false);
        es.close();
        eventSource = null;
        void (async () => {
          const authSession = await auth.probeSession();
          if (disposed || (authSession.enabled && !authSession.authenticated)) {
            return;
          }

          const delay = reconnectDelayRef.current;
          reconnectTimer = setTimeout(connect, delay);
          reconnectDelayRef.current = Math.min(delay * 2, MAX_RECONNECT_DELAY);
        })();
      };

      for (const eventType of eventTypes) {
        es.addEventListener(eventType, (e: MessageEvent) => {
          let parsed: { data: Record<string, unknown> };
          try {
            parsed = JSON.parse(e.data);
          } catch {
            return;
          }

          const queryKeys = EVENT_QUERY_MAP[eventType] || [];
          if (isHighVolumeEvent(eventType)) {
            scheduleHighVolumeInvalidation(queryKeys);
          } else {
            for (const key of queryKeys) {
              invalidateQueryTarget(queryClient, key);
            }
            void queryClient.invalidateQueries({ queryKey: qk.notifications.root });
          }

          const toastFn = TOAST_EVENTS[eventType];
          const formatted = formatEventDetails(eventType, parsed.data);
          const fallbackMessage = formatted.kind === 'text' ? formatted.text : JSON.stringify(parsed.data);
          const result = toastFn
            ? toastFn(parsed.data)
            : { type: 'info' as const, message: fallbackMessage };
          if (result) {
            const { toastEvents, dismissSec, toastThreshold } = toastConfigRef.current;
            const severity = toSeverity(result.type);
            if (!toastEvents.includes(eventType) || !meetsSeverityThreshold(severity, toastThreshold)) {
              return;
            }

            const duration = result.type === 'error'
              ? Infinity
              : (dismissSec <= 0 ? Infinity : dismissSec * 1000);
            toast[result.type](result.message, {
              duration,
            });
          }
        });
      }
    }

    connect();

    return () => {
      disposed = true;
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      setConnected(false);
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
      }
      if (invalidationTimerRef.current) {
        clearTimeout(invalidationTimerRef.current);
        invalidationTimerRef.current = null;
      }
      pendingInvalidations.clear();
    };
  }, [auth, eventTypes, queryClient]);

  return { connected };
}
