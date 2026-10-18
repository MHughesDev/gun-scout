import { useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { useMeta, useStats } from '@/api/queries';
import type { StatsGroupRow, VerticalId } from '@/api/types';
import { ThemedText } from '@/components/themed-text';
import { ThemedView } from '@/components/themed-view';
import { MaxContentWidth, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

/**
 * Market statistics — what things actually sell for, grouped by whatever
 * dimension the user picks.
 *
 * This is the half of Gun Scout that is purely informational, and it is the
 * app's centre of gravity on mobile: it answers "is this a fair price?"
 * without any transaction, and it works identically whether or not listing
 * links are enabled (docs/mobile-platform-plan.md §2).
 *
 * The numbers come from anonymous facts the backend accumulates as searches
 * run — no listings, no URLs, no history. `priced` is shown next to every
 * median because live auction bids and unpriced listings are excluded from
 * price stats, so a group's count is not its sample size.
 */
export default function MarketScreen() {
  const theme = useTheme();
  const meta = useMeta();
  const [vertical, setVertical] = useState<VerticalId>('guns');
  const [groupBy, setGroupBy] = useState<string | null>(null);

  const stats = useStats(vertical, groupBy ? { group_by: groupBy } : {});
  const data = stats.data;

  const money = (value: number | null | undefined) =>
    value == null ? '—' : value < 1 ? `$${value.toFixed(3)}` : `$${Math.round(value).toLocaleString('en-US')}`;

  return (
    <ThemedView style={styles.flex}>
      <SafeAreaView style={styles.flex} edges={['top']}>
        <ScrollView contentContainerStyle={styles.content}>
          <ThemedText type="subtitle">Market</ThemedText>

          <View style={styles.chips}>
            {(meta.data?.verticals ?? []).map((v) => {
              const selected = v.id === vertical;
              return (
                <Pressable
                  key={v.id}
                  accessibilityRole="tab"
                  accessibilityState={{ selected }}
                  onPress={() => {
                    setVertical(v.id);
                    // Group dimensions are per-vertical; carrying one across
                    // would ask the server for a grouping it doesn't have.
                    setGroupBy(null);
                  }}
                  style={[styles.chip,
                    { borderColor: selected ? theme.accent : theme.border },
                    selected && { backgroundColor: theme.backgroundSelected }]}>
                  <ThemedText type="smallBold"
                    themeColor={selected ? 'text' : 'textSecondary'}>
                    {v.label}
                  </ThemedText>
                </Pressable>
              );
            })}
          </View>

          {stats.isLoading && <ActivityIndicator style={styles.loading} />}

          {stats.isError && (
            <View style={[styles.notice, { borderColor: theme.danger }]}>
              <ThemedText type="small" themeColor="danger">
                Couldn&apos;t load market stats.
              </ThemedText>
              <Pressable accessibilityRole="button" onPress={() => stats.refetch()}>
                <ThemedText type="smallBold" themeColor="accent">Try again</ThemedText>
              </Pressable>
            </View>
          )}

          {data && !data.error && (
            <>
              <View style={[styles.overall, { borderColor: theme.border }]}>
                <ThemedText type="smallBold">
                  {data.matched.toLocaleString('en-US')} listings observed
                </ThemedText>
                <ThemedText type="small" themeColor="textSecondary">
                  median {data.price_label} {money(data.overall.median)} · range{' '}
                  {money(data.overall.p25)}–{money(data.overall.p75)} (middle half) ·{' '}
                  {data.overall.priced.toLocaleString('en-US')} priced
                </ThemedText>
              </View>

              <ScrollView horizontal showsHorizontalScrollIndicator={false}
                contentContainerStyle={styles.chips}>
                {data.group_choices.map((choice) => {
                  const selected = choice === data.group_by;
                  return (
                    <Pressable
                      key={choice}
                      accessibilityRole="button"
                      accessibilityState={{ selected }}
                      onPress={() => setGroupBy(choice)}
                      style={[styles.chip,
                        { borderColor: selected ? theme.accent : theme.border },
                        selected && { backgroundColor: theme.backgroundSelected }]}>
                      <ThemedText type="small"
                        themeColor={selected ? 'text' : 'textSecondary'}>
                        {choice.replace(/_/g, ' ')}
                      </ThemedText>
                    </Pressable>
                  );
                })}
              </ScrollView>

              {data.groups.map((row) => (
                <GroupRow key={row.key} row={row} money={money} />
              ))}

              {data.truncated_groups > 0 && (
                <ThemedText type="small" themeColor="muted" style={styles.footnote}>
                  + {data.truncated_groups} smaller groups not shown
                </ThemedText>
              )}

              <ThemedText type="small" themeColor="muted" style={styles.footnote}>
                Prices exclude live auction bids — a bid is a floor, not a
                price. Bundles are excluded too
                {data.bundles_excluded_from_prices > 0
                  ? ` (${data.bundles_excluded_from_prices} here)`
                  : ''}
                .
              </ThemedText>
            </>
          )}

          {data?.error && (
            <ThemedText type="small" themeColor="danger">{data.error}</ThemedText>
          )}
        </ScrollView>
      </SafeAreaView>
    </ThemedView>
  );
}

function GroupRow({ row, money }: {
  row: StatsGroupRow;
  money: (value: number | null | undefined) => string;
}) {
  const theme = useTheme();
  return (
    <View style={[styles.row, { borderBottomColor: theme.border }]}>
      <View style={styles.rowMain}>
        <ThemedText type="small" numberOfLines={1} style={styles.rowKey}>{row.key}</ThemedText>
        <ThemedText type="smallBold">{money(row.median)}</ThemedText>
      </View>
      <ThemedText type="small" themeColor="muted">
        {row.count.toLocaleString('en-US')} seen · {row.share}% · {row.priced} priced
        {row.priced > 0 ? ` · ${money(row.p25)}–${money(row.p75)}` : ''}
      </ThemedText>
    </View>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  content: {
    padding: Spacing.three,
    paddingBottom: Spacing.six,
    gap: Spacing.two,
    width: '100%',
    maxWidth: MaxContentWidth,
    alignSelf: 'center',
  },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: Spacing.two, marginVertical: Spacing.two },
  chip: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 999,
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    marginRight: Spacing.two,
  },
  overall: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 12,
    padding: Spacing.three,
    gap: Spacing.one,
    marginVertical: Spacing.two,
  },
  notice: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 10,
    padding: Spacing.three,
    gap: Spacing.two,
  },
  loading: { marginVertical: Spacing.four },
  row: { paddingVertical: Spacing.three, borderBottomWidth: StyleSheet.hairlineWidth, gap: 2 },
  rowMain: { flexDirection: 'row', justifyContent: 'space-between', gap: Spacing.three },
  rowKey: { flex: 1 },
  footnote: { marginTop: Spacing.three },
});
