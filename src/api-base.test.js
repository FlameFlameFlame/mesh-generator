import { describe, expect, it } from 'vitest';

import { installApiV2Prefixing, resolveApiBase, rewriteApiPath } from './api-base.js';


describe('api-base', () => {
  it('resolves API base from injected window value', () => {
    expect(resolveApiBase({ __API_BASE__: '/api/v2/', location: { origin: 'http://localhost' } })).toBe('/api/v2');
    expect(resolveApiBase({ __API_BASE__: '  /proxy/api  ', location: { origin: 'http://localhost' } })).toBe('/proxy/api');
  });

  it('rewrites relative and same-origin absolute /api paths', () => {
    const fakeWindow = { location: { origin: 'http://localhost:5173' } };
    expect(rewriteApiPath('/api/projects', '/api/v2', fakeWindow)).toBe('/api/v2/projects');
    expect(
      rewriteApiPath('http://localhost:5173/api/projects?x=1', '/api/v2', fakeWindow)
    ).toBe('http://localhost:5173/api/v2/projects?x=1');
    expect(rewriteApiPath('https://example.com/api/projects', '/api/v2', fakeWindow)).toBe('https://example.com/api/projects');
  });

  it('patches fetch and EventSource through /api/v2', () => {
    const fetchCalls = [];
    const eventSourceCalls = [];

    class FakeEventSource {
      constructor(url, options) {
        eventSourceCalls.push({ url, options });
      }
    }

    const fakeWindow = {
      __API_BASE__: '/api/v2',
      location: { origin: 'http://localhost:5173' },
      fetch: (url, init) => {
        fetchCalls.push({ url, init });
        return Promise.resolve({ ok: true });
      },
      EventSource: FakeEventSource,
    };

    installApiV2Prefixing(fakeWindow);

    fakeWindow.fetch('/api/projects', { method: 'GET' });
    new fakeWindow.EventSource('/api/optimization-stream');

    expect(fetchCalls[0].url).toBe('/api/v2/projects');
    expect(eventSourceCalls[0].url).toBe('/api/v2/optimization-stream');
  });
});
