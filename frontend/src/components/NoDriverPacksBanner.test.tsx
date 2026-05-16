import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { NoDriverPacksBanner } from './NoDriverPacksBanner';

it('renders warning when no packs', () => {
  render(<NoDriverPacksBanner packCount={0} />, { wrapper: MemoryRouter });
  expect(screen.getByRole('alert')).toBeInTheDocument();
  expect(screen.getByText(/no driver packs/i)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: /driver/i })).toHaveAttribute('href', '/drivers');
});

it('renders nothing when packs exist', () => {
  const { container } = render(<NoDriverPacksBanner packCount={2} />, { wrapper: MemoryRouter });
  expect(container.firstChild).toBeNull();
});
