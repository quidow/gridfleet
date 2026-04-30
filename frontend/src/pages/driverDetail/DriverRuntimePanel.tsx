import Card from '../../components/ui/Card';
import { Badge, DefinitionList } from '../../components/ui';
import type { AppiumInstallable, DriverPack } from '../../types/driverPacks';
import { installableSummary, objectEntries, recommendedValue, runtimePolicyLabel, scalarValue } from './driverDetailFormat';

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
              <Badge key={version} tone="danger">
                {version}
              </Badge>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

export default function DriverRuntimePanel({ pack }: { pack: DriverPack }) {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card padding="md" className="lg:col-span-2">
        <h2 className="mb-3 text-sm font-semibold text-text-1">Manifest Runtime Contract</h2>
        <DefinitionList items={[{ term: 'Runtime Policy', definition: runtimePolicyLabel(pack.runtime_policy) }]} />
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
