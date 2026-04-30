import SummaryPill from '../ui/SummaryPill';
import { deriveRunsSummary } from './runsSummaryDerivation';
import type { RunRead } from '../../types';

type Props = {
  currentPageRuns: ReadonlyArray<RunRead>;
  last24hRuns: ReadonlyArray<RunRead> | undefined;
  now?: Date;
};

export default function RunsSummaryRow({ currentPageRuns, last24hRuns, now }: Props) {
  const reference = now ?? new Date();
  const live = deriveRunsSummary(currentPageRuns, reference);
  const windowSummary = last24hRuns ? deriveRunsSummary(last24hRuns, reference) : undefined;

  return (
    <>
      <SummaryPill tone="neutral" label="Running" value={live.running} />
      <SummaryPill tone="warn" label="Queued" value={live.queued} />
      <SummaryPill tone="ok" label="Passed 24H" value={windowSummary?.passed24h ?? '—'} />
      <SummaryPill tone="error" label="Failed 24H" value={windowSummary?.failed24h ?? '—'} />
    </>
  );
}
