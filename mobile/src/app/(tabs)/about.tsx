import { ScrollView, StyleSheet, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { API_URL, CLIENT_VERSION } from '@/api/client';
import { useMeta } from '@/api/queries';
import { ThemedText } from '@/components/themed-text';
import { ThemedView } from '@/components/themed-view';
import { MaxContentWidth, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

/**
 * What the app is, what it stores, and where the numbers come from.
 *
 * Not boilerplate: an app in this category gets read closely by reviewers on
 * both stores, and the honest answers here are genuinely good ones — no
 * account, no tracking, no transactions, and market figures whose limits are
 * stated rather than implied. It doubles as the privacy disclosure the Play
 * data-safety form and Apple's privacy label have to agree with.
 */
export default function AboutScreen() {
  const theme = useTheme();
  const meta = useMeta();

  return (
    <ThemedView style={styles.flex}>
      <SafeAreaView style={styles.flex} edges={['top']}>
        <ScrollView contentContainerStyle={styles.content}>
          <ThemedText type="subtitle">About</ThemedText>

          <Section title="What this is">
            <ThemedText type="small" themeColor="textSecondary">
              Gun Scout searches public firearm, ammunition and parts listings
              across several retail and marketplace sites at once, and reports
              what comparable items are actually listed for. It is a price
              research tool. No sale, transfer, payment or transaction of any
              kind happens in this app, and it is not affiliated with the sites
              it reads.
            </ThemedText>
          </Section>

          <Section title="What it stores about you">
            <ThemedText type="small" themeColor="textSecondary">
              Nothing on our side. There is no account and no sign-in. Your
              recent searches are kept on this device only and never sent
              anywhere; clearing the app&apos;s data removes them. The server
              keeps no search history and nothing that identifies you.
            </ThemedText>
          </Section>

          <Section title="Where the numbers come from">
            <ThemedText type="small" themeColor="textSecondary">
              As searches run, each listing seen is reduced to an anonymous
              fact — brand, model, caliber, condition, one price — and folded
              into a shared market picture. Listings themselves aren&apos;t
              stored. Live auction bids are excluded from price statistics,
              because a bid is a floor rather than a price; when a
              GunBroker auction closes, its final hammer price is recorded
              instead. Bundles and law-enforcement trade-ins are flagged and
              held out of price stats by default, since both skew a median.
            </ThemedText>
          </Section>

          <Section title="Coverage is uneven, on purpose">
            <ThemedText type="small" themeColor="textSecondary">
              Some retailers block automated access. When a source can&apos;t be
              reached it is reported as unavailable next to your results rather
              than being quietly dropped, so an empty result never masquerades
              as a complete one. Prices and availability change constantly —
              always confirm on the retailer&apos;s own site.
            </ThemedText>
          </Section>

          <Section title="Legal">
            <ThemedText type="small" themeColor="textSecondary">
              Firearm and ammunition purchases are subject to federal, state and
              local law, including transfer through a licensed dealer where
              required. Nothing here is legal advice, and availability shown in
              this app does not mean an item may lawfully be sold or shipped to
              you.
            </ThemedText>
          </Section>

          <View style={[styles.meta, { borderTopColor: theme.border }]}>
            <ThemedText type="small" themeColor="muted">
              Version {CLIENT_VERSION}
              {meta.data ? ` · API v${meta.data.api_version}` : ''}
            </ThemedText>
            <ThemedText type="small" themeColor="muted">{API_URL}</ThemedText>
          </View>
        </ScrollView>
      </SafeAreaView>
    </ThemedView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={styles.section}>
      <ThemedText type="smallBold">{title}</ThemedText>
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  content: {
    padding: Spacing.three,
    paddingBottom: Spacing.six,
    gap: Spacing.three,
    width: '100%',
    maxWidth: MaxContentWidth,
    alignSelf: 'center',
  },
  section: { gap: Spacing.two, marginTop: Spacing.three },
  meta: {
    marginTop: Spacing.five,
    paddingTop: Spacing.three,
    borderTopWidth: StyleSheet.hairlineWidth,
    gap: Spacing.one,
  },
});
