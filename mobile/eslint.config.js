// Expo's shared flat config: it wires up the React, React Hooks and
// React Native rules that catch the mistakes this codebase is most exposed to
// — a missing hook dependency in the search polling loop, or a hook called
// conditionally in a screen that branches on load state.
const { defineConfig } = require('eslint/config');
const expoConfig = require('eslint-config-expo/flat');

module.exports = defineConfig([
  expoConfig,
  {
    ignores: ['dist/*', 'node_modules/*', '.expo/*'],
  },
]);
