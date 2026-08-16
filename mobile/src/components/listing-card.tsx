import { Image } from 'expo-image';
import { openBrowserAsync } from 'expo-web-browser';
import { memo } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import type { Listing } from '@/api/types';

/**
 * One result row.
 *
 * Two rules here are not styling decisions and should not be "simplified":
 *
 *  1. **A bid is not a price.** The backend keeps `price` (firm/buy-now) and
 *     `current_bid` in separate fields precisely because a live auction's bid
 *     is a lower bound, not what the thing costs. They are labelled
 *     differently and never coalesced.
 *  2. **The card is only tappable when the server sent a URL.** With the
 *     store-compliance flag off, `url` arrives empty and the card is inert —
 *     it presents market information and no way to transact. See
 *     docs/mobile-platform-plan.md §2.
 */

function money(value: number | null | undefined): string | null {
  if (value == null) return null;
  // Cost-per-round is a sub-dollar figure where cents matter; item prices
  // read better whole.
  return value < 1
    ? `$${value.toFixed(3)}`
    : `$${Math.round(value).toLocaleString('en-US')}`;
}

export const ListingCard = memo(function ListingCard({ listing }: { listing: Listing }) {
  const theme = useTheme();
  const price = money(listing.price);
  const bid = money(listing.current_bid);
  const perRound = money(listing.price_per_round);

  const details = [
    listing.condition,
    listing.caliber_canon || listing.caliber,
    listing.barrel_length ? `${listing.barrel_length}" bbl` : null,
    listing.capacity,
    listing.round_count ? `${listing.round_count} rds` : null,
  ].filter(Boolean);

  const openable = Boolean(listing.url);

  const body = (
    <View style={[styles.card, { borderColor: theme.border }]}>
      {listing.image ? (
        <Image
          source={{ uri: listing.image }}
          style={styles.image}
          contentFit="contain"
          transition={120}
          // Listing images are remote and often large; caching them keeps a
          // scroll back through results off the network entirely.
          cachePolicy="memory-disk"
        />
      ) : (
        <View style={[styles.image, { backgroundColor: theme.backgroundElement }]} />
      )}

      <View style={styles.body}>
        <View style={styles.titleRow}>
          {listing.is_new && (
            <View style={[styles.badge, { backgroundColor: theme.ok }]}>
              <ThemedText type="small" style={styles.badgeText}>NEW</ThemedText>
            </View>
          )}
          <ThemedText type="small" numberOfLines={2} style={styles.title}>
            {listing.title}
          </ThemedText>
        </View>

        {details.length > 0 && (
          <ThemedText type="small" themeColor="textSecondary" numberOfLines={1}>
            {details.join(' · ')}
          </ThemedText>
        )}

        <View style={styles.priceRow}>
          {price && <ThemedText type="smallBold">{price}</ThemedText>}
          {bid && (
            <ThemedText type="small" themeColor="warn">
              bid {bid}
              {listing.bid_count ? ` (${listing.bid_count})` : ''}
            </ThemedText>
          )}
          {perRound && (
            <ThemedText type="small" themeColor="textSecondary">{perRound}/rd</ThemedText>
          )}
          <ThemedText type="small" themeColor="muted" style={styles.site}>
            {listing.site}
          </ThemedText>
        </View>
      </View>
    </View>
  );

  if (!openable) return body;

  return (
    <Pressable
      accessibilityRole="link"
      accessibilityLabel={`${listing.title} on ${listing.site}`}
      // In-app browser rather than a hand-off: the user keeps their place in
      // the results, and returning is a swipe instead of an app switch.
      onPress={() => openBrowserAsync(listing.url).catch(() => undefined)}>
      {body}
    </Pressable>
  );
});

const styles = StyleSheet.create({
  card: {
    flexDirection: 'row',
    gap: Spacing.three,
    padding: Spacing.three,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  image: { width: 76, height: 76, borderRadius: 8 },
  body: { flex: 1, gap: Spacing.one },
  titleRow: { flexDirection: 'row', alignItems: 'flex-start', gap: Spacing.two },
  title: { flex: 1 },
  badge: { borderRadius: 4, paddingHorizontal: Spacing.two, paddingVertical: 1 },
  badgeText: { color: '#ffffff', fontSize: 11, fontWeight: '700' },
  priceRow: {
    flexDirection: 'row',
    alignItems: 'baseline',
    gap: Spacing.three,
    marginTop: Spacing.one,
  },
  site: { marginLeft: 'auto' },
});
