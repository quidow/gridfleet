import { describe, expect, it } from 'vitest';
import { resolvePlatformLabel } from './labels';

describe('resolvePlatformLabel', () => {
  it('prefers explicit catalog label and removes redundant platform qualifiers', () => {
    expect(resolvePlatformLabel('android_mobile', 'My Android')).toBe('My Android');
    expect(resolvePlatformLabel('ios', 'Custom iOS')).toBe('Custom iOS');
    expect(resolvePlatformLabel('roku_network', 'Roku (network)')).toBe('Roku');
    expect(resolvePlatformLabel('firetv_real', 'Fire TV (real device)')).toBe('Fire TV');
    expect(resolvePlatformLabel('android_tv', 'Android TV (emulator)')).toBe('Android TV');
  });

  it('humanizes raw platform id when no catalog label is provided', () => {
    expect(resolvePlatformLabel('android_mobile', null)).toBe('Android Mobile');
    expect(resolvePlatformLabel('android_mobile', null)).toBe('Android Mobile');
    expect(resolvePlatformLabel('ios', null)).toBe('IOS');
    expect(resolvePlatformLabel('ios', null)).toBe('IOS');
    expect(resolvePlatformLabel('tvos', null)).toBe('TVOS');
    expect(resolvePlatformLabel('tvos', null)).toBe('TVOS');
    expect(resolvePlatformLabel('roku_network', null)).toBe('Roku');
    expect(resolvePlatformLabel('firetv_real', null)).toBe('Fire TV');
    expect(resolvePlatformLabel('android_tv', null)).toBe('Android TV');
    expect(resolvePlatformLabel('android_tv', null)).toBe('Android TV');
  });

  it('uses catalog platform label for uploaded driver platforms', () => {
    expect(resolvePlatformLabel('test_network', 'Test Network')).toBe('Test Network');
    expect(resolvePlatformLabel('test_network', null)).toBe('Test');
  });

  it('humanizes completely unknown ids when no catalog label is given', () => {
    expect(resolvePlatformLabel('some_future_platform', null)).toBe('Some Future Platform');
    expect(resolvePlatformLabel('unknown_id', undefined)).toBe('Unknown ID');
  });
});
