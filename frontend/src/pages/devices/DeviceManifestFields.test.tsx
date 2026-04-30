import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import DeviceManifestFields, { defaultsForDeviceFields, setDeviceConfigField } from './DeviceManifestFields';
import type { PlatformDeviceField } from '../../types';

const fields: PlatformDeviceField[] = [
  { id: 'roku_password', label: 'Developer password', type: 'string', sensitive: true, required_for_session: true },
  { id: 'use_preinstalled_wda', label: 'Use pre-installed WDA', type: 'bool', default: true },
];

describe('DeviceManifestFields', () => {
  it('renders sensitive strings and booleans from manifest fields', () => {
    const onChange = vi.fn();
    render(<DeviceManifestFields fields={fields} value={{ use_preinstalled_wda: true }} onChange={onChange} />);

    expect(screen.getByLabelText('Developer password')).toHaveAttribute('type', 'password');
    expect(screen.getByLabelText('Developer password')).toBeRequired();
    expect(screen.getByRole('checkbox', { name: 'Use pre-installed WDA' })).toBeChecked();

    fireEvent.change(screen.getByLabelText('Developer password'), { target: { value: 'secret' } });
    expect(onChange).toHaveBeenLastCalledWith({ use_preinstalled_wda: true, roku_password: 'secret' });
  });

  it('builds defaults and removes blank string values', () => {
    expect(defaultsForDeviceFields(fields)).toEqual({ use_preinstalled_wda: true });
    expect(setDeviceConfigField({ roku_password: 'secret' }, 'roku_password', '')).toEqual({});
  });
});
