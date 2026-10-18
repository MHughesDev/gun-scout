import { ScrollView, StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import type { ClientState, ClientStatus } from '@/api/types';

/**
 * Per-site progress while a search runs.
 *
 * Worth the space because "0 results" has several very different causes here,
 * and the user is owed the difference: the site genuinely had nothing, or it
 * refused a datacenter IP (`blocked`), or the operator's worker is offline so
 * it was never asked (`unavailable`), or our parser no longer matches the
 * site's markup (`schema` — the only one that means Gun Scout is at fault).
 * Collapsing those into a single empty state would quietly turn a broken
 * scraper into "no guns match your search".
 */

const LABELS: Record<ClientStatus, string> = {
  queued: 'queued',
  running: 'searching',
  done: 'done',
  blocked: 'blocked',
  schema: 'needs update',
  error: 'error',
  unavailable: 'unavailable',
};

export function SiteStatus({ clients, siteLabels }: {
  clients: ClientState[];
  siteLabels: Record<string, string>;
}) {
  const theme = useTheme();
  if (clients.length === 0) return null;

  const colorFor = (status: ClientStatus) => {
    switch (status) {
      case 'done':
        return theme.ok;
      case 'running':
      case 'queued':
        return theme.accent;
      case 'schema':
        return theme.schema;
      case 'blocked':
      case 'unavailable':
        return theme.warn;
      default:
        return theme.danger;
    }
  };

  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      contentContainerStyle={styles.bar}>
      {clients.map((client) => (
        <View
          key={client.site}
          style={[styles.pill, { borderColor: theme.border,
            backgroundColor: theme.backgroundElement }]}
          accessibilityRole="text"
          accessibilityLabel={
            `${siteLabels[client.site] ?? client.site}: ${LABELS[client.status]}, ` +
            `${client.found} found${client.message ? `. ${client.message}` : ''}`
          }>
          <View style={[styles.dot, { backgroundColor: colorFor(client.status) }]} />
          <ThemedText type="small">{siteLabels[client.site] ?? client.site}</ThemedText>
          <ThemedText type="small" themeColor="textSecondary">
            {LABELS[client.status]} · {client.found}
          </ThemedText>
        </View>
      ))}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  bar: { gap: Spacing.two, paddingHorizontal: Spacing.three, paddingVertical: Spacing.two },
  pill: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 999,
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
  },
  dot: { width: 8, height: 8, borderRadius: 4 },
});
