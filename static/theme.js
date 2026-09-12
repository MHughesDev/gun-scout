/* Theme switch shared by every page.
   Three states: "light", "dark", and unset — unset follows the OS, which is
   what glass.css assumes when no data-theme attribute is present. The choice
   is stamped on <html> as early as possible so the page never flashes the
   wrong palette. */
(function () {
  const KEY = 'gs_theme';
  let stored = null;
  try { stored = localStorage.getItem(KEY); } catch (e) { /* private mode — session-only */ }
  if (stored === 'light' || stored === 'dark') {
    document.documentElement.setAttribute('data-theme', stored);
  }

  function prefersDark() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  function current() {
    return document.documentElement.getAttribute('data-theme') || (prefersDark() ? 'dark' : 'light');
  }
  function apply(mode) {
    document.documentElement.setAttribute('data-theme', mode);
    try { localStorage.setItem(KEY, mode); } catch (e) { /* nothing to persist to */ }
    // the ballistics canvas paints its grid from CSS vars, so let it repaint
    window.dispatchEvent(new CustomEvent('themechange', {detail: {theme: mode}}));
  }

  window.toggleTheme = () => apply(current() === 'dark' ? 'light' : 'dark');

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.theme-toggle').forEach(btn => {
      btn.setAttribute('aria-label', 'Switch between light and dark');
      btn.onclick = window.toggleTheme;
    });
  });
})();
