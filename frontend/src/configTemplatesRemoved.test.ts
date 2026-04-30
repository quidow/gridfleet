import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const ROOT = join(__dirname, '..');
const banned = ['ConfigTemplate', 'config-templates', 'apply-template', 'Apply Template', 'Save Template'];
const SKIP_DIRS = new Set(['node_modules', 'dist', 'test-results', '.git', 'coverage']);

function filesUnder(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue;
    const path = join(dir, entry);
    const stat = statSync(path);
    if (stat.isDirectory()) out.push(...filesUnder(path));
    if (stat.isFile() && /\.(ts|tsx)$/.test(entry)) out.push(path);
  }
  return out;
}

describe('removed config template frontend contract', () => {
  it('has no config-template UI or API references', () => {
    const offenders = filesUnder(ROOT)
      .filter((path) => !path.endsWith('configTemplatesRemoved.test.ts'))
      .flatMap((path) => {
        const text = readFileSync(path, 'utf8');
        return banned.filter((needle) => text.includes(needle)).map((needle) => `${relative(ROOT, path)}:${needle}`);
      });

    expect(offenders).toEqual([]);
  });
});
