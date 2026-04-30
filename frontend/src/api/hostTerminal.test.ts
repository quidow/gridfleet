import { describe, expect, it } from 'vitest';

import { buildTerminalWebSocketUrl } from './hostTerminal';

describe('buildTerminalWebSocketUrl', () => {
  it('builds a ws:// URL when the page is served over http', () => {
    const url = buildTerminalWebSocketUrl('abc-123', {
      protocol: 'http:',
      host: 'gridfleet.example:3000',
    });
    expect(url).toBe('ws://gridfleet.example:3000/api/hosts/abc-123/terminal');
  });

  it('builds a wss:// URL when the page is served over https', () => {
    const url = buildTerminalWebSocketUrl('abc-123', {
      protocol: 'https:',
      host: 'gridfleet.example',
    });
    expect(url).toBe('wss://gridfleet.example/api/hosts/abc-123/terminal');
  });
});
