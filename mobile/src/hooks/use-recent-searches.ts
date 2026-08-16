import AsyncStorage from '@react-native-async-storage/async-storage';
import { useCallback, useEffect, useState } from 'react';

import type { VerticalId } from '@/api/types';

/**
 * Recent searches, stored on the device only.
 *
 * This mirrors a deliberate property of the web app: search history never
 * reaches the server. The backend stores no user identity and no query log —
 * the only thing it persists is anonymous market facts — and that stays true
 * on mobile because history lives here, in the app's own sandbox, as input
 * values and nothing else. It also keeps the Play data-safety form and
 * Apple's privacy nutrition label honest: no data collected, none shared.
 *
 * Capped, so a heavy user can't grow it without bound.
 */

const KEY = 'gun-scout:recent-searches:v1';
const MAX = 12;

export interface RecentSearch {
  vertical: VerticalId;
  criteria: Record<string, unknown>;
  /** Rendered as the row's label; derived at save time so the list needs no
   *  knowledge of what a given vertical's fields mean. */
  label: string;
  at: number;
}

export function useRecentSearches() {
  const [recent, setRecent] = useState<RecentSearch[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    AsyncStorage.getItem(KEY)
      .then((raw) => {
        if (!alive) return;
        setRecent(raw ? (JSON.parse(raw) as RecentSearch[]) : []);
      })
      // Corrupt or unreadable storage is not worth failing a launch over —
      // an empty history is a perfectly good starting state.
      .catch(() => undefined)
      .finally(() => alive && setLoaded(true));
    return () => {
      alive = false;
    };
  }, []);

  const remember = useCallback(
    (vertical: VerticalId, criteria: Record<string, unknown>, label: string) => {
      setRecent((prev) => {
        const key = JSON.stringify([vertical, criteria]);
        const next = [
          { vertical, criteria, label, at: Date.now() },
          ...prev.filter((r) => JSON.stringify([r.vertical, r.criteria]) !== key),
        ].slice(0, MAX);
        AsyncStorage.setItem(KEY, JSON.stringify(next)).catch(() => undefined);
        return next;
      });
    },
    [],
  );

  const clear = useCallback(() => {
    setRecent([]);
    AsyncStorage.removeItem(KEY).catch(() => undefined);
  }, []);

  return { recent, remember, clear, loaded };
}
