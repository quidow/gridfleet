import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { HostAgentVersionIndicator, HostAgentVersionNotice } from './hostPresentation';

describe('HostAgentVersionIndicator', () => {
  it('shows update badge when agent_update_available is true', () => {
    render(
      <HostAgentVersionIndicator
        version="0.2.0"
        status="ok"
        requiredVersion="0.2.0"
        recommendedVersion="0.3.0"
        updateAvailable={true}
      />,
    );

    expect(screen.getByText('0.2.0')).toBeInTheDocument();
    expect(screen.getByText('Update available')).toBeInTheDocument();
    expect(screen.getByTitle('Recommended version is 0.3.0')).toBeInTheDocument();
  });

  it('keeps minimum-version warning above recommended-version messaging', () => {
    render(
      <HostAgentVersionIndicator
        version="0.1.0"
        status="outdated"
        requiredVersion="0.2.0"
        recommendedVersion="0.3.0"
        updateAvailable={true}
      />,
    );

    expect(screen.getByText('Outdated')).toBeInTheDocument();
    expect(screen.queryByText('Update available')).not.toBeInTheDocument();
  });

  it('shows update badge when minimum-version check is disabled', () => {
    render(
      <HostAgentVersionIndicator
        version="0.2.0"
        status="disabled"
        requiredVersion={null}
        recommendedVersion="0.3.0"
        updateAvailable={true}
      />,
    );

    expect(screen.getByText('Update available')).toBeInTheDocument();
  });

  it('does not show update badge when updateAvailable is false', () => {
    render(
      <HostAgentVersionIndicator
        version="0.3.0"
        status="ok"
        requiredVersion="0.2.0"
        recommendedVersion="0.3.0"
        updateAvailable={false}
      />,
    );

    expect(screen.getByText('0.3.0')).toBeInTheDocument();
    expect(screen.queryByText('Update available')).not.toBeInTheDocument();
  });
});

describe('HostAgentVersionNotice', () => {
  it('describes recommended updates when updateAvailable is true and status is ok', () => {
    render(
      <HostAgentVersionNotice
        version="0.2.0"
        status="ok"
        requiredVersion="0.2.0"
        recommendedVersion="0.3.0"
        updateAvailable={true}
      />,
    );

    expect(screen.getByText('Agent update available')).toBeInTheDocument();
    expect(screen.getByText(/recommended version of 0.3.0/)).toBeInTheDocument();
  });
});
