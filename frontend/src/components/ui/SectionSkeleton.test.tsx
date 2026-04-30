import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import SectionSkeleton from './SectionSkeleton';

describe('SectionSkeleton', () => {
  it('renders strip skeleton cells', () => {
    render(<SectionSkeleton shape="strip" label="System health loading" />);

    const skeleton = screen.getByRole('status', { name: 'System health loading' });
    expect(skeleton).toBeInTheDocument();
    expect(within(skeleton).getAllByTestId('section-skeleton-cell')).toHaveLength(3);
  });

  it('renders split skeleton panels and rows', () => {
    render(<SectionSkeleton shape="split" rows={2} label="Operations loading" />);

    const skeleton = screen.getByRole('status', { name: 'Operations loading' });
    expect(within(skeleton).getAllByTestId('section-skeleton-panel')).toHaveLength(2);
    expect(within(skeleton).getAllByTestId('section-skeleton-row')).toHaveLength(4);
  });

  it('renders list skeleton rows', () => {
    render(<SectionSkeleton shape="list" rows={5} label="Device recovery loading" />);

    const skeleton = screen.getByRole('status', { name: 'Device recovery loading' });
    expect(screen.getByTestId('section-skeleton-list')).toBeInTheDocument();
    expect(within(skeleton).getAllByTestId('section-skeleton-row')).toHaveLength(5);
  });
});
