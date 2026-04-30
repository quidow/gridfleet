import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import HostTerminalPanel from './HostTerminalPanel';

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 80;
    rows = 24;
    open() {}
    loadAddon() {}
    onData() { return { dispose: () => {} }; }
    onResize() { return { dispose: () => {} }; }
    write() {}
    dispose() {}
  },
}));

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    activate() {}
    fit() {}
    dispose() {}
  },
}));

describe('HostTerminalPanel', () => {
  it('shows a feature-disabled state when terminalEnabled is false', () => {
    render(<HostTerminalPanel hostId="h1" hostOnline terminalEnabled={false} />);
    expect(screen.getByText(/Web terminal is not enabled/i)).toBeInTheDocument();
  });

  it('shows an offline state when host is not online', () => {
    render(<HostTerminalPanel hostId="h1" hostOnline={false} terminalEnabled />);
    expect(screen.getByText(/Host must be online/i)).toBeInTheDocument();
  });

  it('renders a connect button when ready', () => {
    render(<HostTerminalPanel hostId="h1" hostOnline terminalEnabled />);
    expect(screen.getByRole('button', { name: /open terminal/i })).toBeInTheDocument();
  });
});
