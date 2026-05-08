import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient, type Query, type QueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { useAuth } from '../context/auth';
import { fetchSettings } from '../api/settings';
import { useEventCatalog } from './useEventCatalog';
import type { SettingsGrouped } from '../types';
import { formatEventDetails } from '../components/notifications/eventRegistry';

const EVENT_QUERY_MAP: Record<string, string[][]> = {
  'device.operational_state_changed': [['devices'], ['device']],
  'device.hold_changed': [['devices'], ['device']],
  'device.verification.updated': [['devices'], ['device']],
  'node.state_changed': [['devices'], ['device']],
  'node.crash': [['devices'], ['device']],
  'device.health_changed': [['devices'], ['device'], ['device-health']],
  'host.status_changed': [['hosts'], ['host'], ['devices']],
  'host.heartbeat_lost': [['hosts'], ['host'], ['devices']],
  'host.registered': [['hosts'], ['host']],
  'host.discovery_completed': [['hosts'], ['host'], ['devices'], ['intake-candidates']],
  'session.started': [['sessions'], ['grid-queue'], ['grid-status'], ['devices'], ['device'], ['runs'], ['run']],
  'session.ended': [['sessions'], ['grid-queue'], ['grid-status'], ['devices'], ['device'], ['runs'], ['run']],
  'run.created': [['runs'], ['run'], ['devices']],
  'run.ready': [['runs'], ['run'], ['devices']],
  'run.active': [['runs'], ['run'], ['devices']],
  'run.completed': [['runs'], ['run'], ['devices'], ['sessions']],
  'run.cancelled': [['runs'], ['run'], ['devices'], ['sessions']],
  'run.expired': [['runs'], ['run'], ['devices'], ['sessions']],
  'config.updated': [['device-config'], ['config-history'], ['device'], ['devices']],
  'test_data.updated': [['device-test-data'], ['test-data-history'], ['device'], ['devices']],
  'bulk.operation_completed': [['devices'], ['device'], ['device-groups'], ['device-group']],
  'device_group.updated': [['device-groups'], ['device-group'], ['devices']],
  'device_group.members_changed': [['device-groups'], ['device-group'], ['devices']],
  'settings.changed': [['settings']],
  'system.cleanup_completed': [['sessions'], ['analytics']],
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
  'device.hold_changed': (data) => {
    if (data.new_hold === 'maintenance')
      return { type: 'warning', message: `${data.device_name} entered maintenance` };
    if (data.old_hold === 'maintenance' && data.new_hold === null)
      return { type: 'success', message: `${data.device_name} exited maintenance` };
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
  'device.hold_changed',
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

function invalidateQueryTarget(queryClient: QueryClient, key: string[]) {
  if (key[0] === 'sessions') {
    void queryClient.invalidateQueries({
      predicate: (query) => query.queryKey[0] === 'sessions' && isNewestCursorQuery(query),
    });
    return;
  }
  if (key[0] === 'runs') {
    void queryClient.invalidateQueries({
      predicate: (query) => query.queryKey[0] === 'runs' && isNewestCursorQuery(query),
    });
    return;
  }
  void queryClient.invalidateQueries({ queryKey: key });
}

export function useEventStream() {
  const queryClient = useQueryClient();
  const auth = useAuth();
  const [connected, setConnected] = useState(false);
  const { data: eventCatalog } = useEventCatalog();
  const { data: settingsGroups } = useQuery({
    queryKey: ['settings'],
    queryFn: fetchSettings,
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

    function scheduleHighVolumeInvalidation(queryKeys: string[][]) {
      for (const key of queryKeys) {
        pendingInvalidationsRef.current.add(JSON.stringify(key));
      }
      pendingInvalidationsRef.current.add(JSON.stringify(['notifications']));

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
            void queryClient.invalidateQueries({ queryKey: ['notifications'] });
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
