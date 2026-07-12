import { useState } from 'react';
import { Card } from '../../components/ui/Card';
import { Badge, Button, DefinitionList, Field, Select } from '../../components/ui';
import type { AppiumInstallable, DriverPack, RuntimePolicy } from '../../types/driverPacks';
import { installableSummary, objectEntries, recommendedValue, scalarValue } from './driverDetailFormat';
import { useUpdateRuntimePolicy } from '../../hooks/useDriverDetail';

const STRATEGY_OPTIONS = [
  { value: 'recommended', label: 'Recommended' },
];

function policyEquals(a: RuntimePolicy, b: RuntimePolicy): boolean {
  return a.strategy === b.strategy;
}

function RuntimePolicyEditor({ pack }: { pack: DriverPack }) {
  const current = pack.runtime_policy;
  const [strategy, setStrategy] = useState(current.strategy);
  const [error, setError] = useState<string | null>(null);
  const mutation = useUpdateRuntimePolicy();

  const draft: RuntimePolicy = {
    strategy,
  };
  const isDirty = !policyEquals(current, draft);
  const canSave = isDirty;

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

      <Card padding="md" className="lg:col-span-2">
        <h2 className="mb-3 text-sm font-semibold text-text-1">Appium environment</h2>
        {(pack.appium_env?.length ?? 0) === 0 ? (
          <p className="text-sm text-text-3">No Appium env rules declared.</p>
        ) : (
          <div className="grid gap-3">
            {pack.appium_env?.map((rule) => (
              <div key={rule.id} className="rounded border border-border px-3 py-2">
                <div className="font-mono text-sm text-text-1">{rule.id}</div>
                <div className="mt-2 grid gap-2 md:grid-cols-2">
                  <DefinitionList
                    layout="stacked"
                    items={objectEntries(rule.applies_when).map(([key, value]) => ({
                      term: key,
                      definition: scalarValue(value),
                    }))}
                  />
                  <DefinitionList
                    layout="stacked"
                    items={objectEntries(rule.env).map(([key, value]) => ({
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
