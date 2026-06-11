import { describe, expect, it } from 'vitest';
import type { DriverPack } from '../../types';
import { buildPlatformOptions } from './platformOptions';

function pack(overrides: Record<string, unknown>): DriverPack {
  return {
    id: 'pack-a',
    display_name: 'Pack A',
    state: 'enabled',
    platforms: [],
    ...overrides,
  } as unknown as DriverPack;
}

describe('buildPlatformOptions', () => {
  it('excludes platforms from non-enabled packs', () => {
    const options = buildPlatformOptions([
      pack({ id: 'p1', state: 'disabled', platforms: [{ id: 'android', display_name: 'Android', device_types: ['real_device'], connection_types: ['usb'] }] }),
    ]);
    expect(options).toHaveLength(0);
  });

  it('returns plain labels when base labels are unique', () => {
    const options = buildPlatformOptions([
      pack({ id: 'p1', platforms: [{ id: 'android', display_name: 'Android', device_types: ['real_device'], connection_types: ['usb'] }] }),
    ]);
    expect(options).toHaveLength(1);
    expect(options[0].label).toBe('Android');
    expect(options[0].packId).toBe('p1');
    expect(options[0].platformId).toBe('android');
  });

  it('qualifies colliding base labels by device type, then pack label on residual collision', () => {
    const options = buildPlatformOptions([
      pack({
        id: 'p1',
        display_name: 'Pack One',
        platforms: [
          { id: 'android_real', display_name: 'Android', device_types: ['real_device'], connection_types: ['usb'] },
          { id: 'android_emulator', display_name: 'Android', device_types: ['emulator'], connection_types: ['virtual'] },
        ],
      }),
    ]);
    expect(options.map((o) => o.label)).toEqual(['Android - Real Device', 'Android - Emulator']);
  });
});
