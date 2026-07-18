import { lazy, Suspense, useMemo, useState } from 'react';
import { LockKeyhole, Play, Trash2 } from 'lucide-react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  useDeleteDevice,
  useDevice,
  useDeviceHealth,
  useExitDeviceMaintenance,
  useStartNode,
} from '../hooks/useDevices';
import { useHosts } from '../hooks/useHosts';
import { PlatformIcon } from '../components/PlatformIcon';
import { LoadingSpinner } from '../components/LoadingSpinner';
import { SetupVerificationModal } from './devices/SetupVerificationModal';
import { ConfirmDialog } from '../components/ui/ConfirmDialog';
import type { DeviceVerificationUpdate } from '../types';
import { DeviceCapabilitiesPanel } from '../components/deviceDetail/DeviceCapabilitiesPanel';
import { DeviceHealthPanel } from '../components/deviceDetail/DeviceHealthPanel';
import { DeviceLifecyclePolicyPanel } from '../components/deviceDetail/DeviceLifecyclePolicyPanel';
import { DeviceHardwareTelemetryCard } from '../components/deviceDetail/DeviceHardwareTelemetryCard';
import { DeviceInfoPanel } from '../components/deviceDetail/DeviceInfoPanel';
import { DeviceLogsPanel } from '../components/deviceDetail/DeviceLogsPanel';
import { DeviceNodePanel } from '../components/deviceDetail/DeviceNodePanel';
import { DeviceSessionOutcomeHeatmapPanel } from '../components/deviceDetail/DeviceSessionOutcomeHeatmapPanel';
import { StateHistoryPanel } from '../components/deviceDetail/StateHistoryPanel';
import { DeviceEditModal } from './devices/DeviceEditModal';
import { getVerificationAction } from '../lib/deviceWorkflow';
import { deviceChipStatus } from '../lib/deviceState';
import { usePageTitle } from '../hooks/usePageTitle';
import { SectionErrorBoundary } from '../components/ErrorBoundary';
import { useDevRenderCrashTrigger } from '../hooks/useDevRenderCrashTrigger';
import { Badge, Button, Card, PageHeader, Tabs, useTabParam } from '../components/ui';
import { DeviceDetailStatusPills } from './deviceDetail/DeviceDetailStatusPills';
import { buildDeviceDetailSubtitleNode } from './deviceDetail/deviceDetailSubtitle';
import { DeviceSessionsPanel } from './deviceDetail/DeviceSessionsPanel';
import {
  deriveDeviceDetailTriage,
  type DeviceDetailTriage,
  type DeviceDetailTriageTone,
} from './deviceDetail/deviceDetailTriage';

const DeviceConfigEditor = lazy(() =>
  import('../components/deviceDetail/DeviceConfigEditor').then((m) => ({ default: m.DeviceConfigEditor })),
);
const DeviceTestDataEditor = lazy(() =>
  import('../components/deviceDetail/DeviceTestDataEditor').then((m) => ({ default: m.DeviceTestDataEditor })),
);

const TABS = [
  { id: 'triage', label: 'Triage' },
  { id: 'setup', label: 'Setup' },
  { id: 'logs', label: 'Logs' },
  { id: 'sessions', label: 'Sessions' },
  { id: 'history', label: 'History' },
] as const;

const TAB_IDS = TABS.map((t) => t.id);

const TRIAGE_BADGE_TONE: Record<DeviceDetailTriageTone, 'success' | 'warning' | 'critical' | 'neutral' | 'info'> = {
  ok: 'success',
  warn: 'warning',
  error: 'critical',
  neutral: 'neutral',
  info: 'info',
};

function actionLinkClass(tone: DeviceDetailTriageTone): string {
  const base = 'inline-flex items-center justify-center rounded-md px-4 py-2 text-sm font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-accent/40 focus:ring-offset-1';
  if (tone === 'error') {
    return `${base} bg-danger-strong text-danger-on hover:bg-danger-strong/90`;
  }
  if (tone === 'warn') {
    return `${base} bg-warning-strong text-warning-on hover:bg-warning-strong/90`;
  }
  return `${base} bg-accent text-accent-on hover:bg-accent-hover`;
}

