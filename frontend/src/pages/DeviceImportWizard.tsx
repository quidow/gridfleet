import { ImportResultsStep } from "../components/devices/ImportResultsStep";
import { ImportReviewStep } from "../components/devices/ImportReviewStep";
import { ImportUploadStep } from "../components/devices/ImportUploadStep";
import { useDeviceImport } from "../hooks/useDeviceImport";

export default function DeviceImportWizard(): JSX.Element {
  const { state, upload, commit, reset, dispatch } = useDeviceImport();

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <h1 className="text-xl font-semibold">Import devices</h1>
      {state.errorMessage && (
        <p className="rounded-md border border-danger-strong/30 bg-danger-soft p-3 text-sm text-danger-foreground">
          {state.errorMessage}
        </p>
      )}
      {(state.status === "idle" || state.status === "error" || state.status === "validating") && (
        <ImportUploadStep onBundle={(bundle) => void upload(bundle)} />
      )}
      {state.status === "reviewing" && state.preview && (
        <ImportReviewStep
          preview={state.preview}
          mappings={state.mappings}
          onSetMapping={(index, target_host_id) =>
            dispatch({ type: "SET_MAPPING", index, target_host_id })
          }
          onToggleIncluded={(index) => dispatch({ type: "TOGGLE_INCLUDED", index })}
          onCommit={() => void commit()}
        />
      )}
      {state.status === "committing" && <p>Committing…</p>}
      {state.status === "done" && state.result && (
        <ImportResultsStep result={state.result} onReset={reset} />
      )}
    </div>
  );
}
