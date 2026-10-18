import type { ExpoConfig } from 'expo/config';

/**
 * Expo config as code, because two things about this app have to vary per
 * build and must never be edited by hand at release time:
 *
 *  - which backend it talks to (local Flask / Render / Cloud Run), and
 *  - `version`, which App Store Connect and Play both refuse to accept twice.
 *
 * Both come from the EAS build profile (eas.json), so a production binary
 * cannot accidentally ship pointing at a laptop.
 *
 * Bundle identifiers are deliberately committed rather than templated: they
 * are permanent. Once a build is uploaded under one, that string belongs to
 * this app on that store forever.
 */

const API_URL = process.env.EXPO_PUBLIC_API_URL ?? 'http://localhost:8777';

// Marketing version. Play and App Store Connect both reject a re-upload of a
// version already seen, so this moves every release. Build numbers are
// separate and handled by EAS (`autoIncrement`) — never by editing this file.
const VERSION = '0.1.0';

const config: ExpoConfig = {
  name: 'Gun Scout',
  slug: 'gun-scout',
  version: VERSION,
  orientation: 'portrait',
  icon: './assets/images/icon.png',
  scheme: 'gunscout',
  userInterfaceStyle: 'automatic',
  ios: {
    bundleIdentifier: 'com.gunscout.app',
    supportsTablet: true,
    infoPlist: {
      // The app talks to one HTTPS backend and opens links in the system
      // browser. No ATS exception is requested — a local http:// dev server
      // is reached through the dev client, which is exempt.
      ITSAppUsesNonExemptEncryption: false,
    },
  },
  android: {
    package: 'com.gunscout.app',
    adaptiveIcon: {
      backgroundColor: '#1B1D21',
      foregroundImage: './assets/images/android-icon-foreground.png',
      backgroundImage: './assets/images/android-icon-background.png',
      monochromeImage: './assets/images/android-icon-monochrome.png',
    },
    predictiveBackGestureEnabled: false,
    // No permissions requested. The app has no account, no location, no
    // camera, and stores nothing but the user's own recent search inputs —
    // worth keeping true, since every added permission is a data-safety
    // disclosure and a review question.
    permissions: [],
  },
  web: {
    output: 'static',
    favicon: './assets/images/favicon.png',
  },
  plugins: [
    'expo-router',
    [
      'expo-splash-screen',
      {
        backgroundColor: '#1B1D21',
        image: './assets/images/splash-icon.png',
        imageWidth: 76,
      },
    ],
  ],
  experiments: {
    typedRoutes: true,
    reactCompiler: true,
  },
  extra: {
    apiUrl: API_URL,
    // Read back at runtime as the client version sent to /api/v1, which is
    // what the server's minimum-version gate compares against.
    clientVersion: VERSION,
    eas: {
      // Filled in by `eas init` on first setup; committed afterwards.
      projectId: process.env.EAS_PROJECT_ID,
    },
  },
};

export default config;
