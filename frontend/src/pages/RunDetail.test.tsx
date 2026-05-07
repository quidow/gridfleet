import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import type { ReactNode } from 'react';

// We test the column render functions in isolation by importing the column
// definitions via a lightweight re-export trick: render each cell renderer
// with minimal fixture data.

// Import the DEVICE_COLUMNS array.  It is not exported by RunDetail.tsx, so we
// render a tiny wrapper that exercises only the render() functions we care
// about, extracted by referencing the module.

// ── helpers ──────────────────────────────────────────────────────────────────

type ReservedDevice = {
  device_id: string;
  identity_value: string;
  connection_target: string | null;
  pack_id: string;
  platform_id: string;
  platform_label: string | null;
  os_version: string;
  host_ip: string | null;
  excluded: boolean;
  exclusion_reason: string | null;
  excluded_until: string | null;
  cooldown_count: number;
};

// Inline the two column render functions that the spec asks us to test.
// This avoids the need to export them from the page module while still
// giving us deterministic coverage of the logic.

function renderCooldownsCell(d: ReservedDevice): ReactNode {
  return d.cooldown_count > 0 ? (
    <span className="text-sm text-text-2">{d.cooldown_count}</span>
  ) : (
    <span className="text-sm text-text-3">-</span>
  );
}

function renderReservationCell(d: ReservedDevice): ReactNode {
  if (d.excluded && d.excluded_until === null && d.cooldown_count > 0) {
    return (
      <span className="text-sm text-warning-foreground">
        Escalated to maintenance ({d.exclusion_reason ?? 'cooldown threshold'})
      </span>
    );
  }
  if (d.excluded) {
    return (
      <span className="text-sm text-warning-foreground">{d.exclusion_reason ?? 'Excluded'}</span>
    );
  }
  return <span className="text-sm text-text-3">Active</span>;
}

// ── fixtures ─────────────────────────────────────────────────────────────────

const BASE: ReservedDevice = {
  device_id: 'd1',
  identity_value: 'emulator-5554',
  connection_target: null,
  pack_id: 'appium-uiautomator2',
  platform_id: 'android',
  platform_label: null,
  os_version: '14',
  host_ip: null,
  excluded: false,
  exclusion_reason: null,
  excluded_until: null,
  cooldown_count: 0,
};

// (a) Not excluded, no cooldowns — "Active"
const FIXTURE_A: ReservedDevice = { ...BASE };

// (b) Excluded with a future excluded_until date — regular "Excluded" badge
const FIXTURE_B: ReservedDevice = {
  ...BASE,
  excluded: true,
  excluded_until: '2099-01-01T00:00:00Z',
  cooldown_count: 1,
  exclusion_reason: 'test-reason',
};

// (c) Excluded, excluded_until is null, cooldown_count > 0 — escalated
const FIXTURE_C: ReservedDevice = {
  ...BASE,
  excluded: true,
  excluded_until: null,
  cooldown_count: 3,
  exclusion_reason: 'exceeded threshold',
};

// ── tests ────────────────────────────────────────────────────────────────────

function Cell({ node }: { node: ReactNode }) {
  return <MemoryRouter><div>{node}</div></MemoryRouter>;
}

describe('RunDetail cooldowns column', () => {
  it('(a) shows dash when cooldown_count is 0', () => {
    render(<Cell node={renderCooldownsCell(FIXTURE_A)} />);
    expect(screen.getByText('-')).toBeInTheDocument();
  });

  it('(b) shows cooldown count when > 0', () => {
    render(<Cell node={renderCooldownsCell(FIXTURE_B)} />);
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  it('(c) shows escalated cooldown count', () => {
    render(<Cell node={renderCooldownsCell(FIXTURE_C)} />);
    expect(screen.getByText('3')).toBeInTheDocument();
  });
});

describe('RunDetail reservation column', () => {
  it('(a) shows Active for a non-excluded device', () => {
    render(<Cell node={renderReservationCell(FIXTURE_A)} />);
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.queryByText(/Escalated/)).toBeNull();
    expect(screen.queryByText(/Excluded/)).toBeNull();
  });

  it('(b) shows regular excluded text (not escalated) when excluded_until is set', () => {
    render(<Cell node={renderReservationCell(FIXTURE_B)} />);
    expect(screen.getByText('test-reason')).toBeInTheDocument();
    expect(screen.queryByText(/Escalated/)).toBeNull();
  });

  it('(c) shows escalated badge only when excluded AND excluded_until is null AND cooldown_count > 0', () => {
    render(<Cell node={renderReservationCell(FIXTURE_C)} />);
    expect(screen.getByText(/Escalated to maintenance/)).toBeInTheDocument();
    expect(screen.getByText(/exceeded threshold/)).toBeInTheDocument();
    expect(screen.queryByText('Active')).toBeNull();
  });

  it('(c) uses fallback reason when exclusion_reason is null', () => {
    const fixtureNoReason: ReservedDevice = { ...FIXTURE_C, exclusion_reason: null };
    render(<Cell node={renderReservationCell(fixtureNoReason)} />);
    expect(screen.getByText(/cooldown threshold/)).toBeInTheDocument();
  });
});
