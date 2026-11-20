/* Keep the native disclosure's state for this tab. */
(() => {
  // Reuse the key written by the previous Dash session store.
  const key = 'backtest-monitor-collapsed';
  function attach() {
    const panel = document.getElementById('backtest-monitor-disclosure');
    if (!panel) return false;

    try {
      panel.open = window.sessionStorage.getItem(key) !== 'true';
    } catch {
      return true;
    }
    panel.addEventListener('toggle', () => {
      try {
        window.sessionStorage.setItem(key, String(!panel.open));
      } catch {
        // The disclosure still works if browser storage is blocked.
      }
    });
    return true;
  }

  if (!attach()) {
    const observer = new MutationObserver(() => { if (attach()) observer.disconnect(); });
    observer.observe(document.documentElement, {childList: true, subtree: true});
  }
})();
