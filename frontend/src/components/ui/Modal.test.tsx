import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, it, expect, vi } from 'vitest';
import Modal from './Modal';

function TestModal({ onClose = vi.fn(), isOpen = true, ...props }: Partial<React.ComponentProps<typeof Modal>>) {
  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Test Dialog" {...props}>
      <button>First focusable</button>
      <button>Second focusable</button>
    </Modal>
  );
}

describe('Modal', () => {
  it('renders nothing when isOpen=false', () => {
    render(<TestModal isOpen={false} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('renders dialog when isOpen=true', () => {
    render(<TestModal />);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('shows the title', () => {
    render(<TestModal />);
    expect(screen.getByText('Test Dialog')).toBeInTheDocument();
  });

  it('calls onClose when close button is clicked', async () => {
    const onClose = vi.fn();
    render(<TestModal onClose={onClose} />);
    await userEvent.click(screen.getByRole('button', { name: /close dialog/i }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('calls onClose on Escape key', async () => {
    const onClose = vi.fn();
    render(<TestModal onClose={onClose} />);
    await userEvent.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('does not call onClose on Escape when closeOnEscape=false', async () => {
    const onClose = vi.fn();
    render(<TestModal onClose={onClose} closeOnEscape={false} />);
    await userEvent.keyboard('{Escape}');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('calls onClose when backdrop is clicked', async () => {
    const onClose = vi.fn();
    render(
      <div>
        <TestModal onClose={onClose} />
      </div>,
    );
    // The backdrop is the div with bg-black/50 directly under the presentation wrapper
    const presentation = screen.getByRole('presentation');
    // The backdrop is the second child (first is the backdrop div, second is the dialog)
    const backdrop = presentation.querySelector('[aria-hidden="true"]') as HTMLElement;
    await userEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('renders footer when provided', () => {
    render(
      <Modal isOpen onClose={vi.fn()} title="With Footer" footer={<button>Save</button>}>
        Content
      </Modal>,
    );
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
  });

  it('does not steal focus from a controlled input when the modal rerenders', async () => {
    function ControlledInputModal() {
      const [value, setValue] = useState('');

      return (
        <Modal isOpen onClose={() => undefined} title="Controlled Input">
          <input
            aria-label="Device name"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        </Modal>
      );
    }

    render(<ControlledInputModal />);

    const input = screen.getByLabelText('Device name');
    await userEvent.click(input);
    await userEvent.type(input, 'ab');

    expect(input).toHaveValue('ab');
    expect(input).toHaveFocus();
  });
});
