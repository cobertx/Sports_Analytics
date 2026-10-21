/* Keep the last mobile content reachable beneath the collapsible monitor. */
(() => {
  function attach() {
    const panel = document.getElementById('backtest-monitor');
    if (!panel) return false;
    const update = () => {
      document.documentElement.style.setProperty('--backtest-monitor-space',
        (Math.ceil(panel.getBoundingClientRect().height) + 24) + 'px');
    };
    new ResizeObserver(update).observe(panel);
    update();
    return true;
  }
  if (!attach()) {
    const observer = new MutationObserver(() => { if (attach()) observer.disconnect(); });
    observer.observe(document.documentElement, {childList: true, subtree: true});
  }
})();
