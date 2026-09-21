/* Theme switching, shared by the board and the profile.
 *
 * Loaded with `defer` BEFORE the page script, because the page script reads
 * the level colours out of CSS at startup: if the theme were applied after
 * that read, the globe would come up painted in the previous theme's palette
 * and stay that way until something forced a re-render.
 */
'use strict';

const THEMES = ['light', 'blue', 'sika', 'dark'];
const THEME_LABEL = {
  light: 'Light',
  blue: 'Blue',
  sika: 'Sika',
  dark: 'Dark',
};
const THEME_KEY = 'scrr.theme';

/* Browser storage is a per-viewer convenience and nothing more: it can throw
 * in a private window or with site data blocked, and the page has to render
 * correctly when it comes back empty. Hence the try/catch on both sides and
 * the Light default. */
function storedTheme() {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return THEMES.includes(value) ? value : null;
  } catch { return null; }
}

function storeTheme(name) {
  try { localStorage.setItem(THEME_KEY, name); } catch { /* not fatal */ }
}

function applyTheme(name) {
  const theme = THEMES.includes(name) ? name : 'light';
  document.documentElement.setAttribute('data-theme', theme);
  storeTheme(theme);
  document.querySelectorAll('.theme-btn').forEach((b) =>
    b.classList.toggle('is-on', b.dataset.theme === theme));
  // Anything that cached a CSS colour at startup re-reads it here. The globe
  // is the only such thing today, but the event is the contract, not the
  // caller: a listener is how a new consumer opts in without this module
  // needing to know it exists.
  document.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
  return theme;
}

function mountThemePicker(host) {
  if (!host) return;
  host.innerHTML = THEMES.map((name) => `
    <button type="button" class="theme-btn" data-theme="${name}"
            title="${THEME_LABEL[name]} theme" aria-label="${THEME_LABEL[name]} theme">
      <span class="theme-swatch theme-swatch--${name}"></span>
    </button>`).join('');
  host.addEventListener('click', (event) => {
    const button = event.target.closest('.theme-btn');
    if (button) applyTheme(button.dataset.theme);
  });
}

// Applied at parse time, before the page script reads any colour.
applyTheme(storedTheme() || 'light');
document.addEventListener('DOMContentLoaded', () => {
  mountThemePicker(document.getElementById('themes'));
  applyTheme(document.documentElement.getAttribute('data-theme'));
});
