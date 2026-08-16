import { router, useLocalSearchParams } from 'expo-router';
import { useMemo } from 'react';
import { ActivityIndicator, FlatList, Pressable, StyleSheet, View } from 'react-native';

import { useSites } from '@/api/queries';
import type { VerticalId } from '@/api/types';
import { ListingCard } from '@/components/listing-card';
import { SiteStatus } from '@/components/site-status';
import { ThemedText } from '@/components/themed-text';
import { ThemedView } from '@/components/themed-view';
import { Spacing } from '@/constants/theme';
import { useLiveSearch } from '@/hooks/use-live-search';
import { useTheme } from '@/hooks/use-theme';

/**
 * Live results for one search.
 *
 * Rows stream in over several seconds as each site reports, so this screen is
 * append-only and never re-sorts underneath the user mid-search — a list that
 * reorders while being read is worse than one that isn't sorted at all.
 */
export default function ResultsScreen() {
  const theme = useTheme();
  const params = useLocalSearchParams<{ id?: string; vertical?: string }>();
  const searchId = params.id ? Number(params.id) : null;
  const vertical = (params.vertical ?? 'guns') as VerticalId;

  const { phase, listings, clients, error, newCount } = useLiveSearch(
    Number.isFinite(searchId) ? searchId : null,
  );
  const sites = useSites(vertical);

  const siteLabels = useMemo(() => {
    const map: Record<string, string> = {};
    for (const site of sites.data ?? []) map[site.name] = site.label;
    return map;
  }, [sites.data]);

  const running = phase === 'running';

  return (
    <ThemedView style={styles.flex}>
      <SiteStatus clients={clients} siteLabels={siteLabels} />

      <View style={[styles.summary, { borderBottomColor: theme.border }]}>
        <ThemedText type="smallBold">
          {listings.length} {listings.length === 1 ? 'result' : 'results'}
        </ThemedText>
        {newCount > 0 && (
          <ThemedText type="small" themeColor="ok">{newCount} new</ThemedText>
        )}
        {running && <ActivityIndicator size="small" style={styles.spinner} />}
      </View>

      <FlatList
        data={listings}
        keyExtractor={(item) => `${item.id}`}
        renderItem={({ item }) => <ListingCard listing={item} />}
        // Results arrive in bursts; keeping a modest window mounted holds
        // memory flat on a long search without visible blanking on scroll.
        initialNumToRender={12}
        maxToRenderPerBatch={12}
        windowSize={7}
        removeClippedSubviews
        keyboardDismissMode="on-drag"
        ListEmptyComponent={
          <View style={styles.empty}>
            {running ? (
              <ThemedText type="small" themeColor="textSecondary">Searching…</ThemedText>
            ) : phase === 'expired' ? (
              <>
                <ThemedText type="small" themeColor="textSecondary">
                  This search expired. Results are held for about 15 minutes.
                </ThemedText>
                <Pressable accessibilityRole="button" onPress={() => router.back()}>
                  <ThemedText type="smallBold" themeColor="accent">Search again</ThemedText>
                </Pressable>
              </>
            ) : phase === 'error' ? (
              <ThemedText type="small" themeColor="danger">
                {error ?? 'The search stopped unexpectedly.'}
              </ThemedText>
            ) : (
              <ThemedText type="small" themeColor="textSecondary">
                No matches. Check the site statuses above — a source may have been
                unavailable rather than empty.
              </ThemedText>
            )}
          </View>
        }
      />
    </ThemedView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  summary: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.three,
    paddingHorizontal: Spacing.three,
    paddingBottom: Spacing.two,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  spinner: { marginLeft: 'auto' },
  empty: { padding: Spacing.five, alignItems: 'center', gap: Spacing.three },
});
