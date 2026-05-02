import { expect, test, type Page, type Route } from '@playwright/test';

type AuthSession = {
  enabled: boolean;
  authenticated: boolean;
  username: string | null;
  csrf_token: string | null;
  expires_at: string | null;
};

const UNAUTHENTICATED_SESSION: AuthSession = {
  enabled: true,
  authenticated: false,
  username: null,
  csrf_token: null,
  expires_at: null,
};

const AUTHENTICATED_SESSION: AuthSession = {
  enabled: true,
  authenticated: true,
  username: 'operator',
  csrf_token: 'csrf-token-1',
  expires_at: '2026-04-16T18:00:00Z',
};

const HOSTS = [
  {
    id: 'host-1',
    hostname: 'lab-host-01',
    ip: '10.0.0.20',
    os_type: 'linux',
    status: 'online',
    agent_version: '1.0.0',
    required_agent_version: '1.0.0',
    recommended_agent_version: '1.0.0',
    agent_update_available: false,
    agent_version_status: 'ok',
    capabilities: null,
    last_heartbeat: '2026-04-16T10:00:00Z',
    created_at: '2026-04-16T09:00:00Z',
    agent_port: 5100,
    missing_prerequisites: [],
  },
];

const DEVICES = [
  {
    id: 'device-1',
    host_id: 'host-1',
    name: 'Pixel Lab',
    status: 'available',
    identity_scheme: 'android_serial',
    identity_scope: 'host',
    identity_value: 'emulator-5554',
    connection_target: 'emulator-5554',
    pack_id: 'appium-uiautomator2',
    platform_id: 'android_mobile',
    platform_label: 'Android (real device)',
    os_version: '14',
    tags: {},
    auto_manage: true,
    device_type: 'real_device',
    connection_type: 'usb',
    ip_address: null,
    readiness_state: 'verified',
    missing_setup_fields: [],
    verified_at: '2026-04-16T09:30:00Z',
    reservation: null,
    lifecycle_policy_summary: {
      state: 'idle',
      label: 'Idle',
      detail: null,
      backoff_until: null,
    },
    health_summary: {
      healthy: true,
      summary: 'Healthy',
      last_checked_at: '2026-04-16T09:30:00Z',
    },
    emulator_state: null,
    created_at: '2026-04-16T09:00:00Z',
    updated_at: '2026-04-16T09:30:00Z',
  },
];

async function fulfillJson(route: Route, payload: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(payload),
  });
}

async function installAuthAppRoutes(
  page: Page,
  options: {
    initialSession?: AuthSession;
  } = {},
) {
  const state = {
    session: options.initialSession ?? { ...UNAUTHENTICATED_SESSION },
  };

  await page.route((url) => new URL(url).pathname.startsWith('/api/'), async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === '/api/auth/session' && method === 'GET') {
      await fulfillJson(route, state.session);
      return;
    }

    if (path === '/api/auth/login' && method === 'POST') {
      const body = request.postDataJSON() as { username?: string; password?: string } | null;
      if (body?.username === 'operator' && body.password === 'operator-secret') {
        state.session = { ...AUTHENTICATED_SESSION };
        await fulfillJson(route, state.session);
        return;
      }
      await fulfillJson(
        route,
        {
          error: {
            code: 'UNAUTHORIZED',
            message: 'Invalid username or password',
            request_id: 'req-login-failed',
          },
        },
        401,
      );
      return;
    }

    if (path === '/api/auth/logout' && method === 'POST') {
      state.session = { ...UNAUTHENTICATED_SESSION };
      await fulfillJson(route, state.session);
      return;
    }

    if (path === '/api/events/catalog' && method === 'GET') {
      await fulfillJson(route, { events: [] });
      return;
    }

    if (path === '/api/settings' && method === 'GET') {
      await fulfillJson(route, []);
      return;
    }

    if (path === '/api/hosts' && method === 'GET') {
      await fulfillJson(route, HOSTS);
      return;
    }

    if (/^\/api\/hosts\/[^/]+\/intake-candidates$/.test(path) && method === 'GET') {
      await fulfillJson(route, []);
      return;
    }

    if (path === '/api/devices' && method === 'GET') {
      await fulfillJson(route, DEVICES);
      return;
    }

    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({
        error: {
          code: 'UNEXPECTED_TEST_ROUTE',
          message: `Unhandled mock route: ${method} ${path}`,
        },
      }),
    });
  });
}

test.describe('Authentication', () => {
  test('restores the requested deep link after a successful login', async ({ page }) => {
    await installAuthAppRoutes(page);

    await page.goto('/hosts?search=lab');

    await expect(page.getByRole('heading', { name: 'GridFleet' })).toBeVisible();
    await expect(page.getByLabel('Username')).toBeVisible();
    await expect(page).toHaveURL(/\/login\?next=%2Fhosts%3Fsearch%3Dlab$/);

    await page.getByLabel('Username').fill('operator');
    await page.getByLabel('Password').fill('operator-secret');
    await page.getByRole('button', { name: 'Sign In' }).click();

    await expect(page).toHaveURL(/\/hosts\?search=lab$/, { timeout: 15_000 });
    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('operator')).toBeVisible({ timeout: 15_000 });
  });

  test('shows an inline error when credentials are invalid', async ({ page }) => {
    await installAuthAppRoutes(page);

    await page.goto('/login');

    await page.getByLabel('Username').fill('operator');
    await page.getByLabel('Password').fill('wrong-password');
    await page.getByRole('button', { name: 'Sign In' }).click();

    await expect(page.getByRole('alert')).toContainText('Invalid username or password');
    await expect(page).toHaveURL(/\/login$/);
  });

  test('logs out from the authenticated shell and returns to the login screen', async ({ page }) => {
    await installAuthAppRoutes(page, { initialSession: { ...AUTHENTICATED_SESSION } });

    await page.goto('/hosts');

    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Log Out' }).click();

    await expect(page.getByLabel('Username')).toBeVisible();
    await expect(page).toHaveURL(/\/login(?:\?next=%2Fhosts)?$/);
  });
});
