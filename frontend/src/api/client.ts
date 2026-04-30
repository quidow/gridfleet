import axios, { AxiosHeaders, type InternalAxiosRequestConfig } from 'axios';

type ApiErrorEnvelope = {
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
    request_id?: string | null;
  };
};

class ApiError extends Error {
  code?: string;
  details?: unknown;
  requestId?: string | null;
  status?: number;

  constructor(
    message: string,
    options: {
      code?: string;
      details?: unknown;
      requestId?: string | null;
      status?: number;
    } = {},
  ) {
    super(message);
    this.name = 'ApiError';
    this.code = options.code;
    this.details = options.details;
    this.requestId = options.requestId;
    this.status = options.status;
  }
}

const AUTH_HANDLER_BYPASS_HEADER = 'X-GridFleet-Skip-Auth-Handler';

let csrfTokenProvider: (() => string | null) | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function configureApiAuth(options: {
  getCsrfToken: (() => string | null) | null;
  onUnauthorized: (() => void) | null;
}) {
  csrfTokenProvider = options.getCsrfToken;
  unauthorizedHandler = options.onUnauthorized;
}

export function authHandlerBypassHeaders(): Record<string, string> {
  return { [AUTH_HANDLER_BYPASS_HEADER]: '1' };
}

function isMutatingMethod(method?: string): boolean {
  if (!method) {
    return false;
  }
  return ['post', 'put', 'patch', 'delete'].includes(method.toLowerCase());
}

function hasAuthBypassHeader(config?: InternalAxiosRequestConfig): boolean {
  if (!config?.headers) {
    return false;
  }
  return AxiosHeaders.from(config.headers).get(AUTH_HANDLER_BYPASS_HEADER) === '1';
}

const api = axios.create({
  baseURL: '/api',
});

api.interceptors.request.use((config) => {
  if (!isMutatingMethod(config.method)) {
    return config;
  }

  const csrfToken = csrfTokenProvider?.();
  if (!csrfToken) {
    return config;
  }

  const headers = AxiosHeaders.from(config.headers);
  headers.set('X-CSRF-Token', csrfToken);
  config.headers = headers;
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (!axios.isAxiosError(error)) {
      return Promise.reject(error);
    }

    if (error.response?.status === 401 && !hasAuthBypassHeader(error.config)) {
      unauthorizedHandler?.();
    }

    const envelope = error.response?.data as ApiErrorEnvelope | undefined;
    const structuredError = envelope?.error;
    if (structuredError?.message) {
      return Promise.reject(
        new ApiError(structuredError.message, {
          code: structuredError.code,
          details: structuredError.details,
          requestId: structuredError.request_id,
          status: error.response?.status,
        }),
      );
    }

    return Promise.reject(error);
  },
);

export default api;
