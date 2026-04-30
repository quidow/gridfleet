import { describe, expect, it } from 'vitest';
import * as ui from './index';

describe('ui barrel', () => {
  it.each([
    'Badge',
    'Button',
    'Card',
    'Checkbox',
    'ConfirmDialog',
    'DataTable',
    'DateInput',
    'DefinitionList',
    'EmptyState',
    'Field',
    'Modal',
    'NumberField',
    'PageHeader',
    'Select',
    'TextField',
    'Textarea',
    'Toggle',
  ])('exports %s', (name) => {
    expect(ui).toHaveProperty(name);
  });
});
