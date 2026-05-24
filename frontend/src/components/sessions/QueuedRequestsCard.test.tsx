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

  it('links to run detail when gridfleet:run_id is a UUID', () => {
    const runId = '550e8400-e29b-41d4-a716-446655440000';
    renderCard([{ capabilities: { platformName: 'ios', 'gridfleet:run_id': runId } }]);
    const link = screen.getByRole('link', { name: runId.slice(0, 8) });
    expect(link).toHaveAttribute('href', `/runs/${runId}`);
  });

  it('shows "—" for run when gridfleet:run_id is "free"', () => {
    renderCard([{ capabilities: { platformName: 'android', 'gridfleet:run_id': 'free' } }]);
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
});
