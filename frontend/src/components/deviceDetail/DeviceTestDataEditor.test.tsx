import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import DeviceTestDataEditor from './DeviceTestDataEditor';
import * as api from '../../api/devices';

vi.mock('../../api/devices');
vi.mock('@monaco-editor/react', () => ({
  default: ({ value, onChange }: any) => (
    <textarea data-testid="monaco" value={value} onChange={(e) => onChange(e.target.value)} />
  ),
}));

function wrap(ui: React.ReactNode) {
  const client = new QueryClient();
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

it('saves through replaceDeviceTestData and does not open verification modal', async () => {
  vi.mocked(api.getDeviceTestData).mockResolvedValueOnce({});
  vi.mocked(api.getTestDataHistory).mockResolvedValueOnce([]);
  vi.mocked(api.replaceDeviceTestData).mockResolvedValueOnce({ k: 'v' });

  render(wrap(<DeviceTestDataEditor device={{ id: 'abc' } as any} />));

  await waitFor(() => screen.getByTestId('monaco'));
  fireEvent.change(screen.getByTestId('monaco'), { target: { value: '{"k":"v"}' } });
  fireEvent.click(screen.getByRole('button', { name: /save/i }));

  await waitFor(() =>
    expect(api.replaceDeviceTestData).toHaveBeenCalledWith('abc', { k: 'v' })
  );
  expect(screen.queryByText(/Save Config & Verify/i)).toBeNull();
});
