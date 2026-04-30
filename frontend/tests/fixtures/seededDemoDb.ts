// frontend/tests/fixtures/seededDemoDb.ts
/**
 * Playwright worker-scoped fixture that reseeds the demo database before
 * the first test in each file. Shells out to the backend seeding CLI so the
 * real router/service stack is exercised.
 */
import { test as base } from "@playwright/test";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import * as path from "node:path";

const execFileAsync = promisify(execFile);

export type SeedScenario = "minimal" | "full_demo" | "chaos";

export interface SeededDemoDbFixture {
  withScenario: (name: SeedScenario) => Promise<void>;
}

const REPO_ROOT = path.resolve(__dirname, "../../..");
const BACKEND_DIR = path.join(REPO_ROOT, "backend");

export const test = base.extend<Record<string, never>, { seededDemoDb: SeededDemoDbFixture }>({
  seededDemoDb: [
    async (_fixtures, use) => {
      const alreadySeeded = new Set<SeedScenario>();
      const fixture: SeededDemoDbFixture = {
        async withScenario(name) {
          if (alreadySeeded.has(name)) return;
          alreadySeeded.add(name);
          const dbUrl =
            process.env.GRIDFLEET_SEED_DATABASE_URL ??
            "postgresql+asyncpg://postgres@localhost/gridfleet_demo";
          await execFileAsync(
            "uv",
            [
              "run",
              "python",
              "-m",
              "app.seeding",
              "--scenario",
              name,
              "--seed",
              "42",
              "--skip-telemetry",
            ],
            {
              cwd: BACKEND_DIR,
              env: { ...process.env, GRIDFLEET_DATABASE_URL: dbUrl, GRIDFLEET_SEED_DATABASE_URL: dbUrl },
            },
          );
        },
      };
      await use(fixture);
    },
    { scope: "worker" },
  ],
});

export const expect = base.expect;
