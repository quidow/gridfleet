import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import HostLogsPanel from './HostLogsPanel';

vi.mock('./HostAgentLogPanel', () => ({
  default: () => <div>AGENT_PANEL</div>,
}));

vi.mock('./HostEventsPanel', () => ({
  default: () => <div>EVENTS_PANEL</div>,
}));

function renderPanel(path = '/hosts/host-1?tab=logs') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <HostLogsPanel hostId="host-1" />
    </MemoryRouter>,
  );
}

describe('HostLogsPanel', () => {
  it('defaults to agent logs', () => {
    renderPanel();

    expect(screen.getByText('AGENT_PANEL')).toBeInTheDocument();
    expect(screen.queryByText('EVENTS_PANEL')).not.toBeInTheDocument();
  });

  it('opens the events pane from the logs_tab query param', () => {
    renderPanel('/hosts/host-1?tab=logs&logs_tab=events');

    expect(screen.getByText('EVENTS_PANEL')).toBeInTheDocument();
    expect(screen.queryByText('AGENT_PANEL')).not.toBeInTheDocument();
  });

  it('switches panes without dropping the outer tab query param', () => {
    renderPanel();

    fireEvent.click(screen.getByRole('button', { name: 'Host events' }));

    expect(screen.getByText('EVENTS_PANEL')).toBeInTheDocument();
  });
});
