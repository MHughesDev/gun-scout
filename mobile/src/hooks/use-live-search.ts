import { useCallback, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { api, ApiError, SearchExpiredError } from '@/api/client';
import type { ClientState, Listing } from '@/api/types';

/**
 * Drives one running search: poll, accumulate, stop.
 *
 * The server holds a search in memory and hands back only rows newer than the
 * cursor you send, so results arrive in slices and the client owns the
 * accumulated list. That's the same contract the web UI polls (static/search.js),
 * with three things a phone needs and a browser tab doesn't:
 *
 *  - **Stop polling when backgrounded.** iOS suspends the process anyway, but
 *    Android will happily keep a timer firing behind the lock screen, burning
 *    battery and cellular data on a search nobody is watching. The cursor
 *    makes resuming free: the next poll after foregrounding returns everything
 *    missed in one response.
 *  - **Back off on failure instead of hammering.** A poll that fails on a weak
 *    connection is likely to fail again immediately; retrying at 1.2s turns a
 *    tunnel into a request storm. Consecutive failures widen the interval, and
 *    only a sustained run of them surfaces as an error.
 *  - **Treat expiry as an outcome, not a crash.** Results evaporate server-side
 *    ~15 minutes after a search finishes, which is easy to hit on mobile — the
 *    user locks the phone mid-search and comes back to it. That yields
 *    `expired`, which the screen offers to re-run.
 */

const POLL_MS = 1_200;
const MAX_BACKOFF_MS = 10_000;
const FAILURES_BEFORE_ERROR = 4;

export type LiveSearchPhase = 'idle' | 'running' | 'done' | 'expired' | 'error';

export interface LiveSearch {
  phase: LiveSearchPhase;
  listings: Listing[];
  clients: ClientState[];
  error: string | null;
  /** Count of rows the stats engine had never seen — the "NEW" badges. */
  newCount: number;
}

interface State {
  /** Which search the rest of this object describes. Held in state so a new
   *  id can be detected during render, not after the first paint. */
  searchId: number | null;
  listings: Listing[];
  clients: ClientState[];
  phase: LiveSearchPhase;
  error: string | null;
}

const emptyState = (searchId: number | null): State => ({
  searchId,
  listings: [],
  clients: [],
  phase: searchId == null ? 'idle' : 'running',
  error: null,
});

export function useLiveSearch(searchId: number | null): LiveSearch {
  const [state, setState] = useState<State>(() => emptyState(searchId));

  // Cursor and failure count live in refs: the poll loop reads them on every
  // tick, and routing them through state would restart the loop each time.
  const cursor = useRef(0);
  const failures = useRef(0);

  // Reset during render, React's prescribed way to adjust state when an input
  // changes. Doing it in the effect instead would let one frame render the
  // previous search's rows under the new id.
  if (state.searchId !== searchId) {
    cursor.current = 0;
    failures.current = 0;
    setState(emptyState(searchId));
  }

  useEffect(() => {
    if (searchId == null) return;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const inFlight = new AbortController();

    const schedule = (ms: number) => {
      if (cancelled) return;
      timer = setTimeout(poll, ms);
    };

    const poll = async () => {
      if (cancelled) return;

      // Backgrounded: hold the loop open but do no work. Cheaper and more
      // honest than tearing the search down and rebuilding it on resume.
      if (AppState.currentState !== 'active') {
        schedule(POLL_MS);
        return;
      }

      try {
        const fresh = await api.searchState(searchId, cursor.current, inFlight.signal);
        if (cancelled) return;
        failures.current = 0;

        // Row ids are 1..n in arrival order; the max is the next cursor.
        cursor.current = fresh.listings.reduce(
          (max, row) => Math.max(max, row.id),
          cursor.current,
        );
        setState((prev) => ({
          ...prev,
          listings: fresh.listings.length > 0
            ? prev.listings.concat(fresh.listings)
            : prev.listings,
          clients: fresh.clients,
          phase: fresh.done ? 'done' : prev.phase,
        }));

        if (fresh.done) return; // no reschedule: the search is over
        schedule(POLL_MS);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof SearchExpiredError) {
          setState((prev) => ({ ...prev, phase: 'expired' }));
          return;
        }
        // A cancelled request is our own doing (unmount), not a failure.
        if (err instanceof ApiError && err.status === 0 && err.message === 'cancelled') {
          return;
        }

        failures.current += 1;
        if (failures.current >= FAILURES_BEFORE_ERROR) {
          setState((prev) => ({
            ...prev,
            phase: 'error',
            error: err instanceof Error ? err.message : 'Search failed.',
          }));
          return;
        }
        // Exponential backoff: 1.2s, 2.4s, 4.8s… capped.
        schedule(Math.min(POLL_MS * 2 ** failures.current, MAX_BACKOFF_MS));
      }
    };

    poll();

    // Foregrounding shouldn't wait out the remainder of a sleeping timer —
    // poll immediately so the screen catches up the moment it's visible.
    const sub = AppState.addEventListener('change', (next) => {
      if (next === 'active' && !cancelled && timer) {
        clearTimeout(timer);
        schedule(0);
      }
    });

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      inFlight.abort();
      sub.remove();
    };
  }, [searchId]);

  const newCount = state.listings.reduce((n, row) => n + (row.is_new ? 1 : 0), 0);
  return {
    phase: state.phase,
    listings: state.listings,
    clients: state.clients,
    error: state.error,
    newCount,
  };
}

/** Starts a search and hands back its id. Kept separate from the polling hook
 *  so a failed start is a plain, retryable action rather than a phase of a
 *  search that never existed. */
export function useStartSearch() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = useCallback(
    async (vertical: Parameters<typeof api.startSearch>[0],
           criteria: Record<string, unknown>): Promise<number | null> => {
      setPending(true);
      setError(null);
      try {
        const { search_id } = await api.startSearch(vertical, criteria);
        return search_id;
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not start the search.');
        return null;
      } finally {
        setPending(false);
      }
    },
    [],
  );

  return { start, pending, error };
}
