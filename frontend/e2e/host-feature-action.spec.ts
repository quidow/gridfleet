import { expect, test } from './helpers/fixtures';
import { fulfillJson } from './helpers/routes';

const HOST_ID = 'host-feature-1';
const PACK_ID = 'appium-uiautomator2';
const FEATURE_ID = 'tunnel';
const ACTION_ID = 'restart';

const HOST = {
  id: HOST_ID,
  hostname: 'lab-feature-host',
  ip: '10.0.2.1',
  os_type: 'macos',
  agent_port: 5100,
  status: 'online',
  agent_version: '1.0.0',
  required_agent_version: '1.0.0',
  agent_version_status: 'ok',
  capabilities: null,
  last_heartbeat: '2026-04-27T10:00:00Z',
  created_at: '2026-04-27T10:00:00Z',
};

const CATALOG_WITH_FEATURE = {
  packs: [
    {
      id: PACK_ID,
      display_name: 'Appium UiAutomator2',
      state: 'enabled',
      active_runs: 0,
      live_sessions: 0,
      current_release: '2026.04.0',
      features: {
        [FEATURE_ID]: {
          display_name: 'Tunnel',
          description_md: '',
          actions: [{ id: ACTION_ID, label: 'Restart tunnel' }],
        },
      },
    },
  ],
};

const HOST_DRIVER_PACKS = {
  host_id: HOST_ID,
  packs: [
    {
      pack_id: PACK_ID,
      pack_release: '2026.04.0',
      runtime_id: 'runtime-android',
      status: 'installed',
      resolved_install_spec: null,
      installer_log_excerpt: null,
      resolver_version: '1',
      blocked_reason: null,
      installed_at: '2026-04-27T00:00:00Z',
    },
  ],
  runtimes: [],
  doctor: [],
  features: [
    {
      pack_id: PACK_ID,
      feature_id: FEATURE_ID,
      ok: false,
      detail: 'tunnel down',
    },
  ],
};

test('feature action button on host detail page triggers action and shows success', async ({ page }) => {
  // Hosts list.
  await page.route('**/api/hosts', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, [HOST]);
  });

  // Host detail.
  await page.route(`**/api/hosts/${HOST_ID}`, async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, { ...HOST, devices: [] });
  });

  // Host driver packs.
  await page.route(`**/api/hosts/${HOST_ID}/driver-packs`, async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, HOST_DRIVER_PACKS);
  });

  // Catalog with one pack that has one feature + one action.
  await page.route('**/api/driver-packs/catalog', async (route) => {
    await fulfillJson(route, CATALOG_WITH_FEATURE);
  });

  // Feature action endpoint: return ok=true.
  await page.route(
    `**/api/hosts/${HOST_ID}/driver-packs/${encodeURIComponent(PACK_ID)}/features/${FEATURE_ID}/actions/${ACTION_ID}`,
    async (route) => {
      if (route.request().method() !== 'POST') {
        await route.fallback();
        return;
      }
      await fulfillJson(route, { ok: true, detail: 'ok', data: {} });
    },
  );

  // Additional mocks needed to keep the host detail page happy.
  await page.route(`**/api/hosts/${HOST_ID}/intake-candidates`, async (route) => {
    await fulfillJson(route, []);
  });

  await page.route(`**/api/hosts/${HOST_ID}/diagnostics`, async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, {
      host_id: HOST_ID,
      circuit_breaker: {
        status: 'closed',
        consecutive_failures: 0,
        cooldown_seconds: 30,
        retry_after_seconds: null,
        probe_in_flight: false,
        last_error: null,
      },
      appium_processes: { reported_at: '2026-04-27T10:00:00Z', running_nodes: [] },
      recent_recovery_events: [],
    });
  });

  await page.route(`**/api/hosts/${HOST_ID}/resource-telemetry`, async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, {
      samples: [],
      latest_recorded_at: null,
      window_start: '2026-04-27T09:00:00Z',
      window_end: '2026-04-27T10:00:00Z',
      bucket_minutes: 5,
    });
  });

  // Step 1: navigate to host detail page directly at the Drivers tab.
  await page.goto(`/hosts/${HOST_ID}?tab=drivers`);

  // Step 2: wait for the drivers panel to load.
  await expect(page.getByText('Appium Drivers')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('Tunnel', { exact: true })).toBeVisible();
  await expect(page.getByText('tunnel down')).toBeVisible();

  // Step 3: the feature action button "Restart tunnel" must be visible (pack status = installed).
  await expect(page.getByRole('button', { name: 'Restart tunnel' })).toBeVisible();

  // Step 4: click the action button.
  await page.getByRole('button', { name: 'Restart tunnel' }).click();

  // Step 5: assert success — sonner renders the toast text in the document.
  await expect(page.getByText('Action Restart tunnel succeeded')).toBeVisible({ timeout: 10_000 });
});
