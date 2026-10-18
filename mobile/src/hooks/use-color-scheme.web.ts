import { useSyncExternalStore } from 'react';
import { useColorScheme as useRNColorScheme } from 'react-native';

/**
 * Web build only. The static export is rendered without a client, so the
 * user's colour scheme is unknowable at that point — it has to be recomputed
 * once hydration happens, or the markup the server produced won't match what
 * the browser renders.
 *
 * useSyncExternalStore is React's own answer to "am I hydrated yet": the
 * server snapshot returns false, the client snapshot true, and React handles
 * the transition without an effect that sets state on mount.
 */
const subscribe = () => () => {};

export function useColorScheme() {
  const hydrated = useSyncExternalStore(
    subscribe,
    () => true,   // client
    () => false,  // server / static render
  );
  const colorScheme = useRNColorScheme();
  return hydrated ? colorScheme : 'light';
}
