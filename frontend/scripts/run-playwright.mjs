import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const playwrightCli = require.resolve('@playwright/test/cli');
const env = { ...process.env };

delete env.NO_COLOR;

const result = spawnSync(process.execPath, [playwrightCli, ...process.argv.slice(2)], {
  env,
  stdio: 'inherit',
});

if (result.error) {
  throw result.error;
}

process.exit(result.status ?? 1);
