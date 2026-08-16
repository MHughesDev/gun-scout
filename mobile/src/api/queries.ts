import { useQuery } from '@tanstack/react-query';

import { api } from './client';
import type { VerticalId } from './types';

/**
 * Read-only server state, via React Query. Cache lifetimes are set by how
 * often each thing actually changes, not by habit:
 *
 *  - the form schema and site list change only when the server is redeployed,
 *    so they're effectively static for a session;
 *  - `meta` carries the feature flags that switch capability on and off, so
 *    it's refetched often enough that a policy change lands within a session
 *    rather than at next launch;
 *  - stats move whenever anyone, anywhere, runs a search.
 *
 * Live search polling is deliberately NOT here — it accumulates rows across
 * responses, which is not a shape React Query models well. See
 * hooks/use-live-search.ts.
 */

export const queryKeys = {
  meta: ['meta'] as const,
  schema: (vertical: VerticalId) => ['schema', vertical] as const,
  sites: (vertical: VerticalId) => ['sites', vertical] as const,
  stats: (vertical: VerticalId, params: Record<string, string>) =>
    ['stats', vertical, params] as const,
};

export function useMeta() {
  return useQuery({
    queryKey: queryKeys.meta,
    queryFn: ({ signal }) => api.meta(signal),
    // The flags here can disable a capability across every installed copy;
    // an hour-stale answer would be a build ignoring a live policy decision.
    staleTime: 60_000,
    retry: 2,
  });
}

export function useSchema(vertical: VerticalId) {
  return useQuery({
    queryKey: queryKeys.schema(vertical),
    queryFn: ({ signal }) => api.schema(vertical, signal),
    staleTime: 30 * 60_000,
  });
}

export function useSites(vertical: VerticalId) {
  return useQuery({
    queryKey: queryKeys.sites(vertical),
    queryFn: ({ signal }) => api.sites(vertical, signal),
    staleTime: 30 * 60_000,
  });
}

export function useStats(vertical: VerticalId, params: Record<string, string>) {
  return useQuery({
    queryKey: queryKeys.stats(vertical, params),
    queryFn: ({ signal }) => api.stats(vertical, params, signal),
    // Every search anyone runs moves these numbers, so a short window keeps
    // the page honest without re-computing on every focus.
    staleTime: 30_000,
  });
}
