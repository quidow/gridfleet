import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function runEslint(input: string, filename: string) {
  const eslintBinPath = fileURLToPath(new URL('../../node_modules/eslint/bin/eslint.js', import.meta.url));
  return execFileSync(process.execPath, [eslintBinPath, '--stdin', '--stdin-filename', filename], {
    cwd: process.cwd(),
    input,
  });
}

describe('design lint rules', () => {
  it('flags raw <select> outside Select.tsx', () => {
    const snippet = "export default function Example() { return <select value='a' onChange={() => {}}><option>a</option></select>; }";

    expect(() => runEslint(snippet, 'src/test/lint-smoke-select.tsx')).toThrow();
  });

  it('flags raw card class string', () => {
    const snippet =
      "export default function Example() { return <div className='bg-surface-1 rounded-lg border border-border p-5' />; }";

    expect(() => runEslint(snippet, 'src/test/lint-smoke-card.tsx')).toThrow();
  });

  it('flags raw input class string', () => {
    const snippet =
      "export default function Example() { return <input className='w-full rounded-md border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent' />; }";

    expect(() => runEslint(snippet, 'src/test/lint-smoke-input.tsx')).toThrow();
  });
});
