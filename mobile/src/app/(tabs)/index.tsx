import { router } from 'expo-router';
import { useState } from 'react';
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { useMeta, useSchema } from '@/api/queries';
import type { VerticalId } from '@/api/types';
import { SchemaField, type Criteria } from '@/components/schema-field';
import { ThemedText } from '@/components/themed-text';
import { ThemedView } from '@/components/themed-view';
import { MaxContentWidth, Spacing } from '@/constants/theme';
import { useStartSearch } from '@/hooks/use-live-search';
import { useRecentSearches } from '@/hooks/use-recent-searches';
import { useTheme } from '@/hooks/use-theme';

/**
 * The search screen. Which engines exist and what each one's form contains are
 * both server-supplied (`/meta`, `/<vertical>/schema`), so this file describes
 * layout and interaction only — it holds no knowledge of guns, ammo or parts.
 */
export default function SearchScreen() {
  const theme = useTheme();
  const meta = useMeta();
  const [vertical, setVertical] = useState<VerticalId>('guns');
  const schema = useSchema(vertical);
  const [criteria, setCriteria] = useState<Criteria>({});
  const { start, pending, error } = useStartSearch();
  const { recent, remember } = useRecentSearches();

  const patch = (next: Criteria) => setCriteria((prev) => ({ ...prev, ...next }));

  const switchVertical = (next: VerticalId) => {
    // Criteria are per-vertical (a barrel length means nothing to the ammo
    // engine), so switching clears rather than carrying values across.
    setVertical(next);
    setCriteria({});
  };

  const describe = (): string => {
    const parts = [criteria.keyword, criteria.manufacturer, criteria.caliber]
      .filter((v) => typeof v === 'string' && v.trim().length > 0);
    return parts.length > 0 ? parts.join(' ') : `All ${vertical}`;
  };

  const run = async () => {
    // Blanks are dropped rather than sent: the backend treats "" as "no
    // filter" anyway, and an empty payload keeps the request small.
    const payload = Object.fromEntries(
      Object.entries(criteria).filter(([, v]) => v !== '' && v !== false && v != null),
    );
    const searchId = await start(vertical, payload);
    if (searchId == null) return;
    remember(vertical, payload, describe());
    router.push({ pathname: '/results', params: { id: String(searchId), vertical } });
  };

  const verticals = meta.data?.verticals ?? [];

  return (
    <ThemedView style={styles.flex}>
      <SafeAreaView style={styles.flex} edges={['top']}>
        <KeyboardAvoidingView
          style={styles.flex}
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          <ScrollView
            contentContainerStyle={styles.content}
            keyboardShouldPersistTaps="handled"
            keyboardDismissMode="on-drag">
            <ThemedText type="subtitle">Gun Scout</ThemedText>

            {verticals.length > 0 && (
              <View style={styles.verticals}>
                {verticals.map((v) => {
                  const selected = v.id === vertical;
                  return (
                    <Pressable
                      key={v.id}
                      onPress={() => switchVertical(v.id)}
                      accessibilityRole="tab"
                      accessibilityState={{ selected }}
                      style={[styles.verticalTab,
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
            )}

            {/* A source being unreachable is worth saying up front, not after
                a search comes back half-empty. */}
            {meta.data?.features.worker_online === false && (
              <View style={[styles.notice, { borderColor: theme.warn }]}>
                <ThemedText type="small" themeColor="warn">
                  Some retailers are offline right now. Results will come from the
                  sources that answer directly.
                </ThemedText>
              </View>
            )}

            {schema.isLoading && <ActivityIndicator style={styles.loading} />}

            {schema.isError && (
              <View style={[styles.notice, { borderColor: theme.danger }]}>
                <ThemedText type="small" themeColor="danger">
                  Couldn&apos;t load the search filters.
                </ThemedText>
                <Pressable onPress={() => schema.refetch()} accessibilityRole="button">
                  <ThemedText type="smallBold" themeColor="accent">Try again</ThemedText>
                </Pressable>
              </View>
            )}

            {schema.data?.inputs.map((field) => (
              <SchemaField
                key={field.id}
                field={field}
                criteria={criteria}
                presets={schema.data.presets ?? {}}
                onChange={patch}
              />
            ))}

            {error && (
              <ThemedText type="small" themeColor="danger" style={styles.error}>
                {error}
              </ThemedText>
            )}

            {recent.length > 0 && (
              <View style={styles.recent}>
                <ThemedText type="smallBold" themeColor="textSecondary">Recent</ThemedText>
                {recent.slice(0, 5).map((item) => (
                  <Pressable
                    key={`${item.at}`}
                    accessibilityRole="button"
                    onPress={() => {
                      switchVertical(item.vertical);
                      setCriteria(item.criteria as Criteria);
                    }}
                    style={[styles.recentRow, { borderColor: theme.border }]}>
                    <ThemedText type="small" numberOfLines={1}>{item.label}</ThemedText>
                    <ThemedText type="small" themeColor="muted">{item.vertical}</ThemedText>
                  </Pressable>
                ))}
              </View>
            )}
          </ScrollView>

          <View style={[styles.footer, { borderTopColor: theme.border }]}>
            <Pressable
              accessibilityRole="button"
              disabled={pending || !schema.data}
              onPress={run}
              style={[styles.button,
                { backgroundColor: theme.accent, opacity: pending || !schema.data ? 0.5 : 1 }]}>
              {pending
                ? <ActivityIndicator color="#ffffff" />
                : <ThemedText type="smallBold" style={styles.buttonText}>Search</ThemedText>}
            </Pressable>
          </View>
        </KeyboardAvoidingView>
      </SafeAreaView>
    </ThemedView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  content: {
    padding: Spacing.three,
    paddingBottom: Spacing.five,
    gap: Spacing.two,
    width: '100%',
    maxWidth: MaxContentWidth,
    alignSelf: 'center',
  },
  verticals: { flexDirection: 'row', gap: Spacing.two, marginVertical: Spacing.three },
  verticalTab: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 999,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.two,
  },
  notice: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 10,
    padding: Spacing.three,
    marginBottom: Spacing.three,
    gap: Spacing.two,
  },
  loading: { marginVertical: Spacing.four },
  error: { marginTop: Spacing.two },
  recent: { marginTop: Spacing.four, gap: Spacing.two },
  recentRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    gap: Spacing.three,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 10,
    padding: Spacing.three,
  },
  footer: { padding: Spacing.three, borderTopWidth: StyleSheet.hairlineWidth },
  button: {
    minHeight: 50,
    borderRadius: 12,
    alignItems: 'center',
    justifyContent: 'center',
    width: '100%',
    maxWidth: MaxContentWidth,
    alignSelf: 'center',
  },
  buttonText: { color: '#ffffff', fontSize: 16 },
});
