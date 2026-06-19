import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SessionCapabilities } from './SessionCapabilities';
import type { SessionDetail } from '../../types';

function makeSession(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    id: 'row-1',
    session_id: 'sess-1',
    test_name: null,
    started_at: '2026-06-07T10:00:00Z',
    ended_at: null,
    status: 'running',
    requested_capabilities: { platformName: 'Android' },
    actual_capabilities: null,
    error_type: null,
    error_message: null,
    run_id: null,
    is_probe: false,
    probe_checked_by: null,
    device_id: null,
    device_name: null,
    device_pack_id: null,
    device_platform_id: null,
    device_platform_label: null,
    ...overrides,
  } as SessionDetail;
}

describe('SessionCapabilities', () => {
  it('renders requested caps JSON and "Not captured" for missing actual caps', () => {
    render(<SessionCapabilities session={makeSession()} />);
    expect(screen.getByText('Requested capabilities')).toBeInTheDocument();
    expect(screen.getByText('Actual capabilities')).toBeInTheDocument();
    expect(screen.getByText(/"platformName": "Android"/)).toBeInTheDocument();
    expect(screen.getByText('Not captured')).toBeInTheDocument();
  });

  it('renders actual caps when present', () => {
    render(
      <SessionCapabilities
        session={makeSession({ actual_capabilities: { 'appium:systemPort': 8201 } })}
      />,
    );
    expect(screen.getByText(/"appium:systemPort": 8201/)).toBeInTheDocument();
    expect(screen.queryByText('Not captured')).not.toBeInTheDocument();
  });
});
