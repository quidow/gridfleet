import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { NodeCard } from './NodeCard';
import type { GridRouterNodeRead } from '../../types/gridRouter';

const node: GridRouterNodeRead = {
  device_id: 'd1',
  device_name: 'iPhone 15',
  platform_id: 'ios',
  host_id: 'h1',
  host_name: 'host-b',
  operational_state: 'busy',
  node_effective_state: 'running',
  session_id: 's_4821',
  session_target: 'http://host-b:8100',
  stereotype: { platformName: 'iOS', 'gridfleet:deviceId': '2b9c41' },
};

function renderCard() {
  render(
    <MemoryRouter>
      <NodeCard node={node} />
    </MemoryRouter>,
  );
}

describe('NodeCard', () => {
  it('renders device name, state, and routing keys', () => {
    renderCard();
    expect(screen.getByText('iPhone 15')).toBeInTheDocument();
    expect(screen.getByText('busy')).toBeInTheDocument();
    expect(screen.getByText('platformName:')).toBeInTheDocument();
    expect(screen.getByText('iOS')).toBeInTheDocument();
  });

  it('copies routing keys to clipboard and shows confirmation', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    renderCard();
    await userEvent.click(screen.getByRole('button', { name: /copy keys/i }));
    expect(writeText).toHaveBeenCalledWith(JSON.stringify(node.stereotype, null, 2));
    expect(await screen.findByText('Copied')).toBeInTheDocument();
  });
});
