import { lazy, Suspense, useMemo, useRef, useState } from 'react';
import type { OnMount } from '@monaco-editor/react';
import { Save, RotateCcw, Code, Clock, ChevronDown, ChevronRight } from 'lucide-react';
import {
  useDeviceTestData,
  useTestDataHistory,
  useReplaceDeviceTestData,
} from '../../hooks/useDevices';
import LoadingSpinner from '../LoadingSpinner';
import Button from '../ui/Button';
import type { DeviceDetail, TestDataAuditEntry } from '../../types';
import { formatDate } from './utils';

interface Props {
  device: DeviceDetail;
}

const MonacoEditor = lazy(() => import('@monaco-editor/react'));

export default function DeviceTestDataEditor({ device }: Props) {
  const { id: deviceId } = device;
  const { data: testData, refetch } = useDeviceTestData(deviceId);
  const { data: history } = useTestDataHistory(deviceId);
  const replaceMutation = useReplaceDeviceTestData(deviceId);

  const [editorValue, setEditorValue] = useState('');
  const [isValid, setIsValid] = useState(true);
  const [isDirty, setIsDirty] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null);

  const syncedValue = useMemo(
    () => (testData !== undefined ? JSON.stringify(testData, null, 2) : ''),
    [testData],
  );
  const activeValue = isDirty ? editorValue : syncedValue;
  const activeIsValid = isDirty ? isValid : true;

  function handleEditorChange(value: string | undefined) {
    const val = value ?? '';
    setEditorValue(val);
    setIsDirty(val !== syncedValue);
    try {
      JSON.parse(val);
      setIsValid(true);
    } catch {
      setIsValid(false);
    }
  }

  function handleReset() {
    setEditorValue(syncedValue);
    setIsDirty(false);
    setIsValid(true);
  }

  function handleFormat() {
    editorRef.current?.getAction('editor.action.formatDocument')?.run();
  }

  async function handleSave() {
    try {
      const parsed = JSON.parse(activeValue) as Record<string, unknown>;
      await replaceMutation.mutateAsync(parsed);
      setIsDirty(false);
      refetch();
    } catch {
      // parse guarded above by activeIsValid; nothing to do
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between border-b border-border px-5 py-4">
        <div>
          <h2 className="text-sm font-semibold text-text-1">Test Data</h2>
          <p className="mt-1 text-xs text-text-2">
            Free-form data delivered to testkit at run time. Not used by Appium sessions and
            never triggers re-verification.
          </p>
        </div>
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

      <div className={`border-b ${!activeIsValid ? 'border-danger-strong' : 'border-border'}`}>
        <Suspense fallback={<LoadingSpinner />}>
          <MonacoEditor
            height="240px"
            language="json"
            value={activeValue}
            onChange={handleEditorChange}
            onMount={(editor) => {
              editorRef.current = editor;
            }}
            options={{
              minimap: { enabled: false },
              lineNumbers: 'on',
              fontSize: 13,
              tabSize: 2,
              automaticLayout: true,
            }}
          />
        </Suspense>
      </div>

      {!activeIsValid && (
        <div className="border-b border-danger-strong bg-danger-soft px-5 py-2 text-xs text-danger-foreground">
          Invalid JSON — fix errors before saving.
        </div>
      )}

      <div className="flex items-center justify-between px-5 py-3">
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            onClick={handleSave}
            disabled={!activeIsValid || !isDirty}
            leadingIcon={<Save size={14} />}
          >
            Save
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

      {showHistory && (
        <div className="max-h-64 overflow-auto border-t border-border px-5 py-4">
          {!history || history.length === 0 ? (
            <p className="text-sm text-text-2">No test-data changes recorded.</p>
          ) : (
            <div className="space-y-3">
              {history.map((entry: TestDataAuditEntry) => (
                <div key={entry.id} className="rounded border border-border p-3">
                  <div className="mb-2 flex justify-between text-xs text-text-2">
                    <span>{formatDate(entry.changed_at)}</span>
                    {entry.changed_by && <span>by {entry.changed_by}</span>}
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <div>
                      <div className="mb-1 text-xs font-medium text-text-3">Previous</div>
                      <pre className="max-h-32 overflow-auto rounded bg-danger-soft p-2 text-xs text-danger-foreground">
                        {entry.previous_test_data
                          ? JSON.stringify(entry.previous_test_data, null, 2)
                          : '{}'}
                      </pre>
                    </div>
                    <div>
                      <div className="mb-1 text-xs font-medium text-text-3">New</div>
                      <pre className="max-h-32 overflow-auto rounded bg-success-soft p-2 text-xs text-success-foreground">
                        {JSON.stringify(entry.new_test_data, null, 2)}
                      </pre>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
