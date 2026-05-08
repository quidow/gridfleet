import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { beforeEach, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import DeviceTestDataEditor from './DeviceTestDataEditor';
import * as api from '../../api/devices';

vi.mock('../../api/devices');

beforeEach(() => {
  vi.clearAllMocks();
});
vi.mock('@monaco-editor/react', () => ({
  default: ({ value, onChange }: { value: string; onChange: (next: string) => void }) => (
    <textarea data-testid="monaco" value={value} onChange={(e) => onChange(e.target.value)} />
  ),
}));

function wrap(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

function renderEditor() {
  return render(
    wrap(
      <DeviceTestDataEditor
        device={{ id: 'abc' } as unknown as Parameters<typeof DeviceTestDataEditor>[0]['device']}
      />,
    ),
  );
}

it('saves through replaceDeviceTestData and does not open verification modal', async () => {
  vi.mocked(api.getDeviceTestData).mockResolvedValueOnce({});
  vi.mocked(api.getTestDataHistory).mockResolvedValueOnce([]);
  vi.mocked(api.replaceDeviceTestData).mockResolvedValueOnce({ k: 'v' });

  renderEditor();
  await waitFor(() => screen.getByTestId('monaco'));
  fireEvent.change(screen.getByTestId('monaco'), { target: { value: '{"k":"v"}' } });
  fireEvent.click(screen.getByRole('button', { name: /save/i }));

  await waitFor(() =>
    expect(api.replaceDeviceTestData).toHaveBeenCalledWith('abc', { k: 'v' })
  );
  expect(screen.queryByText(/Save Config & Verify/i)).toBeNull();
});

it('rejects array root with inline error and disables save', async () => {
  vi.mocked(api.getDeviceTestData).mockResolvedValueOnce({});
  vi.mocked(api.getTestDataHistory).mockResolvedValueOnce([]);

  renderEditor();
  await waitFor(() => screen.getByTestId('monaco'));
  fireEvent.change(screen.getByTestId('monaco'), { target: { value: '["a","b"]' } });

  await waitFor(() =>
    expect(screen.getByText(/Root must be a JSON object/i)).toBeDefined()
  );
  expect(screen.getByRole('button', { name: /save/i })).toBeDisabled();
  expect(api.replaceDeviceTestData).not.toHaveBeenCalled();
});

it('rejects scalar root with inline error', async () => {
  vi.mocked(api.getDeviceTestData).mockResolvedValueOnce({});
  vi.mocked(api.getTestDataHistory).mockResolvedValueOnce([]);

  renderEditor();
  await waitFor(() => screen.getByTestId('monaco'));
  fireEvent.change(screen.getByTestId('monaco'), { target: { value: '"foo"' } });

  await waitFor(() =>
    expect(screen.getByText(/Root must be a JSON object/i)).toBeDefined()
  );
  expect(api.replaceDeviceTestData).not.toHaveBeenCalled();
});

it('rejects oversized payload with inline error', async () => {
  vi.mocked(api.getDeviceTestData).mockResolvedValueOnce({});
  vi.mocked(api.getTestDataHistory).mockResolvedValueOnce([]);

  renderEditor();
  await waitFor(() => screen.getByTestId('monaco'));
  const big = JSON.stringify({ k: 'x'.repeat(64 * 1024 + 100) });
  fireEvent.change(screen.getByTestId('monaco'), { target: { value: big } });

  await waitFor(() =>
    expect(screen.getByText(/limit is 65536 bytes/i)).toBeDefined()
  );
  expect(api.replaceDeviceTestData).not.toHaveBeenCalled();
});

it('surfaces server error when mutation rejects', async () => {
  vi.mocked(api.getDeviceTestData).mockResolvedValueOnce({});
  vi.mocked(api.getTestDataHistory).mockResolvedValueOnce([]);
  vi.mocked(api.replaceDeviceTestData).mockRejectedValueOnce({
    response: { status: 500, data: { error: { message: 'boom' } } },
    message: 'Request failed with status code 500',
  });

  renderEditor();
  await waitFor(() => screen.getByTestId('monaco'));
  fireEvent.change(screen.getByTestId('monaco'), { target: { value: '{"k":"v"}' } });
  fireEvent.click(screen.getByRole('button', { name: /save/i }));

  await waitFor(() =>
    expect(screen.getByText(/Save failed \(500\): boom/i)).toBeDefined()
  );
});
