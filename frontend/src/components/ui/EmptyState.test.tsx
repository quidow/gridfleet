import { render, screen } from '@testing-library/react';
import { Table } from 'lucide-react';
import { describe, expect, it } from 'vitest';
import EmptyState from './EmptyState';

describe('EmptyState', () => {
  it('renders icon, title, description, and action', () => {
    render(
      <EmptyState icon={Table} title="No drivers" description="Add one." action={<button>Add</button>} />,
    );

    expect(screen.getByText('No drivers')).toBeInTheDocument();
    expect(screen.getByText('Add one.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add' })).toBeInTheDocument();
  });
});
