import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { DarkTheme, DefaultTheme, Stack, ThemeProvider } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';
import { useState } from 'react';
import { useColorScheme } from 'react-native';

import { ApiVersionError, NetworkError } from '@/api/client';

SplashScreen.preventAutoHideAsync();

/**
 * Root layout: a stack whose first screen is the tab bar, plus the app-wide
 * query client.
 *
 * The retry policy is the only interesting thing here. Defaults that are fine
 * on a desktop are wrong on a phone in a parking garage — and two error
 * classes must never be retried at all: a 426 (this build is retired) can
 * only ever fail again, and a 4xx from our own API means the request was
 * wrong, not unlucky.
 */
function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (failureCount, error) => {
          if (error instanceof ApiVersionError) return false;
          if (error instanceof NetworkError) return failureCount < 3;
          const status = (error as { status?: number }).status ?? 0;
          if (status >= 400 && status < 500) return false;
          return failureCount < 2;
        },
        retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 8000),
        // Cellular round trips are expensive; don't refetch just because the
        // user glanced at another app.
        refetchOnWindowFocus: false,
      },
    },
  });
}

export default function RootLayout() {
  const colorScheme = useColorScheme();
  // Lazy initial state, not module scope: one client for the life of the app,
  // created on first render rather than at import time, so a Fast Refresh in
  // development doesn't strand queries against a discarded one.
  const [queryClient] = useState(makeQueryClient);

  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider value={colorScheme === 'dark' ? DarkTheme : DefaultTheme}>
        <Stack>
          <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
          <Stack.Screen name="results" options={{ title: 'Results' }} />
        </Stack>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
