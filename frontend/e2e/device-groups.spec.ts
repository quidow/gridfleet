import { test, expect } from './helpers/fixtures';

test.describe('Device group detail', () => {
  test.beforeEach(async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) });
    });
    await page.route((url) => new URL(url).pathname === '/api/device-groups', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 'group-1',
            name: 'QA Devices',
            description: 'Shared devices for QA workflows',
            group_type: 'static',
            device_count: 1,
            filters: null,
            created_at: '2026-04-04T10:00:00Z',
            updated_at: '2026-04-04T10:00:00Z',
          },
        ]),
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/device-groups/group-1', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'group-1',
          name: 'QA Devices',
          description: 'Shared devices for QA workflows',
          group_type: 'static',
          device_count: 1,
          devices: [
            {
              id: 'device-1',
              identity_scheme: 'android_serial',
              identity_scope: 'host',
              identity_value: 'android-1',
              connection_target: '10.0.0.10:5555',
              name: 'Android 1',
              pack_id: 'appium-uiautomator2',
              platform_id: 'android_mobile',
              platform_label: 'Android (real device)',
              os_version: '14',
              host_id: 'host-1',
        operational_state: 'available',
        hold: null,
              tags: { team: 'qa' },
              auto_manage: true,
              device_type: 'real_device',
              connection_type: 'network',
              ip_address: '10.0.0.10',
              readiness_state: 'verified',
              missing_setup_fields: [],
              verified_at: '2026-04-04T10:00:00Z',
              reservation: null,
              lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
              health_summary: { healthy: true, summary: 'Healthy', last_checked_at: '2026-04-04T10:00:00Z' },
              created_at: '2026-04-04T10:00:00Z',
              updated_at: '2026-04-04T10:00:00Z',
            },
          ],
          filters: null,
        }),
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 'device-1',
            identity_scheme: 'android_serial',
            identity_scope: 'host',
            identity_value: 'android-1',
            connection_target: '10.0.0.10:5555',
            name: 'Android 1',
            pack_id: 'appium-uiautomator2',
            platform_id: 'android_mobile',
            platform_label: 'Android (real device)',
            os_version: '14',
            host_id: 'host-1',
        operational_state: 'available',
        hold: null,
            tags: { team: 'qa' },
            auto_manage: true,
            device_type: 'real_device',
            connection_type: 'network',
            ip_address: '10.0.0.10',
            readiness_state: 'verified',
            missing_setup_fields: [],
            verified_at: '2026-04-04T10:00:00Z',
            reservation: null,
            lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
            health_summary: { healthy: true, summary: 'Healthy', last_checked_at: '2026-04-04T10:00:00Z' },
            created_at: '2026-04-04T10:00:00Z',
            updated_at: '2026-04-04T10:00:00Z',
          },
        ]),
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 'host-1',
            hostname: 'lab-linux-1',
            ip: '10.0.0.5',
            os_type: 'linux',
            agent_port: 5100,
            status: 'online',
            agent_version: '1.0.0',
            required_agent_version: '1.0.0',
            recommended_agent_version: '1.0.0',
            agent_update_available: false,
            agent_version_status: 'ok',
            capabilities: null,
            last_heartbeat: '2026-04-04T10:00:00Z',
            created_at: '2026-04-04T10:00:00Z',
          },
        ]),
      });
    });
  });

  test('whole-group actions are visible on the detail page', async ({ page }) => {
    await page.goto('/groups');
    await expect(page.getByRole('heading', { name: 'Device Groups' })).toBeVisible({ timeout: 15_000 });

    const firstGroupLink = page.locator('a[href^="/groups/"]').first();
    await expect(firstGroupLink).toBeVisible();
    await firstGroupLink.click();

    await expect(page.getByRole('heading', { name: 'Whole Group Actions' })).toBeVisible({ timeout: 10_000 });
    const removedTemplateAction = new RegExp(['Apply', 'Template'].join(' '), 'i');
    await expect(page.getByRole('button', { name: removedTemplateAction })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /Update Tags/i })).toBeVisible();
  });

  test('groups index empty state includes a create CTA', async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) });
    });
    await page.route((url) => new URL(url).pathname === '/api/device-groups', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });

    await page.goto('/groups');
    await expect(page.getByText('No device groups')).toBeVisible();
    await page.getByRole('button', { name: 'Create Group' }).last().click();
    await expect(page.getByRole('dialog', { name: 'Create Device Group' })).toBeVisible();
  });

  test('dynamic group creation sends the unified filters payload', async ({ page }) => {
    let createPayload: Record<string, unknown> | null = null;

    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) });
    });
    await page.route((url) => new URL(url).pathname === '/api/device-groups', async (route) => {
      if (route.request().method() === 'POST') {
        createPayload = route.request().postDataJSON() as Record<string, unknown>;
        await route.fulfill({
          status: 201,
          contentType: 'application/json',
          body: JSON.stringify({
            id: 'dynamic-new',
            name: 'QA Android Network',
            description: null,
            group_type: 'dynamic',
            device_count: 0,
            filters: createPayload.filters ?? null,
            created_at: '2026-04-04T10:00:00Z',
            updated_at: '2026-04-04T10:00:00Z',
          }),
        });
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 'device-1',
            identity_scheme: 'android_serial',
            identity_scope: 'host',
            identity_value: 'android-1',
            connection_target: '10.0.0.10:5555',
            name: 'Android 1',
            pack_id: 'appium-uiautomator2',
            platform_id: 'android_mobile',
            platform_label: 'Android (real device)',
            os_version: '14',
            host_id: 'host-1',
        operational_state: 'available',
        hold: null,
            tags: { team: 'qa' },
            auto_manage: true,
            device_type: 'real_device',
            connection_type: 'network',
            ip_address: '10.0.0.10',
            readiness_state: 'verified',
            missing_setup_fields: [],
            verified_at: '2026-04-04T10:00:00Z',
            reservation: null,
            lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
            health_summary: { healthy: true, summary: 'Healthy', last_checked_at: '2026-04-04T10:00:00Z' },
            created_at: '2026-04-04T10:00:00Z',
            updated_at: '2026-04-04T10:00:00Z',
          },
        ]),
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 'host-1',
            hostname: 'lab-linux-1',
            ip: '10.0.0.5',
            os_type: 'linux',
            agent_port: 5100,
            status: 'online',
            agent_version: '1.0.0',
            required_agent_version: '1.0.0',
            recommended_agent_version: '1.0.0',
            agent_update_available: false,
            agent_version_status: 'ok',
            capabilities: null,
            last_heartbeat: '2026-04-04T10:00:00Z',
            created_at: '2026-04-04T10:00:00Z',
          },
        ]),
      });
    });

    await page.goto('/groups');
    await page.getByRole('button', { name: 'Create Group' }).last().click();
    await page.getByLabel('Name').fill('QA Android Network');
    await page.getByLabel('Type').selectOption('dynamic');
    await page.getByText('Device Type').locator('..').getByRole('combobox').selectOption('real_device');
    await page.getByText('Connection Type').locator('..').getByRole('combobox').selectOption('network');
    await page.getByText('Host').locator('..').getByRole('combobox').selectOption('host-1');
    await page.getByText('OS Version').locator('..').getByRole('combobox').selectOption('14');
    await page.getByRole('button', { name: /Add tag/i }).click();
    await page.getByPlaceholder('Tag key').fill('team');
    await page.getByPlaceholder('Tag value').fill('qa');
    
    const responsePromise = page.waitForResponse(res => res.url().includes('/api/device-groups') && res.request().method() === 'POST');
    await page.getByRole('button', { name: 'Create Group' }).last().click();
    await responsePromise;

    expect(createPayload).toBeTruthy();
    expect(createPayload).toMatchObject({
      name: 'QA Android Network',
      group_type: 'dynamic',
      filters: {
        host_id: 'host-1',
        device_type: 'real_device',
        connection_type: 'network',
        os_version: '14',
        tags: { team: 'qa' },
      },
    });
    expect(createPayload).not.toHaveProperty('filter_rules');
  });

  test('group detail empty state exposes the add-devices CTA for static groups', async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) });
    });
    await page.route((url) => new URL(url).pathname === '/api/device-groups/static-empty', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'static-empty',
          name: 'Empty Static Group',
          description: null,
          group_type: 'static',
          device_count: 0,
          devices: [],
          filters: null,
        }),
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });

    await page.goto('/groups/static-empty');
    await expect(page.getByText('No devices in this group yet')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Add Devices' }).last()).toBeVisible();
  });
});
