import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import DivergedFromUpstreamBadge from './DivergedFromUpstreamBadge';
import type { DriverPack } from '../../types/driverPacks';

function makePack(overrides: Partial<DriverPack> = {}): DriverPack {
  return {
    id: 'local/my-android',
    display_name: 'My Android',
    state: 'enabled',
    current_release: '2026.04.0',
    active_runs: 0,
    live_sessions: 0,
    runtime_policy: { strategy: 'recommended' },
    derived_from: null,
    ...overrides,
  };
}

const upstreamPack = makePack({
  id: 'appium-uiautomator2',
  display_name: 'Appium UiAutomator2',
  current_release: '2026.05.0',
  derived_from: null,
});

describe('DivergedFromUpstreamBadge', () => {
  it('renders nothing when derived_from is null', () => {
    const pack = makePack({ derived_from: null });
    const { container } = render(
      <DivergedFromUpstreamBadge pack={pack} catalog={[upstreamPack]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when upstream is not in catalog', () => {
    const pack = makePack({
      derived_from: { pack_id: 'appium-uiautomator2', release: '2026.04.0' },
    });
    const { container } = render(
      <DivergedFromUpstreamBadge pack={pack} catalog={[]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when upstream current_release matches derived_from.release', () => {
    const sameRelease = makePack({
      id: 'appium-uiautomator2',
      current_release: '2026.04.0',
      derived_from: null,
    });
    const pack = makePack({
      derived_from: { pack_id: 'appium-uiautomator2', release: '2026.04.0' },
    });
    const { container } = render(
      <DivergedFromUpstreamBadge pack={pack} catalog={[sameRelease]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders the badge when upstream current_release differs from derived_from.release', () => {
    const pack = makePack({
      derived_from: { pack_id: 'appium-uiautomator2', release: '2026.04.0' },
    });
    render(<DivergedFromUpstreamBadge pack={pack} catalog={[upstreamPack]} />);
    expect(
      screen.getByText('Diverged from appium-uiautomator2 2026.05.0'),
    ).toBeInTheDocument();
  });
});
