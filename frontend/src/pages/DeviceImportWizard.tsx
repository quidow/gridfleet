import { DeviceImportPanel } from '../components/settings/DeviceImportPanel';

export default function DeviceImportWizard() {
  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <h1 className="text-xl font-semibold">Import devices</h1>
      <DeviceImportPanel />
    </div>
  );
}
