export function resolveApiBase(windowObj = window) {
  const raw = (windowObj.__API_BASE__ || '/api/v2').trim();
  return raw.endsWith('/') ? raw.slice(0, -1) : raw;
}

export function rewriteApiPath(path, apiBase, windowObj = window) {
  if (typeof path !== 'string') return path;
  if (path.startsWith('/api/')) {
    return apiBase + path.slice(4);
  }
  try {
    const parsed = new URL(path, windowObj.location.origin);
    if (parsed.origin === windowObj.location.origin && parsed.pathname.startsWith('/api/')) {
      parsed.pathname = apiBase + parsed.pathname.slice(4);
      return parsed.toString();
    }
  } catch (_err) {
    return path;
  }
  return path;
}

export function installApiV2Prefixing(windowObj = window) {
  const apiBase = resolveApiBase(windowObj);
  const nativeFetch = windowObj.fetch.bind(windowObj);

  windowObj.fetch = function patchedFetch(input, init) {
    if (typeof input === 'string') {
      return nativeFetch(rewriteApiPath(input, apiBase, windowObj), init);
    }
    if (input instanceof URL) {
      return nativeFetch(new URL(rewriteApiPath(input.toString(), apiBase, windowObj)), init);
    }
    if (typeof Request !== 'undefined' && input instanceof Request) {
      const rewritten = rewriteApiPath(input.url, apiBase, windowObj);
      if (rewritten === input.url) {
        return nativeFetch(input, init);
      }
      return nativeFetch(new Request(rewritten, input), init);
    }
    return nativeFetch(input, init);
  };

  if (typeof windowObj.EventSource === 'function') {
    const NativeEventSource = windowObj.EventSource;
    windowObj.EventSource = class ApiEventSource extends NativeEventSource {
      constructor(url, options) {
        const nextUrl = typeof url === 'string' ? rewriteApiPath(url, apiBase, windowObj) : url;
        super(nextUrl, options);
      }
    };
  }
}
