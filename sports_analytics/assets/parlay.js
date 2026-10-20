/* Reserve exactly the drawer's visible height so the last market stays reachable. */
(() => {
  function attach() {
    const panel = document.getElementById('parlay-drawer');
    if (!panel) return false;
    const update = () => {
      const height = Math.ceil(panel.getBoundingClientRect().height);
      document.documentElement.style.setProperty('--parlay-space', (height ? height + 16 : 0) + 'px');
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
