/* Keyboard access and focus return for the shared activity dialog. */
(() => {
  let previous = null;
  function bind() {
    const modal = document.getElementById('structure-modal');
    if (!modal || modal.dataset.focusBound) return;
    modal.dataset.focusBound = '1';
    const dialog = modal.querySelector('.structure-dialog');
    new MutationObserver(() => {
      if (getComputedStyle(modal).display !== 'none') {
        previous = document.activeElement;
        document.getElementById('structure-close')?.focus();
      } else if (previous?.isConnected) {
        previous.focus();
        previous = null;
      }
    }).observe(modal, {attributes:true, attributeFilter:['style']});
    modal.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        document.getElementById('structure-close')?.click();
      }
      if (event.key !== 'Tab') return;
      const controls = [...dialog.querySelectorAll('button:not(:disabled),a[href],input:not(:disabled)')]
        .filter(element => element.getClientRects().length);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first?.focus();
      }
    });
  }
  new MutationObserver(bind).observe(document.documentElement,{childList:true,subtree:true});
  bind();
})();
