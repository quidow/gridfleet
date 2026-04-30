import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import DefinitionList from './DefinitionList';

describe('DefinitionList', () => {
  it('renders terms and definitions', () => {
    render(
      <DefinitionList
        items={[
          { term: 'Platform', definition: 'android_mobile' },
          { term: 'Model', definition: 'Pixel 7' },
        ]}
      />,
    );
    expect(screen.getByText('Platform')).toBeInTheDocument();
    expect(screen.getByText('android_mobile')).toBeInTheDocument();
    expect(screen.getByText('Model')).toBeInTheDocument();
    expect(screen.getByText('Pixel 7')).toBeInTheDocument();
  });

  it('uses justified layout by default (flex justify-between)', () => {
    const { container } = render(
      <DefinitionList items={[{ term: 'A', definition: 'B' }]} />,
    );
    const row = container.querySelector('dl > div');
    expect(row?.className).toMatch(/flex justify-between/);
  });

  it('uses stacked layout (flex-col) when requested', () => {
    const { container } = render(
      <DefinitionList
        layout="stacked"
        items={[{ term: 'A', definition: 'B' }]}
      />,
    );
    const row = container.querySelector('dl > div');
    expect(row?.className).toMatch(/flex-col/);
  });

  it('renders ReactNode definitions (not just strings)', () => {
    render(
      <DefinitionList
        items={[{ term: 'T', definition: <span data-testid="node">X</span> }]}
      />,
    );
    expect(screen.getByTestId('node')).toBeInTheDocument();
  });

  it('renders dt and dd semantic elements', () => {
    const { container } = render(
      <DefinitionList items={[{ term: 'A', definition: 'B' }]} />,
    );
    expect(container.querySelector('dl')).toBeInTheDocument();
    expect(container.querySelector('dt')).toBeInTheDocument();
    expect(container.querySelector('dd')).toBeInTheDocument();
  });
});
