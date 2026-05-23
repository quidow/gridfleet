import { Link } from "react-router-dom";

import Button from "../ui/Button";
import type { ImportCommitResult } from "../../api/devicesPortability";

interface Props {
  result: ImportCommitResult;
  onReset: () => void;
}

export function ImportResultsStep({ result, onReset }: Props): JSX.Element {
  return (
    <div className="space-y-3 text-sm">
      <p>
        <span>{result.created.length} created</span>
        {" · "}
        <span>{result.skipped.length} skipped</span>
        {" · "}
        <span>{result.failed.length} failed</span>
      </p>
      {result.created.length > 0 && (
        <section>
          <h3 className="font-semibold">Created</h3>
          <ul className="list-disc pl-5">
            {result.created.map((row) => (
              <li key={row.index}>
                <Link className="text-accent hover:underline" to={`/devices/${row.device_id}`}>
                  {row.device_id}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
      {result.skipped.length > 0 && (
        <section>
          <h3 className="font-semibold">Skipped</h3>
          <ul className="list-disc pl-5">
            {result.skipped.map((row) => (
              <li key={row.index}>
                row {row.index}: {row.reason}
              </li>
            ))}
          </ul>
        </section>
      )}
      {result.failed.length > 0 && (
        <section>
          <h3 className="font-semibold text-danger-foreground">Failed</h3>
          <ul className="list-disc pl-5">
            {result.failed.map((row) => (
              <li key={row.index}>
                row {row.index}: {row.reason}
              </li>
            ))}
          </ul>
        </section>
      )}
      <Button variant="secondary" onClick={onReset}>
        Import another bundle
      </Button>
    </div>
  );
}
