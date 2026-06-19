import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import type { SessionDetail } from '../../types';
import { buildSessionColumns } from './sessionColumns';

function renderTestNameCell(session: SessionDetail) {
  const columns = buildSessionColumns();
  const testNameCol = columns.find((c) => c.key === 'test_name');
  expect(testNameCol).toBeDefined();
  return render(<MemoryRouter>{testNameCol!.render(session)}</MemoryRouter>);
}

function baseSession(overrides: Partial<SessionDetail>): SessionDetail {
  return {
    id: 'abc',
    session_id: 'sess-1',
    test_name: 'test_login',
    started_at: '2026-05-15T12:00:00Z',
    ended_at: '2026-05-15T12:00:05Z',
    status: 'passed',
    requested_capabilities: null,
    error_type: null,
    error_message: null,
    run_id: null,
    is_probe: false,
    probe_checked_by: null,
    device_id: 'd-1',
    device_name: 'dev-1',
    device_pack_id: 'appium-uiautomator2',
    device_platform_id: 'android_mobile',
    device_platform_label: null,
    ...overrides,
  } as SessionDetail;
}

describe('buildSessionColumns options', () => {
  it('includes platform column by default', () => {
    const cols = buildSessionColumns();
    expect(cols.find((c) => c.key === 'platform')).toBeDefined();
  });

  it('omits platform column when hidePlatform is true', () => {
    const cols = buildSessionColumns({ hidePlatform: true });
    expect(cols.find((c) => c.key === 'platform')).toBeUndefined();
  });

  it('omits device column when hideDevice is true', () => {
    const cols = buildSessionColumns({ hideDevice: true });
    expect(cols.find((c) => c.key === 'device')).toBeUndefined();
  });

  it('includes both device and platform by default', () => {
    const cols = buildSessionColumns();
    expect(cols.find((c) => c.key === 'device')).toBeDefined();
    expect(cols.find((c) => c.key === 'platform')).toBeDefined();
  });
});

describe('sessionColumns test_name cell', () => {
  it('renders probe badge and source for probe sessions', () => {
    renderTestNameCell(baseSession({ is_probe: true, probe_checked_by: 'scheduled', test_name: '__gridfleet_probe__' }));
    expect(screen.getByText('probe')).toBeInTheDocument();
    expect(screen.getByText('scheduled')).toBeInTheDocument();
    expect(screen.queryByText('__gridfleet_probe__')).not.toBeInTheDocument();
  });

  it('renders the test name for real sessions', () => {
    renderTestNameCell(baseSession({ is_probe: false, test_name: 'test_login' }));
    expect(screen.getByText('test_login')).toBeInTheDocument();
    expect(screen.queryByText('probe')).not.toBeInTheDocument();
  });

  it('omits the source line when probe_checked_by is missing', () => {
    renderTestNameCell(baseSession({ is_probe: true, probe_checked_by: null, test_name: '__gridfleet_probe__' }));
    expect(screen.getByText('probe')).toBeInTheDocument();
    expect(screen.queryByText('scheduled')).not.toBeInTheDocument();
  });
});
