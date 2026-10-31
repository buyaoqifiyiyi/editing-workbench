'use strict';
(() => {
  const storageKey = 'workbench-theme';
  let theme = document.documentElement.dataset.theme;
  if (theme !== 'dark' && theme !== 'light') {
    theme = 'dark';
    try {
      if (localStorage.getItem(storageKey) === 'light') theme = 'light';
    } catch {
      // Storage may be unavailable; the current page can still switch themes.
    }
  }
  document.documentElement.dataset.theme = theme;

  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('theme-toggle');
    if (!button) return;
    const icon = document.getElementById('theme-icon');
    const label = document.getElementById('theme-label');
    const token = document.querySelector('meta[name="workbench-token"]').content;
    let saves = Promise.resolve(), selection = 0;
    function render() {
      const dark = theme === 'dark';
      document.documentElement.dataset.theme = theme;
      label.textContent = dark ? '深色' : '浅色';
      icon.setAttribute('href', dark ? '#i-sun' : '#i-moon');
      const action = dark ? '切换到浅色主题' : '切换到深色主题';
      button.title = action;
      button.setAttribute('aria-label', action);
    }
    render();
    button.addEventListener('click', () => {
      theme = theme === 'dark' ? 'light' : 'dark';
      render();
      try {
        localStorage.setItem(storageKey, theme);
      } catch {
        // Keep the selected theme in memory if persistence is blocked.
      }
      const requestedTheme = theme, revision = ++selection;
      saves = saves.then(async () => {
        try {
          const response = await fetch('/api/theme', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Workbench-Token': token},
            body: JSON.stringify({theme: requestedTheme}),
          });
          if (!response.ok) throw new Error('Theme save failed');
        } catch {
          if (revision === selection) button.title += '（主题仅本次有效，保存失败）';
        }
      });
    });
  }, {once: true});
})();
