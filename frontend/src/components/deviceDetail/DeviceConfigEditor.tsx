import { lazy, Suspense, useMemo, useRef, useState } from 'react';
import type { OnMount } from '@monaco-editor/react';
import { Save, RotateCcw, Code, Clock, ChevronDown, ChevronRight, Plus, Eye, EyeOff } from 'lucide-react';
import {
  useDeviceConfig,
  useConfigHistory,
} from '../../hooks/useDevices';
import ConfirmDialog from '../ui/ConfirmDialog';
import LoadingSpinner from '../LoadingSpinner';
import SetupVerificationModal from '../../pages/devices/SetupVerificationModal';
import Button from '../ui/Button';
import type { ConfigAuditEntry, DeviceDetail } from '../../types';
import { formatDateTime } from '../../utils/dateFormatting';

interface Props {
  device: DeviceDetail;
}

const MonacoEditor = lazy(() => import('@monaco-editor/react'));

function formatDate(dateStr: string): string {
  return formatDateTime(dateStr);
}

export default function DeviceConfigEditor({ device }: Props) {
  const { id: deviceId } = device;
  const [reveal, setReveal] = useState(false);
  const { data: config, refetch } = useDeviceConfig(deviceId, reveal);
  const { data: history } = useConfigHistory(deviceId);

  const [editorValue, setEditorValue] = useState('');
  const [isValid, setIsValid] = useState(true);
  const [isDirty, setIsDirty] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showEmptyEditor, setShowEmptyEditor] = useState(false);
  const [pendingVerificationConfig, setPendingVerificationConfig] = useState<Record<string, unknown> | null>(null);
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null);

  const syncedEditorValue = useMemo(() => (config !== undefined ? JSON.stringify(config, null, 2) : ''), [config]);
  const activeEditorValue = isDirty ? editorValue : syncedEditorValue;
  const activeIsValid = isDirty ? isValid : true;
  const hasNoOverrides = config !== undefined && Object.keys(config).length === 0 && !isDirty;
  const editorVisible = !hasNoOverrides || showEmptyEditor;

  const handleEditorMount: OnMount = (editor) => {
    editorRef.current = editor;
  };

  function handleEditorChange(value: string | undefined) {
    const val = value ?? '';
    setEditorValue(val);
    setIsDirty(val !== syncedEditorValue);
    try {
      JSON.parse(val);
      setIsValid(true);
    } catch {
      setIsValid(false);
    }
  }

  function handleFormat() {
    if (editorRef.current) {
      editorRef.current.getAction('editor.action.formatDocument')?.run();
    }
  }

  function handleReset() {
    if (config !== undefined) {
      setEditorValue(syncedEditorValue);
      setIsDirty(false);
      setIsValid(true);
    }
  }

  async function handleSave() {
    try {
      const parsed = JSON.parse(activeEditorValue);
      setPendingVerificationConfig(parsed);
      setShowConfirm(false);
    } catch {
      // JSON parse error — shouldn't happen since we validate
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between border-b border-border px-5 py-4">
        <div>
          <h2 className="text-sm font-semibold text-text-1">Configuration</h2>
          <p className="mt-1 text-xs text-text-2">Device-specific overrides that must pass guided re-verification.</p>
        </div>
        <div className="flex items-center gap-2">
          {/* Reveal toggle */}
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setReveal(!reveal);
              setIsDirty(false);
              setIsValid(true);
              refetch();
            }}
            leadingIcon={reveal ? <EyeOff size={12} /> : <Eye size={12} />}
            title={reveal ? 'Hide sensitive values' : 'Show sensitive values'}
          >
            {reveal ? 'Hide' : 'Show'} secrets
          </Button>

          <Button
            size="sm"
            variant="ghost"
            onClick={() => setShowHistory(!showHistory)}
            leadingIcon={<Clock size={14} />}
            trailingIcon={showHistory ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          >
            History
          </Button>
        </div>
      </div>

      {/* Monaco Editor */}
      {hasNoOverrides && !editorVisible ? (
        <div className="px-5 py-6">
          <div className="flex flex-col gap-4 rounded-lg border border-dashed border-border-strong bg-surface-2 px-4 py-5 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <p className="text-sm font-semibold text-text-1">No config overrides</p>
              <p className="mt-1 text-sm text-text-2">This device uses platform defaults until custom Appium JSON is added.</p>
            </div>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => setShowEmptyEditor(true)}
              leadingIcon={<Plus size={14} />}
            >
              Add override
            </Button>
          </div>
        </div>
      ) : null}
      {editorVisible ? (
      <div className={`border-b ${!activeIsValid ? 'border-danger-strong' : 'border-border'}`}>
        <Suspense fallback={<LoadingSpinner />}>
          <MonacoEditor
            height="300px"
            language="json"
            value={activeEditorValue}
            onChange={handleEditorChange}
            onMount={handleEditorMount}
            options={{
              minimap: { enabled: false },
              lineNumbers: 'on',
              scrollBeyondLastLine: false,
              fontSize: 13,
              tabSize: 2,
              automaticLayout: true,
            }}
          />
        </Suspense>
      </div>
      ) : null}

      {/* Validation error */}
      {editorVisible && !activeIsValid && (
        <div className="border-b border-danger-strong bg-danger-soft px-5 py-2 text-xs text-danger-foreground">
          Invalid JSON — fix errors before saving.
        </div>
      )}

      {/* Actions */}
      {editorVisible ? (
      <div className="flex items-center justify-between px-5 py-3">
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            onClick={() => setShowConfirm(true)}
            disabled={!activeIsValid || !isDirty}
            leadingIcon={<Save size={14} />}
          >
            Save & Verify
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={handleReset}
            disabled={!isDirty}
            leadingIcon={<RotateCcw size={14} />}
          >
            Reset
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={handleFormat}
            leadingIcon={<Code size={14} />}
          >
            Format
          </Button>
        </div>
      </div>
      ) : null}

      {/* History panel */}
      {showHistory && (
        <div className="max-h-64 overflow-auto border-t border-border px-5 py-4">
          {!history || history.length === 0 ? (
            <p className="text-sm text-text-2">No config changes recorded.</p>
          ) : (
            <div className="space-y-3">
              {history.map((entry: ConfigAuditEntry) => (
                <div key={entry.id} className="rounded border border-border p-3">
                  <div className="mb-2 flex justify-between text-xs text-text-2">
                    <span>{formatDate(entry.changed_at)}</span>
                    {entry.changed_by && <span>by {entry.changed_by}</span>}
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <div>
                      <div className="mb-1 text-xs font-medium text-text-3">Previous</div>
                      <pre className="max-h-32 overflow-auto rounded bg-danger-soft p-2 text-xs text-danger-foreground">
                        {entry.previous_config ? JSON.stringify(entry.previous_config, null, 2) : '{}'}
                      </pre>
                    </div>
                    <div>
                      <div className="mb-1 text-xs font-medium text-text-3">New</div>
                      <pre className="max-h-32 overflow-auto rounded bg-success-soft p-2 text-xs text-success-foreground">
                        {JSON.stringify(entry.new_config, null, 2)}
                      </pre>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Save confirmation dialog */}
      <ConfirmDialog
        isOpen={showConfirm}
        onClose={() => setShowConfirm(false)}
        onConfirm={handleSave}
        title="Save Configuration & Verify"
        message="Configuration changes affect device behavior. Saving will hand this device into guided re-verification before the updated config is accepted."
        confirmLabel="Continue"
        variant="default"
      />

      {pendingVerificationConfig && (
        <SetupVerificationModal
          isOpen={pendingVerificationConfig !== null}
          onClose={() => setPendingVerificationConfig(null)}
          onCompleted={() => {
            setPendingVerificationConfig(null);
            setIsDirty(false);
            refetch();
          }}
          existingDevice={device}
          initialExistingForm={{
            host_id: device.host_id,
            device_config: pendingVerificationConfig,
            replace_device_config: true,
          }}
          handoffMessage="Configuration changes affect device behavior and must pass guided re-verification before they are saved."
          title="Save Config & Verify"
        />
      )}
    </div>
  );
}
