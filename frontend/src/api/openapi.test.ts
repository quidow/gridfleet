import { describe, it, expectTypeOf } from 'vitest';
import type { components } from './openapi';

describe('generated openapi.ts', () => {
  it('exposes the core schemas the frontend derives from', () => {
    expectTypeOf<components['schemas']['DeviceRead']>().not.toBeNever();
    expectTypeOf<components['schemas']['HostRead']>().not.toBeNever();
    expectTypeOf<components['schemas']['SessionRead']>().not.toBeNever();
    expectTypeOf<components['schemas']['GridStatusRead']>().not.toBeNever();
    expectTypeOf<components['schemas']['HealthStatusRead']>().not.toBeNever();
    expectTypeOf<components['schemas']['DeviceHealthRead']>().not.toBeNever();
    expectTypeOf<components['schemas']['ConfigAuditEntryRead']>().not.toBeNever();
    expectTypeOf<components['schemas']['TestDataAuditEntryRead']>().not.toBeNever();
    expectTypeOf<components['schemas']['HostToolStatusRead']>().not.toBeNever();
    expectTypeOf<components['schemas']['HTTPValidationError']>().not.toBeNever();
  });
});
