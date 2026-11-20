/* Keep the last mobile content reachable beneath the collapsible monitors. */
(() => {
  function attach() {
    const panel = document.getElementById('dashboard-monitors');
    if (!panel) return false;
    const update = () => {
      document.documentElement.style.setProperty('--dashboard-monitors-space',
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
