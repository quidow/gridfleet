/// <reference types="node" />
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const FRONTEND_SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SKIP_DIRS = new Set(['node_modules', 'test']);
const SKIP_FILES = new Set(['tokens.css']);
const TOKENS_FILE = join(FRONTEND_SRC, 'tokens.css');

const FORBIDDEN: { name: string; pattern: RegExp }[] = [
  {
    name: 'numeric Tailwind color utility',
    pattern:
      /\b(?:[a-z]+:)*(?:text|bg|border(?:-[trblxy])?|ring(?:-offset)?|outline|from|to|via|stroke|fill|divide|placeholder|accent|caret|decoration|shadow)-(?:blue|amber|indigo|emerald|purple|red|green|slate|gray|zinc|stone|neutral|yellow)-\d{2,3}(?:\/\d{1,3})?\b/,
  },
  { name: 'bracketed dark background hex', pattern: /dark:bg-\[#/ },
  { name: 'bracketed dark text hex', pattern: /dark:text-\[#/ },
  { name: 'bracketed dark border hex', pattern: /dark:border-\[#/ },
  { name: 'bracketed dark ring hex', pattern: /dark:ring-\[#/ },
];

const SCOPED_PATHS = [
  'pages/Dashboard.tsx',
  'components/dashboard/',
  'components/ui/',
];

const SCOPED_FORBIDDEN: { name: string; pattern: RegExp }[] = [
  {
    name: 'arbitrary sizing utility',
    pattern:
      /\b(?:[a-z]+:)*(?:text|leading|w|h|gap|space-[xy]|p[lrtbxy]?|m[lrtbxy]?)-\[\d/,
  },
  {
    name: 'token alpha suffix',
    pattern:
      /\b(?:[a-z]+:)*(?:bg|text|border(?:-[trblxy])?|ring(?:-offset)?|outline|divide|placeholder|from|to|via|stroke|fill|accent|caret|decoration|shadow)-(?:surface|border|text|accent|success|warning|danger|info|neutral|sidebar|device-type|platform|lifecycle)[a-z0-9-]*\/\d+\b/,
  },
];

function isScopedPath(relativePath: string): boolean {
  return SCOPED_PATHS.some((prefix) => relativePath.startsWith(prefix));
}

export function scanSource(relativePath: string, source: string): string[] {
  const violations: string[] = [];
  const lines = source.split('\n');
  const rules = isScopedPath(relativePath)
    ? [...FORBIDDEN, ...SCOPED_FORBIDDEN]
    : FORBIDDEN;
  lines.forEach((line: string, index: number) => {
    for (const rule of rules) {
      if (rule.pattern.test(line)) {
        violations.push(`${relativePath}:${index + 1} — ${rule.name}: ${line.trim()}`);
      }
    }
  });
  return violations;
}

function* walk(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      if (SKIP_DIRS.has(entry)) continue;
      yield* walk(full);
    } else if (st.isFile()) {
      if (SKIP_FILES.has(entry)) continue;
      if (entry.endsWith('.test.ts') || entry.endsWith('.test.tsx')) continue;
      if (/\.(ts|tsx|css)$/.test(entry)) {
        yield full;
      }
    }
  }
}

function* walkAllSource(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      if (SKIP_DIRS.has(entry)) continue;
      yield* walkAllSource(full);
    } else if (st.isFile() && /\.(ts|tsx|css)$/.test(entry)) {
      yield full;
    }
  }
}

describe('scanSource', () => {
  it('returns zero violations for a clean snippet', () => {
    const violations = scanSource('components/ui/Example.tsx', 'const cls = "bg-surface-2 text-text-1";');
    expect(violations).toEqual([]);
  });

  it('returns one violation when the snippet contains a raw numeric color', () => {
    const violations = scanSource('components/ui/Example.tsx', 'const cls = "text-blue-500";');
    expect(violations).toHaveLength(1);
    expect(violations[0]).toContain('numeric Tailwind color utility');
  });
});

describe('scoped hygiene — dashboard + ui primitives', () => {
  it('flags arbitrary sizing inside scoped files', () => {
    const v = scanSource('components/ui/Example.tsx', 'const cls = "text-[11px]";');
    expect(v).toHaveLength(1);
    expect(v[0]).toContain('arbitrary sizing utility');
  });

  it('flags token alpha suffixes inside scoped files', () => {
    const v = scanSource('components/dashboard/Example.tsx', 'const cls = "bg-surface-2/40";');
    expect(v).toHaveLength(1);
    expect(v[0]).toContain('token alpha suffix');
  });

  it('ignores both patterns outside scoped paths', () => {
    const arb = scanSource('pages/Settings.tsx', 'const cls = "text-[11px]";');
    const alpha = scanSource('pages/Settings.tsx', 'const cls = "bg-surface-2/40";');
    expect(arb).toEqual([]);
    expect(alpha).toEqual([]);
  });

  it('accepts tokenized classNames in scoped files', () => {
    const clean = [
      'const cls = "text-xs text-text-3";',
      'const cls = "bg-surface-2 border border-border";',
      'const cls = "bg-accent-soft text-accent";',
    ].join('\n');
    expect(scanSource('components/ui/Example.tsx', clean)).toEqual([]);
  });
});

describe('design token guard', () => {
  it('no forbidden color utilities leak past the token layer', () => {
    const violations: string[] = [];
    for (const file of walk(FRONTEND_SRC)) {
      const source = readFileSync(file, 'utf8');
      violations.push(...scanSource(relative(FRONTEND_SRC, file), source));
    }
    expect(violations, violations.join('\n')).toEqual([]);
  });

  it('keeps the committed heading scale tokens in place', () => {
    const tokens = readFileSync(TOKENS_FILE, 'utf8');
    expect(tokens).toContain("--font-display: 'IBM Plex Sans Variable'");
    expect(tokens).toContain('.heading-page');
    expect(tokens).toContain('@apply font-display text-2xl font-semibold text-text-1');
    expect(tokens).toContain('.heading-section');
    expect(tokens).toContain('@apply font-display text-base font-semibold text-text-1');
    expect(tokens).toContain('.heading-subsection');
    expect(tokens).toContain('@apply font-display text-sm font-semibold text-text-1');
    expect(tokens).toContain('--color-surface-soft: #f3f4f6;');
    expect(tokens).toContain('.heading-label');
    expect(tokens).toContain("@apply font-display text-[11px] font-semibold uppercase tracking-wide text-text-3");
    expect(tokens).toContain('--color-surface-2: #eef2f6;');
  });

  it('pins dark-theme surface + sidebar contrast targets', () => {
    const tokens = readFileSync(TOKENS_FILE, 'utf8');
    const darkBlockMatch = tokens.match(/\.dark \{[\s\S]*?\n {2}\}/);
    expect(darkBlockMatch, 'could not locate .dark {} block in tokens.css').not.toBeNull();
    const darkBlock = darkBlockMatch![0];
    expect(darkBlock).toContain('--color-surface-0: #16161a;');
    expect(darkBlock).toContain('--color-surface-1: #1f1f24;');
    expect(darkBlock).toContain('--color-surface-2: #2a2a30;');
    expect(darkBlock).toContain('--color-surface-soft: #1b1b20;');
    expect(darkBlock).toContain('--color-border: #3a3a42;');
    expect(darkBlock).toContain('--color-sidebar-surface: #0c0c0f;');
    expect(darkBlock).toContain('--color-sidebar-active-bg: #1f1f24;');
    expect(darkBlock).toContain('--color-sidebar-hover-bg: #18181c;');
    expect(darkBlock).toContain('--color-sidebar-border: #3a3a42;');
    expect(darkBlock).toContain('--color-text-3: #939399;');
  });

  it('does not reintroduce legacy tight heading typography', () => {
    const violations: string[] = [];
    const forbidden = [
      { name: 'Inter Tight display alias', pattern: /Inter Tight/ },
      { name: 'tracking-tight utility', pattern: /\btracking-tight\b/ },
      { name: 'negative letter spacing', pattern: /letter-spacing\s*:\s*-/ },
    ];

    for (const file of walkAllSource(FRONTEND_SRC)) {
      const lines = readFileSync(file, 'utf8').split('\n');
      lines.forEach((line: string, index: number) => {
        for (const rule of forbidden) {
          if (rule.pattern.test(line)) {
            violations.push(`${relative(FRONTEND_SRC, file)}:${index + 1} — ${rule.name}: ${line.trim()}`);
          }
        }
      });
    }

    expect(violations, violations.join('\n')).toEqual([]);
  });
});
