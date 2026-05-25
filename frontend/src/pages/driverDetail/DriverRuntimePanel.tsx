import { useState } from 'react';
import { Card } from '../../components/ui/Card';
import { Badge, Button, DefinitionList, Field, Select, TextField } from '../../components/ui';
import type { AppiumInstallable, DriverPack, RuntimePolicy } from '../../types/driverPacks';
import { installableSummary, objectEntries, recommendedValue, scalarValue } from './driverDetailFormat';
import { useUpdateRuntimePolicy } from '../../hooks/useDriverDetail';

const STRATEGY_OPTIONS = [
  { value: 'recommended', label: 'Recommended' },
  { value: 'latest_patch', label: 'Latest Patch' },
  { value: 'exact', label: 'Exact' },
];

function policyEquals(a: RuntimePolicy, b: RuntimePolicy): boolean {
  if (a.strategy !== b.strategy) return false;
  if (a.strategy === 'exact') {
    return (a.appium_server_version ?? null) === (b.appium_server_version ?? null)
      && (a.appium_driver_version ?? null) === (b.appium_driver_version ?? null);
  }
  return true;
}

function RuntimePolicyEditor({ pack }: { pack: DriverPack }) {
  const current = pack.runtime_policy;
  const [strategy, setStrategy] = useState(current.strategy);
  const [serverVersion, setServerVersion] = useState(current.appium_server_version ?? '');
  const [driverVersion, setDriverVersion] = useState(current.appium_driver_version ?? '');
  const [error, setError] = useState<string | null>(null);
  const mutation = useUpdateRuntimePolicy();

  const draft: RuntimePolicy = {
    strategy,
    appium_server_version: strategy === 'exact' ? serverVersion || null : null,
    appium_driver_version: strategy === 'exact' ? driverVersion || null : null,
  };
  const isDirty = !policyEquals(current, draft);
  const canSave = isDirty && (strategy !== 'exact' || (serverVersion.length > 0 && driverVersion.length > 0));

  function handleSave() {
    setError(null);
    mutation.mutate(
      { packId: pack.id, runtimePolicy: draft },
      {
        onError: (err: unknown) => {
          const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
          const message = typeof detail === 'string' ? detail : err instanceof Error ? err.message : 'Failed to update runtime policy.';
          setError(message);
        },
      },
    );
  }

  function handleStrategyChange(value: string) {
    setStrategy(value as RuntimePolicy['strategy']);
    setError(null);
  }

  return (
    <div className="grid gap-3">
      <Field label="Runtime Policy" htmlFor="runtime-strategy">
        <Select
          id="runtime-strategy"
          value={strategy}
          onChange={handleStrategyChange}
          options={STRATEGY_OPTIONS}
          size="sm"
        />
      </Field>

      {strategy === 'exact' && (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Appium Server Version" htmlFor="server-version" required>
            <TextField
              id="server-version"
              value={serverVersion}
              onChange={setServerVersion}
              size="sm"
              placeholder="e.g. 2.11.5"
            />
          </Field>
          <Field label="Appium Driver Version" htmlFor="driver-version" required>
            <TextField
              id="driver-version"
              value={driverVersion}
              onChange={setDriverVersion}
              size="sm"
              placeholder="e.g. 3.6.0"
            />
          </Field>
        </div>
      )}

      {error !== null && (
        <p role="alert" className="rounded border border-danger-foreground bg-danger-soft px-3 py-2 text-sm text-danger-foreground">
          {error}
        </p>
      )}

      {isDirty && (
        <div>
          <Button size="sm" onClick={handleSave} disabled={!canSave || mutation.isPending}>
            {mutation.isPending ? 'Saving…' : 'Save Policy'}
          </Button>
        </div>
      )}
    </div>
  );
}

function InstallSpecCard({ title, spec }: { title: string; spec: AppiumInstallable | null | undefined }) {
  return (
    <Card padding="md">
      <h2 className="mb-3 text-sm font-semibold text-text-1">{title}</h2>
      <DefinitionList
        layout="stacked"
        items={[
          { term: 'Install Spec', definition: installableSummary(spec) },
          { term: 'Version Range', definition: spec?.version ?? 'None' },
          { term: 'Recommended', definition: recommendedValue(spec) },
          { term: 'GitHub Repo', definition: spec?.github_repo ?? 'None' },
        ]}
      />
      {spec?.known_bad && spec.known_bad.length > 0 && (
        <div className="mt-3">
          <span className="text-xs font-medium text-text-3">Known Bad Versions</span>
          <div className="mt-1 flex flex-wrap gap-1">
            {spec.known_bad.map((version) => (
              <Badge key={version} tone="critical">
                {version}
              </Badge>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

export function DriverRuntimePanel({ pack }: { pack: DriverPack }) {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card padding="md" className="lg:col-span-2">
        <h2 className="mb-3 text-sm font-semibold text-text-1">Manifest Runtime Contract</h2>
        <RuntimePolicyEditor key={pack.id} pack={pack} />
        {(pack.insecure_features?.length ?? 0) > 0 && (
          <div className="mt-3">
            <span className="text-xs font-medium text-text-3">Insecure Appium Features</span>
            <div className="mt-1 flex flex-wrap gap-1">
              {pack.insecure_features?.map((feature) => (
                <Badge key={feature} tone="warning">
                  {feature}
                </Badge>
              ))}
            </div>
          </div>
        )}
      </Card>

      <InstallSpecCard title="Desired Appium Server" spec={pack.appium_server} />
      <InstallSpecCard title="Desired Appium Driver" spec={pack.appium_driver} />

      <Card padding="md">
        <h2 className="mb-3 text-sm font-semibold text-text-1">Doctor Checks</h2>
        {(pack.doctor?.length ?? 0) === 0 ? (
          <p className="text-sm text-text-3">No doctor checks declared.</p>
        ) : (
          <div className="grid gap-2">
            {pack.doctor?.map((check) => (
              <div key={check.id} className="rounded border border-border px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm text-text-1">{check.id}</span>
                  {check.adapter_hook && <Badge tone="neutral">{check.adapter_hook}</Badge>}
                </div>
                <p className="mt-1 text-sm text-text-3">{check.description}</p>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card padding="md" className="lg:col-span-2">
        <h2 className="mb-3 text-sm font-semibold text-text-1">Workarounds</h2>
        {(pack.workarounds?.length ?? 0) === 0 ? (
          <p className="text-sm text-text-3">No workarounds declared.</p>
        ) : (
          <div className="grid gap-3">
            {pack.workarounds?.map((workaround) => (
              <div key={workaround.id} className="rounded border border-border px-3 py-2">
                <div className="font-mono text-sm text-text-1">{workaround.id}</div>
                <div className="mt-2 grid gap-2 md:grid-cols-2">
                  <DefinitionList
                    layout="stacked"
                    items={objectEntries(workaround.applies_when).map(([key, value]) => ({
                      term: key,
                      definition: scalarValue(value),
                    }))}
                  />
                  <DefinitionList
                    layout="stacked"
                    items={objectEntries(workaround.env).map(([key, value]) => ({
                      term: key,
                      definition: <span className="font-mono">{scalarValue(value)}</span>,
                    }))}
                  />
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
