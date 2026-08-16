import { useState } from 'react';
import { Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import type { InputField, SelectOption } from '@/api/types';

/**
 * Renders one server-described filter.
 *
 * The search form isn't written in this app — `/api/v1/<vertical>/schema`
 * describes it, and this component turns one field description into controls.
 * That's what lets the backend add a filter (or a whole vertical) and have it
 * appear in builds that shipped months earlier. The cost is that every field
 * type the server can emit must be handled here, including ones no vertical
 * uses yet, so an unknown type degrades to a plain text box rather than
 * rendering nothing and silently dropping the filter.
 */

export type Criteria = Record<string, string | boolean>;

interface Props {
  field: InputField;
  criteria: Criteria;
  presets: Record<string, string[]>;
  onChange: (patch: Criteria) => void;
}

function optionValue(option: SelectOption | string): string {
  return typeof option === 'string' ? option : option.value;
}

function optionLabel(option: SelectOption | string): string {
  return typeof option === 'string' ? option : option.label;
}

export function SchemaField({ field, criteria, presets, onChange }: Props) {
  const theme = useTheme();
  const suggestions = field.presets ? (presets[field.presets] ?? []) : [];

  const label = (
    <ThemedText type="smallBold" style={styles.label}>
      {field.label}
    </ThemedText>
  );

  if (field.type === 'select') {
    const options = field.options ?? [];
    const current = String(criteria[field.id] ?? field.default ?? '');
    return (
      <View style={styles.field}>
        {label}
        <View style={styles.chips}>
          {options.map((option) => {
            const value = optionValue(option);
            const selected = current === value;
            return (
              <Pressable
                key={value || '(any)'}
                onPress={() => onChange({ [field.id]: value })}
                accessibilityRole="radio"
                accessibilityState={{ selected }}
                style={[
                  styles.chip,
                  { borderColor: selected ? theme.accent : theme.border },
                  selected && { backgroundColor: theme.backgroundSelected },
                ]}>
                <ThemedText type="small" themeColor={selected ? 'text' : 'textSecondary'}>
                  {optionLabel(option)}
                </ThemedText>
              </Pressable>
            );
          })}
        </View>
      </View>
    );
  }

  if (field.type === 'range') {
    // A range writes two separate criteria keys (e.g. price_min/price_max),
    // which is why it can't be modelled as one value.
    const minId = field.min_id ?? `${field.id}_min`;
    const maxId = field.max_id ?? `${field.id}_max`;
    return (
      <View style={styles.field}>
        {label}
        <View style={styles.row}>
          <TextInput
            value={String(criteria[minId] ?? '')}
            onChangeText={(text) => onChange({ [minId]: text })}
            placeholder={field.min_placeholder || 'min'}
            placeholderTextColor={theme.muted}
            keyboardType="numeric"
            inputMode="numeric"
            style={[styles.input, styles.half,
              { color: theme.text, borderColor: theme.border,
                backgroundColor: theme.backgroundElement }]}
          />
          <TextInput
            value={String(criteria[maxId] ?? '')}
            onChangeText={(text) => onChange({ [maxId]: text })}
            placeholder={field.max_placeholder || 'max'}
            placeholderTextColor={theme.muted}
            keyboardType="numeric"
            inputMode="numeric"
            style={[styles.input, styles.half,
              { color: theme.text, borderColor: theme.border,
                backgroundColor: theme.backgroundElement }]}
          />
        </View>
      </View>
    );
  }

  if (field.type === 'checkbox') {
    const checked = criteria[field.id] === true;
    return (
      <Pressable
        style={[styles.field, styles.row]}
        accessibilityRole="checkbox"
        accessibilityState={{ checked }}
        onPress={() => onChange({ [field.id]: !checked })}>
        <View
          style={[styles.box,
            { borderColor: checked ? theme.accent : theme.border },
            checked && { backgroundColor: theme.accent }]}
        />
        <ThemedText type="small">{field.label}</ThemedText>
      </Pressable>
    );
  }

  // text and combo. A combo is a text box plus tappable suggestions: the
  // server's presets are hints, never a closed list — a caliber or brand it
  // hasn't heard of still has to be searchable.
  return (
    <ComboField
      field={field}
      value={String(criteria[field.id] ?? '')}
      suggestions={suggestions}
      onChange={(value) => onChange({ [field.id]: value })}
      label={label}
    />
  );
}

function ComboField({
  field,
  value,
  suggestions,
  onChange,
  label,
}: {
  field: InputField;
  value: string;
  suggestions: string[];
  onChange: (value: string) => void;
  label: React.ReactNode;
}) {
  const theme = useTheme();
  const [focused, setFocused] = useState(false);

  // Suggestions are only worth the screen space while the field is being
  // edited, and only the ones that match what's typed so far.
  const matches = focused
    ? suggestions
        .filter((s) => !value || s.toLowerCase().includes(value.toLowerCase()))
        .slice(0, 12)
    : [];

  return (
    <View style={styles.field}>
      {label}
      <TextInput
        value={value}
        onChangeText={onChange}
        onFocus={() => setFocused(true)}
        // Delayed so a tap on a suggestion registers before the list closes.
        onBlur={() => setTimeout(() => setFocused(false), 150)}
        placeholder={field.placeholder}
        placeholderTextColor={theme.muted}
        autoCapitalize="none"
        autoCorrect={false}
        returnKeyType="search"
        style={[styles.input,
          { color: theme.text, borderColor: theme.border,
            backgroundColor: theme.backgroundElement }]}
      />
      {matches.length > 0 && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false}
          keyboardShouldPersistTaps="handled" style={styles.suggestions}>
          {matches.map((suggestion) => (
            <Pressable
              key={suggestion}
              onPress={() => onChange(suggestion)}
              style={[styles.chip, { borderColor: theme.border }]}>
              <ThemedText type="small" themeColor="textSecondary">
                {suggestion}
              </ThemedText>
            </Pressable>
          ))}
        </ScrollView>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  field: { marginBottom: Spacing.three },
  label: { marginBottom: Spacing.two },
  row: { flexDirection: 'row', alignItems: 'center', gap: Spacing.two },
  half: { flex: 1 },
  input: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 10,
    paddingHorizontal: Spacing.three,
    // 44pt is the minimum comfortable touch target on both platforms.
    minHeight: 44,
    fontSize: 16,
  },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: Spacing.two },
  chip: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 999,
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    marginRight: Spacing.two,
  },
  suggestions: { marginTop: Spacing.two },
  box: { width: 22, height: 22, borderRadius: 6, borderWidth: 2 },
});
