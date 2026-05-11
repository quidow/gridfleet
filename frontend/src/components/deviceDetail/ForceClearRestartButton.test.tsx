import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ForceClearRestartButton from './ForceClearRestartButton';

const mutation = { isPending: false, mutate: vi.fn() };

vi.mock('../../hooks/useDevices', () => ({
  useClearAppiumNodeTransition: () => mutation,
}));

describe('ForceClearRestartButton', () => {
  it('renders nothing when transition token is missing', () => {
    const { container } = render(<ForceClearRestartButton nodeId="node-1" transitionToken={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('clears the restart token when clicked', () => {
    render(<ForceClearRestartButton nodeId="node-1" transitionToken="token-1" />);
    fireEvent.click(screen.getByRole('button', { name: /force-clear restart/i }));
    expect(mutation.mutate).toHaveBeenCalledWith({ nodeId: 'node-1' });
  });
});
