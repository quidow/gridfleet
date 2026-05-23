import { ImportResultsStep } from '../devices/ImportResultsStep';
import { ImportReviewStep } from '../devices/ImportReviewStep';
import { ImportUploadStep } from '../devices/ImportUploadStep';
import { useDeviceImport } from '../../hooks/useDeviceImport';

export function DeviceImportPanel() {
  const { state, upload, commit, reset, dispatch } = useDeviceImport();

  return (
    <div className="space-y-4">
      {state.errorMessage && (
        <p
          role="alert"
          className="rounded-md border border-danger-strong/30 bg-danger-soft p-3 text-sm text-danger-foreground"
        >
          {state.errorMessage}
        </p>
      )}
      {(state.status === 'idle' || state.status === 'error' || state.status === 'validating') && (
        <ImportUploadStep onBundle={(bundle) => void upload(bundle)} />
      )}
      {state.status === 'reviewing' && state.preview && (
        <ImportReviewStep
          preview={state.preview}
          mappings={state.mappings}
          onSetMapping={(index, target_host_id) =>
            dispatch({ type: 'SET_MAPPING', index, target_host_id })
          }
          onToggleIncluded={(index) => dispatch({ type: 'TOGGLE_INCLUDED', index })}
          onCommit={() => void commit()}
        />
      )}
      {state.status === 'committing' && (
        <p role="status" aria-live="polite" className="text-sm text-text-3">
          Committing…
        </p>
      )}
      {state.status === 'done' && state.result && (
        <ImportResultsStep result={state.result} onReset={reset} />
      )}
    </div>
  );
}
