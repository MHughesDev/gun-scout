import Constants from 'expo-constants';

import type {
  Meta,
  SearchState,
  SiteRef,
  StartSearchResponse,
  StatsResponse,
  VerticalId,
  VerticalSchema,
} from './types';

/**
 * The one place that talks to the network.
 *
 * A phone is not a browser: requests hang on a dying cellular connection
 * instead of failing, the process is suspended mid-flight when the user
 * switches apps, and the binary can be years older than the server. So every
 * call goes through here, where those three facts are handled once:
 *
 *  - a hard timeout, because fetch() has none and a hung poll would otherwise
 *    stall the search screen forever;
 *  - `X-Client-Version` on every request, which is what lets the server retire
 *    a broken build (426 -> ApiVersionError -> the app's upgrade screen);
 *  - typed errors, so a screen can tell "search expired" from "no network"
 *    from "server is unhappy" and say something true about each.
 */

const extra = (Constants.expoConfig?.extra ?? {}) as {
  apiUrl?: string;
  clientVersion?: string;
};

export const API_URL = (extra.apiUrl ?? 'http://localhost:8777').replace(/\/+$/, '');
export const CLIENT_VERSION = extra.clientVersion ?? '0.0.0';

const BASE = `${API_URL}/api/v1`;
const DEFAULT_TIMEOUT_MS = 15_000;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/** The server has retired this build. The only correct response is to stop
 *  retrying and tell the user to update — retrying cannot succeed. */
export class ApiVersionError extends ApiError {
  constructor(readonly minVersion: string) {
    super('This version of Gun Scout is no longer supported.', 426);
    this.name = 'ApiVersionError';
  }
}

/** A search that has aged out of the server's memory (results live ~15 min).
 *  Expected, not a failure: the screen offers to run it again. */
export class SearchExpiredError extends ApiError {
  constructor() {
    super('This search has expired.', 404);
    this.name = 'SearchExpiredError';
  }
}

/** Anything that stopped the request from reaching the server: no signal,
 *  airplane mode, DNS, or the timeout below. Worth distinguishing, because
 *  it's the only error class where "try again" is honest advice. */
export class NetworkError extends ApiError {
  constructor(message = 'Could not reach Gun Scout. Check your connection.') {
    super(message, 0);
    this.name = 'NetworkError';
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST';
  body?: unknown;
  timeoutMs?: number;
  /** Lets React Query cancel a poll when the screen unmounts, so a search
   *  left behind doesn't keep the radio awake. */
  signal?: AbortSignal;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT_MS, signal } = opts;

  // fetch() has no timeout of its own; without this a request on a stalled
  // connection never settles and the caller waits indefinitely.
  const timer = new AbortController();
  const timeout = setTimeout(() => timer.abort(), timeoutMs);
  const onOuterAbort = () => timer.abort();
  signal?.addEventListener('abort', onOuterAbort);

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      signal: timer.signal,
      headers: {
        Accept: 'application/json',
        'X-Client-Version': CLIENT_VERSION,
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    // An abort from the caller is a cancellation, not a failure to report.
    if (signal?.aborted) throw new ApiError('cancelled', 0);
    throw new NetworkError();
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener('abort', onOuterAbort);
  }

  if (response.status === 426) {
    const payload = await safeJson<{ min_client_version?: string }>(response);
    throw new ApiVersionError(payload?.min_client_version ?? 'newer');
  }
  if (!response.ok) {
    const payload = await safeJson<{ error?: string; expired?: boolean }>(response);
    if (response.status === 404 && payload?.expired) throw new SearchExpiredError();
    throw new ApiError(payload?.error ?? `Request failed (${response.status})`,
      response.status);
  }
  return (await response.json()) as T;
}

async function safeJson<T>(response: Response): Promise<T | null> {
  try {
    return (await response.json()) as T;
  } catch {
    return null; // an error page that isn't JSON is still an error
  }
}

export const api = {
  meta: (signal?: AbortSignal) => request<Meta>('/meta', { signal }),

  schema: (vertical: VerticalId, signal?: AbortSignal) =>
    request<VerticalSchema>(`/${vertical}/schema`, { signal }),

  sites: (vertical: VerticalId, signal?: AbortSignal) =>
    request<SiteRef[]>(`/${vertical}/clients`, { signal }),

  startSearch: (vertical: VerticalId, criteria: Record<string, unknown>) =>
    request<StartSearchResponse>(`/${vertical}/search`, {
      method: 'POST',
      body: criteria,
      // Starting a search only queues it; the work happens while we poll.
      timeoutMs: 20_000,
    }),

  /** Incremental: `after` is the highest row id already held, so a long
   *  search doesn't re-send everything on every tick. */
  searchState: (searchId: number, after: number, signal?: AbortSignal) =>
    request<SearchState>(`/search/${searchId}?after=${after}`, { signal }),

  stats: (vertical: VerticalId, params: Record<string, string>, signal?: AbortSignal) => {
    const query = new URLSearchParams(params).toString();
    return request<StatsResponse>(`/${vertical}/stats${query ? `?${query}` : ''}`, {
      signal,
      // Stats are computed over the whole fact set; give it room.
      timeoutMs: 25_000,
    });
  },
};
