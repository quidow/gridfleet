import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { QueuedRequestsCard } from './QueuedRequestsCard';
import type { GridQueueRequest } from '../../types';

function renderCard(requests: GridQueueRequest[]) {
  return render(
    <MemoryRouter>
      <QueuedRequestsCard requests={requests} />
    </MemoryRouter>,
  );
}

describe('QueuedRequestsCard', () => {
  it('renders nothing when requests is empty', () => {
    const { container } = renderCard([]);
    expect(container.firstChild).toBeNull();
  });

  it('shows queue count in header', () => {
    renderCard([{ capabilities: { platformName: 'android' } }]);
    expect(screen.getByText('Queued Requests (1)')).toBeInTheDocument();
  });

  it('displays platform name from capabilities', () => {
    renderCard([{ capabilities: { platformName: 'android', 'appium:platformVersion': '14' } }]);
    expect(screen.getByText('android 14')).toBeInTheDocument();
  });

  it('links to run detail when the request carries a runId', () => {
    const runId = '550e8400-e29b-41d4-a716-446655440000';
    renderCard([{ capabilities: { platformName: 'ios' }, runId }]);
    const link = screen.getByRole('link', { name: runId.slice(0, 8) });
    expect(link).toHaveAttribute('href', `/runs/${runId}`);
  });

  it('shows "—" for run on a free request (runId null)', () => {
    renderCard([{ capabilities: { platformName: 'android' }, runId: null }]);
    const cells = screen.getAllByText('—');
    expect(cells.length).toBeGreaterThan(0);
  });

  it('shows "Any" when no gridfleet:deviceId', () => {
    renderCard([{ capabilities: { platformName: 'android' } }]);
    expect(screen.getByText('Any')).toBeInTheDocument();
  });

  it('links to device when gridfleet:deviceId is present', () => {
    const deviceId = 'dev-123';
    renderCard([{ capabilities: { platformName: 'android', 'gridfleet:deviceId': deviceId } }]);
    const link = screen.getByRole('link', { name: deviceId });
    expect(link).toHaveAttribute('href', `/devices/${deviceId}`);
  });

  it('renders formatted wait time from requestTimestamp', () => {
    const now = Date.now();
    const twoMinAgo = new Date(now - 120_000).toISOString();
    renderCard([{ capabilities: { platformName: 'android' }, requestTimestamp: twoMinAgo }]);
    expect(screen.getByText('2m 0s')).toBeInTheDocument();
  });

  it('renders "—" for waiting column when no requestTimestamp', () => {
    renderCard([{ capabilities: { platformName: 'android' } }]);
    const waitingCells = screen.getAllByText('—');
    expect(waitingCells.length).toBeGreaterThanOrEqual(1);
  });
});
