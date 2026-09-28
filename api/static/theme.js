/* Theme switching, shared by the board and the profile.
 *
 * Loaded with `defer` BEFORE the page script, because the page script reads
 * the level colours out of CSS at startup: if the theme were applied after
 * that read, the globe would come up painted in the previous theme's palette
 * and stay that way until something forced a re-render.
 */
'use strict';

// Three: a light one, a dark one, and Sika's own colours. (A fourth, "Blue",
// was a light theme with a blue accent; a saved choice of it now opens Light.)
const THEMES = ['light', 'dark', 'sika'];
const THEME_LABEL = {
  light: 'Light',
  dark: 'Dark',
  sika: 'Sika',
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
  document.querySelectorAll('.theme-btn').forEach((b) => {
    b.classList.toggle('is-on', b.dataset.theme === theme);
    b.setAttribute('aria-checked', String(b.dataset.theme === theme));
  });
  document.querySelectorAll('.theme-swatch[data-current]').forEach((sw) => {
    sw.className = `theme-swatch theme-swatch--${theme}`;
  });
  // Anything that cached a CSS colour at startup re-reads it here. The globe
  // is the only such thing today, but the event is the contract, not the
  // caller: a listener is how a new consumer opts in without this module
  // needing to know it exists.
  document.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
  return theme;
}

/* One small button that opens a menu, instead of a row of swatches in the
 * header. Each item keeps class .theme-btn and data-theme, so anything that
 * already knew the swatches still finds them. */
function mountThemePicker(host) {
  if (!host) return;
  host.classList.add('theme-menu');
  host.innerHTML = `
    <button type="button" class="theme-toggle" aria-haspopup="true" aria-expanded="false"
            title="Colour theme" aria-label="Colour theme">
      <span class="theme-swatch" data-current></span>
    </button>
    <div class="theme-list" role="menu" hidden>
      ${THEMES.map((name) => `
        <button type="button" class="theme-btn" role="menuitemradio" data-theme="${name}">
          <span class="theme-swatch theme-swatch--${name}"></span>${THEME_LABEL[name]}
        </button>`).join('')}
    </div>`;
  const toggle = host.querySelector('.theme-toggle');
  const list = host.querySelector('.theme-list');
  const setOpen = (open) => {
    list.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  };
  toggle.addEventListener('click', (event) => {
    event.stopPropagation();
    setOpen(list.hidden);
  });
  list.addEventListener('click', (event) => {
    const button = event.target.closest('.theme-btn');
    if (!button) return;
    applyTheme(button.dataset.theme);
    setOpen(false);
  });
  document.addEventListener('click', (event) => { if (!host.contains(event.target)) setOpen(false); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') setOpen(false); });
}

// Applied at parse time, before the page script reads any colour.
applyTheme(storedTheme() || 'light');
document.addEventListener('DOMContentLoaded', () => {
  mountThemePicker(document.getElementById('themes'));
  applyTheme(document.documentElement.getAttribute('data-theme'));
});