function TriageHero({
  triage,
  onVerify,
  onStartNode,
  onExitMaintenance,
  pending,
  verificationLabel,
}: {
  triage: DeviceDetailTriage;
  onVerify: () => void;
  onStartNode: () => void;
  onExitMaintenance: () => void;
  pending: {
    startNode: boolean;
  };
  verificationLabel?: string;
}) {
  const action = triage.action;
  const actionNode = (() => {
    if (action.to) {
      return (
        <Link to={action.to} className={actionLinkClass(triage.tone)}>
          {action.label}
        </Link>
      );
    }
    if (action.kind === 'verify') {
      return <Button onClick={onVerify}>{action.label}</Button>;
    }
    if (action.kind === 'start-node') {
      return <Button onClick={onStartNode} loading={pending.startNode} leadingIcon={<Play size={15} />}>{action.label}</Button>;
    }
    if (action.kind === 'exit-maintenance') {
      return <Button onClick={onExitMaintenance}>{action.label}</Button>;
    }
    return null;
  })();

  const showVerifySecondary = Boolean(verificationLabel) && action.kind === 'start-node';

  return (
    <Card padding="none" as="section" className="overflow-hidden">
      <div className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3 sm:items-center">
          <Badge tone={TRIAGE_BADGE_TONE[triage.tone]} dot>{triage.eyebrow}</Badge>
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-text-1">
              {triage.title}
              {triage.titleLink ? (
                <>{' '}<Link to={triage.titleLink.to} className="text-accent underline hover:text-accent-hover">{triage.titleLink.text}</Link></>
              ) : null}
            </h2>
            {triage.detail ? <p className="mt-0.5 text-xs text-text-2">{triage.detail}</p> : null}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {actionNode}
          {showVerifySecondary ? (
            <Button
              variant="secondary"
              size="sm"
              onClick={onVerify}
              leadingIcon={<LockKeyhole size={14} />}
            >
              {verificationLabel}
            </Button>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

export function DeviceDetail() {
  useDevRenderCrashTrigger('device-detail-page');
  const { id } = useParams<{ id: string }>();
  const deviceId = id ?? '';
  const fallbackTitle = deviceId || 'Device';
  const navigate = useNavigate();
  const { data: device, isLoading } = useDevice(deviceId);
  usePageTitle((device?.name ?? fallbackTitle) || 'Device');
  const {
    data: health,
    isLoading: healthLoading,
  } = useDeviceHealth(deviceId);
  const { data: hosts = [] } = useHosts();
  const deleteDevice = useDeleteDevice();
  const startNode = useStartNode();
  const exitMaintenance = useExitDeviceMaintenance();
  const [tab, setTab] = useTabParam('tab', TAB_IDS as unknown as string[], 'triage');
  const [setupRequest, setSetupRequest] = useState<{
    title: string;
    handoffMessage?: string;
    initialExistingForm?: DeviceVerificationUpdate;
  } | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const hostMap = useMemo(() => new Map(hosts.map((h) => [h.id, h.hostname])), [hosts]);

  const reservation = device?.reservation;
  const reservationLocked = !!reservation;
  const readinessLocked = device ? device.readiness_state !== 'verified' : true;
  const verificationAction = device ? getVerificationAction(device.readiness_state) : null;
  const hostLabel = device ? (hostMap.get(device.host_id) ?? device.host_id) : null;
  const canTestSession = !!device && !reservationLocked && !readinessLocked && deviceChipStatus(device) === 'available';
  const triage = device ? deriveDeviceDetailTriage(device, { health }) : null;
  const triagePending = {
    startNode: startNode.isPending,
  };
  if (!device && isLoading) {
    return (
      <div>
        <Card padding="none" className="py-12">
          <LoadingSpinner />
        </Card>
      </div>
    );
  }

  if (!device) {
    return <p className="text-text-3 text-center mt-12">Device not found</p>;
  }

  // device is defined from here on
  return (
    <div>
      <PageHeader
        title={
          <span className="flex items-center gap-3">
            <span className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-border bg-surface-2 text-text-2">
              <PlatformIcon platformId={device.platform_id} platformLabel={device.platform_label} showLabel={false} />
            </span>
            <span className="min-w-0 truncate">{device.name}</span>
          </span>
        }
        subtitle={buildDeviceDetailSubtitleNode(device, hostLabel)}
        updatedAt={device.updated_at}
        summary={<DeviceDetailStatusPills device={device} />}
      />

      {triage ? (
        <div className="mb-2">
          <TriageHero
            triage={triage}
            pending={triagePending}
            verificationLabel={verificationAction?.buttonLabel}
            onVerify={() => {
              if (!verificationAction) {
                return;
              }
              setSetupRequest({
                title: verificationAction.title,
                handoffMessage: verificationAction.handoffMessage,
              });
            }}
            onStartNode={() => startNode.mutate(device.id)}
            onExitMaintenance={() => exitMaintenance.mutate(device.id)}
          />
        </div>
      ) : null}

      <Tabs
        tabs={TABS as unknown as { id: string; label: string }[]}
        activeId={tab}
        onChange={setTab}
        className="mb-6"
      />

      <div className="fade-in-stagger flex flex-col gap-6">
        {tab === 'triage' ? (
          <>
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] lg:items-start">
              <Card padding="none" as="section" className="overflow-hidden">
                <SectionErrorBoundary scope="device-info-panel">
                  <DeviceInfoPanel
                    device={device}
                    hostLabel={hostLabel ?? undefined}
                    onEdit={() => setEditOpen(true)}
                  />
                </SectionErrorBoundary>
              </Card>

              <Card id="device-health" padding="none" as="section" className="overflow-hidden">
                <div className="divide-y divide-border">
                  <div className="p-5">
                    <SectionErrorBoundary scope="device-health-panel">
                      <DeviceHealthPanel
                        health={health}
                        packId={device.pack_id}
                        platformId={device.platform_id}
                        deviceType={device.device_type}
                        connectionType={device.connection_type}
                        deviceId={device.id}
                        canTestSession={canTestSession}
                        isLoading={healthLoading}
                      />
                    </SectionErrorBoundary>
                  </div>
                  <SectionErrorBoundary scope="device-hardware-telemetry">
                    <DeviceHardwareTelemetryCard device={device} />
                  </SectionErrorBoundary>
                </div>
              </Card>
            </div>
          </>
        ) : null}

        {tab === 'logs' ? (
          <SectionErrorBoundary scope="device-logs-panel">
            <DeviceLogsPanel deviceId={deviceId} />
          </SectionErrorBoundary>
        ) : null}

        {tab === 'setup' ? (
          <>
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2 lg:items-start">
              <Card padding="none" as="section" className="overflow-hidden p-5">
                <SectionErrorBoundary scope="device-node-panel-setup">
                  <DeviceNodePanel device={device} />
                </SectionErrorBoundary>
              </Card>

              <Card padding="none" as="section" className="overflow-hidden">
                <SectionErrorBoundary scope="device-lifecycle-policy-panel">
                  <DeviceLifecyclePolicyPanel policy={health?.lifecycle_policy} />
                </SectionErrorBoundary>
              </Card>
            </div>

            <Card padding="none" as="section" className="overflow-hidden">
              <SectionErrorBoundary scope="device-capabilities-panel">
                <DeviceCapabilitiesPanel deviceId={deviceId} device={device} />
              </SectionErrorBoundary>

              <div className="border-t border-border">
                <SectionErrorBoundary scope="device-config-editor">
                  <Suspense fallback={<LoadingSpinner />}>
                    <DeviceConfigEditor device={device} />
                  </Suspense>
                </SectionErrorBoundary>
              </div>
            </Card>

            <Card padding="none" as="section" className="overflow-hidden">
              <SectionErrorBoundary scope="device-test-data-editor">
                <Suspense fallback={<LoadingSpinner />}>
                  <DeviceTestDataEditor device={device} />
                </Suspense>
              </SectionErrorBoundary>
            </Card>

            <Card aria-labelledby="device-danger-zone-heading" padding="none" as="section" className="overflow-hidden">
              <div className="flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <Badge tone="critical">Danger</Badge>
                    <h2
                      id="device-danger-zone-heading"
                      className="text-sm font-semibold text-text-1"
                    >
                      Danger Zone
                    </h2>
                  </div>
                  <p className="mt-1 text-xs text-text-2">
                    Deleting removes this device and its session history from GridFleet. This action cannot be undone.
                  </p>
                </div>
                <Button
                  variant="danger"
                  leadingIcon={<Trash2 size={14} />}
                  onClick={() => setDeleteOpen(true)}
                  disabled={deleteDevice.isPending}
                >
                  Delete Device
                </Button>
              </div>
            </Card>
          </>
        ) : null}

        {tab === 'sessions' ? (
          <SectionErrorBoundary scope="device-sessions-panel">
            <DeviceSessionsPanel deviceId={deviceId} />
          </SectionErrorBoundary>
        ) : null}

        {tab === 'history' ? (
          <div className="space-y-6">
            <SectionErrorBoundary scope="device-session-outcome-heatmap">
              <DeviceSessionOutcomeHeatmapPanel deviceId={device.id} />
            </SectionErrorBoundary>

            <SectionErrorBoundary scope="device-state-history">
              <StateHistoryPanel deviceId={device.id} />
            </SectionErrorBoundary>
          </div>
        ) : null}
      </div>

      {setupRequest && verificationAction ? (
        <SetupVerificationModal
          isOpen
          onClose={() => setSetupRequest(null)}
          existingDevice={device}
          initialExistingForm={setupRequest.initialExistingForm}
          onCompleted={() => setSetupRequest(null)}
          handoffMessage={setupRequest.handoffMessage}
          title={setupRequest.title ?? verificationAction.title}
        />
      ) : null}

      <DeviceEditModal
        device={editOpen ? device : null}
        hostMap={hostMap}
        onClose={() => setEditOpen(false)}
        onRequestVerification={(req) => {
          setEditOpen(false);
          setSetupRequest({
            title: req.title,
            handoffMessage: req.handoffMessage,
            initialExistingForm: req.initialExistingForm,
          });
        }}
      />

      <ConfirmDialog
        isOpen={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        onConfirm={() => {
          deleteDevice.mutate(device.id, {
            onSuccess: () => navigate('/devices'),
          });
        }}
        title="Delete Device"
        message="Are you sure you want to delete this device? This action cannot be undone."
        confirmLabel="Delete"
        variant="danger"
      />
    </div>
  );
}
