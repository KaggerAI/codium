/*
 * Kagger theme bootstrap (light / dark).
 *
 * Load synchronously as the FIRST script in <head> so <html data-theme> is set
 * before anything paints (no flash of the wrong theme):
 *     <script src="/kagger_theme.js"></script>
 *     <link rel="stylesheet" href="/kagger_theme.css">
 *
 * Choice order: the user's saved choice (localStorage "kagger-theme"), else the
 * operating-system preference. The choice is shared by every page and synced
 * live across open tabs.
 *
 * Markup hooks (wired automatically, also for elements added later):
 *   <button class="kg-theme-toggle" data-kg-theme-toggle></button>
 *       -> sun/moon toggle; icons + aria-label are filled in here.
 *   <button data-kg-menu-toggle="menuId" aria-controls="menuId">
 *       -> toggles class "kg-open" on #menuId (mobile menus); closes on
 *          Escape, outside click, clicking a link inside the menu, or when
 *          the window grows to the desktop layout (>= 1024px, or the button's
 *          data-kg-menu-max="<px>" for menus that collapse at another width).
 *
 * JS API:  window.KaggerTheme
 *   .get()                 -> 'light' | 'dark'
 *   .set('light'|'dark')   -> apply + remember
 *   .toggle()
 *   .onChange(fn)          -> fn(theme) after every switch; returns an unsubscribe fn
 *   .colors()              -> resolved palette for charts { isDark, text, muted, grid, bg, surface, border, primary, accent, ... }
 *   .isDark()
 * Also fires  window 'kagger:themechange'  (detail: { theme }).
 */
