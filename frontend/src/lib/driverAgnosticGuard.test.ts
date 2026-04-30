import { readdirSync, readFileSync, statSync } from 'fs';
import { join, relative } from 'path';
import { describe, expect, it } from 'vitest';

const LEGACY_PLATFORM_LITERALS = [
  'android_mobile',
  'android_mobile',
  'android_tv',
  'android_tv',
  'firetv_real',
  'ios',
  'ios',
  'tvos',
  'tvos',
  'roku_network',
  'android_mobile',
  'android_tv',
  'firetv',
  'roku',
  'ios',
  'tvos',
];

const LEGACY_PRODUCTION_PATTERNS = [
  'launchEmulator',
  'shutdownEmulator',
  'bootSimulator',
  'shutdownSimulator',
  "appiumPlatformName === 'Android'",
  'appiumPlatformName === "Android"',
  "appiumPlatformName === 'iOS'",
  'appiumPlatformName === "iOS"',
  "appiumPlatformName === 'tvOS'",
  'appiumPlatformName === "tvOS"',
];

const SRC_DIR = join(__dirname, '..');

function collectFiles(dir: string): string[] {
  const results: string[] = [];
  for (const entry of readdirSync(dir)) {
    const fullPath = join(dir, entry);
    const stat = statSync(fullPath);
    if (stat.isDirectory()) {
      if (entry === '__fixtures__' || entry === '__mocks__') continue;
      results.push(...collectFiles(fullPath));
    } else if (stat.isFile() && /\.(ts|tsx)$/.test(entry)) {
      if (/\.(test|spec)\.(ts|tsx)$/.test(entry)) continue;
      const rel = relative(SRC_DIR, fullPath);
      if (rel === join('lib', 'driverAgnosticGuard.test.ts')) continue;
      results.push(fullPath);
    }
  }
  return results;
}

describe('driver-agnostic guard', () => {
  it('production code contains no legacy platform string literals', () => {
    const files = collectFiles(SRC_DIR);
    const violations: string[] = [];

    for (const filePath of files) {
      const content = readFileSync(filePath, 'utf-8');
      const rel = relative(SRC_DIR, filePath);
      for (const literal of LEGACY_PLATFORM_LITERALS) {
        if (content.includes(`'${literal}'`) || content.includes(`"${literal}"`)) {
          violations.push(`${rel} contains '${literal}'`);
        }
      }
    }

    expect(violations).toEqual([]);
  });

  it('production code contains no legacy lifecycle or health-label branches', () => {
    const files = collectFiles(SRC_DIR);
    const violations: string[] = [];

    for (const filePath of files) {
      const content = readFileSync(filePath, 'utf-8');
      const rel = relative(SRC_DIR, filePath);
      for (const pattern of LEGACY_PRODUCTION_PATTERNS) {
        if (content.includes(pattern)) {
          violations.push(`${rel} contains ${pattern}`);
        }
      }
    }

    expect(violations).toEqual([]);
  });
});