(function () {
  'use strict';

  var KEY = 'kagger-theme';
  var root = document.documentElement;
  var mql = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  var listeners = [];
  var META_COLORS = { light: '#14532d', dark: '#0a0a0f' };

  function readStored() {
    try {
      var v = window.localStorage.getItem(KEY);
      return v === 'light' || v === 'dark' ? v : null;
    } catch (e) {
      return null;
    }
  }

  function writeStored(theme) {
    try { window.localStorage.setItem(KEY, theme); } catch (e) { /* private mode */ }
  }

  function systemTheme() {
    return mql && mql.matches ? 'dark' : 'light';
  }

  function current() {
    return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }

  function syncMeta(theme) {
    var head = document.head;
    if (!head) return;
    var meta = head.querySelector('meta[name="theme-color"]');
    if (!meta) {
      meta = document.createElement('meta');
      meta.setAttribute('name', 'theme-color');
      head.appendChild(meta);
    }
    meta.setAttribute('content', META_COLORS[theme]);
  }

  var SUN = '<svg class="kg-i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
  var MOON = '<svg class="kg-i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

  function syncToggles(theme) {
    var label = theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    var toggles = document.querySelectorAll('[data-kg-theme-toggle]');
    for (var i = 0; i < toggles.length; i++) {
      var t = toggles[i];
      if (!t.querySelector('.kg-i-sun')) t.insertAdjacentHTML('beforeend', SUN + MOON);
      if (t.tagName === 'BUTTON' && !t.hasAttribute('type')) t.setAttribute('type', 'button');
      t.setAttribute('aria-label', label);
      t.setAttribute('title', label);
      t.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
    }
  }

  function apply(theme) {
    if (theme !== 'light' && theme !== 'dark') return;
    var prev = root.getAttribute('data-theme');
    var changed = prev !== theme;
    if (changed && prev) root.classList.add('kg-theme-switching');
    root.setAttribute('data-theme', theme);
    syncMeta(theme);
    syncToggles(theme);
    if (!changed || !prev) return;
    // Let the new colours paint, then re-enable transitions and notify.
    var done = function () { root.classList.remove('kg-theme-switching'); };
    if (window.requestAnimationFrame) {
      window.requestAnimationFrame(function () { window.requestAnimationFrame(done); });
    } else {
      setTimeout(done, 50);
    }
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i](theme); } catch (e) { if (window.console) console.error('[KaggerTheme] listener failed', e); }
    }
    try {
      window.dispatchEvent(new CustomEvent('kagger:themechange', { detail: { theme: theme } }));
    } catch (e) { /* very old browsers */ }
  }

  function cssVar(name, fallback) {
    var v = '';
    try { v = getComputedStyle(root).getPropertyValue(name).trim(); } catch (e) { /* ignore */ }
    return v || fallback;
  }

  var api = {
    get: current,
    isDark: function () { return current() === 'dark'; },
    set: function (theme) {
      if (theme !== 'light' && theme !== 'dark') return;
      writeStored(theme);
      apply(theme);
    },
    toggle: function () { api.set(current() === 'dark' ? 'light' : 'dark'); },
    onChange: function (fn) {
      if (typeof fn !== 'function') return function () {};
      listeners.push(fn);
      return function () {
        var i = listeners.indexOf(fn);
        if (i >= 0) listeners.splice(i, 1);
      };
    },
    colors: function () {
      var dark = current() === 'dark';
      return {
        isDark: dark,
        text: cssVar('--kg-chart-text', dark ? '#a0a0b8' : '#334155'),
        strong: cssVar('--kg-text', dark ? '#f0f0f5' : '#1e293b'),
        muted: cssVar('--kg-muted', dark ? '#8b8ba3' : '#64748b'),
        grid: cssVar('--kg-chart-grid', dark ? '#2a2a3a' : '#e2e8f0'),
        bg: cssVar('--kg-bg', dark ? '#0a0a0f' : '#f1f5f9'),
        surface: cssVar('--kg-surface', dark ? '#16161f' : '#ffffff'),
        border: cssVar('--kg-border', dark ? '#2a2a3a' : '#e2e8f0'),
        primary: cssVar('--kg-primary', dark ? '#7c3aed' : '#16a34a'),
        accent: cssVar('--kg-accent', dark ? '#10b981' : '#059669'),
        success: cssVar('--kg-success', dark ? '#10b981' : '#16a34a'),
        danger: cssVar('--kg-danger', dark ? '#ef4444' : '#dc2626'),
        warning: cssVar('--kg-warning', dark ? '#f59e0b' : '#d97706')
      };
    },
    // Theme the page was originally designed in (from <body class="kg-native-*">).
    native: function () {
      var b = document.body;
      return b && b.classList.contains('kg-native-dark') ? 'dark' : 'light';
    },
    // Run fn (sync or async) with the page temporarily in `theme`, then restore,
    // without saving the choice. Used so PDF/image exports (html2canvas,
    // html2pdf, jsPDF) come out exactly as they did before theming existed:
    //   KaggerTheme.withTheme(KaggerTheme.native(), () => html2pdf()...save())
    // onChange listeners run both ways, so charts re-colour for the capture.
    withTheme: function (theme, fn) {
      var prev = current();
      if (theme !== 'light' && theme !== 'dark') theme = prev;
      if (theme === prev) {
        try { return Promise.resolve(fn()); } catch (e) { return Promise.reject(e); }
      }
      apply(theme);
      var settle = new Promise(function (res) {
        var raf = window.requestAnimationFrame || function (f) { return setTimeout(f, 16); };
        raf(function () { raf(function () { setTimeout(res, 60); }); });
      });
      // Back to the user's effective preference (normally `prev`).
      var restore = function () { apply(readStored() || systemTheme()); };
      return settle.then(function () { return fn(); }).then(
        function (v) { restore(); return v; },
        function (err) { restore(); throw err; }
      );
    },
    _syncToggles: function () { syncToggles(current()); }
  };
  window.KaggerTheme = api;

  // Printing (Ctrl+P / window.print) uses the page's original theme, exactly as before.
  var printPrev = null;
  window.addEventListener('beforeprint', function () {
    var n = api.native();
    if (current() !== n) { printPrev = current(); apply(n); }
  });
  window.addEventListener('afterprint', function () {
    if (printPrev) { apply(printPrev); printPrev = null; }
  });

  // ---- initial theme (runs synchronously in <head>) ----
  apply(readStored() || systemTheme());

  // Follow the OS setting until the user picks a theme explicitly.
  if (mql) {
    var onSystem = function () { if (!readStored()) apply(systemTheme()); };
    if (mql.addEventListener) mql.addEventListener('change', onSystem);
    else if (mql.addListener) mql.addListener(onSystem);
  }

  // Keep every open tab in step.
  window.addEventListener('storage', function (e) {
    if (e.key === KEY) apply(readStored() || systemTheme());
  });

  // ---- delegated click handling: theme toggles + collapsible menus ----
  function closest(el, sel) {
    while (el && el.nodeType === 1) {
      if (el.matches && el.matches(sel)) return el;
      el = el.parentElement;
    }
    return null;
  }

  function setMenu(btn, menu, open) {
    menu.classList.toggle('kg-open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function closeAllMenus(except) {
    var btns = document.querySelectorAll('[data-kg-menu-toggle][aria-expanded="true"]');
    for (var i = 0; i < btns.length; i++) {
      var menu = document.getElementById(btns[i].getAttribute('data-kg-menu-toggle'));
      if (menu && menu !== except) setMenu(btns[i], menu, false);
    }
  }

  document.addEventListener('click', function (e) {
    var toggle = closest(e.target, '[data-kg-theme-toggle]');
    if (toggle) {
      e.preventDefault();
      api.toggle();
      return;
    }
    var menuBtn = closest(e.target, '[data-kg-menu-toggle]');
    if (menuBtn) {
      var menu = document.getElementById(menuBtn.getAttribute('data-kg-menu-toggle'));
      if (menu) {
        e.preventDefault();
        var open = !menu.classList.contains('kg-open');
        closeAllMenus(menu);
        setMenu(menuBtn, menu, open);
      }
      return;
    }
    // Click outside an open menu, or on a link/action inside it, closes it.
    var openBtns = document.querySelectorAll('[data-kg-menu-toggle][aria-expanded="true"]');
    for (var i = 0; i < openBtns.length; i++) {
      var m = document.getElementById(openBtns[i].getAttribute('data-kg-menu-toggle'));
      if (!m) continue;
      var inside = m.contains(e.target);
      var activated = inside && closest(e.target, 'a[href], [data-kg-menu-close]');
      if (!inside || activated) setMenu(openBtns[i], m, false);
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeAllMenus(null);
  });

  // A menu left open on a phone should not stay open after rotating to desktop width.
  window.addEventListener('resize', function () {
    var btns = document.querySelectorAll('[data-kg-menu-toggle][aria-expanded="true"]');
    for (var i = 0; i < btns.length; i++) {
      var max = parseInt(btns[i].getAttribute('data-kg-menu-max'), 10) || 1024;
      var menu = document.getElementById(btns[i].getAttribute('data-kg-menu-toggle'));
      if (menu && window.innerWidth >= max) setMenu(btns[i], menu, false);
    }
  });

  // Fill toggle icons once the markup exists.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { syncToggles(current()); });
  } else {
    syncToggles(current());
  }
})();
