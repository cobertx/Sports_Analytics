/* Retain the reader's accordion choices when exchange metadata replaces rows. */
(() => {
  const scopes = new Map();
  const selector = 'details.kalshi-market[data-market-scope]';
  function state(section) {
    const scope = section.dataset.marketScope;
    if (!scopes.has(scope)) {
      scopes.set(scope, new Map());
      while (scopes.size > 32) scopes.delete(scopes.keys().next().value);
    }
    return scopes.get(scope);
  }
  document.addEventListener('click', event => {
    const summary = event.target.closest?.('summary');
    const section = summary?.parentElement;
    if (!section?.matches(selector)) return;
    state(section).set(section.id, !section.open);
  }, true);
  let scheduled = false;
  const observer = new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      for (const section of document.querySelectorAll(selector)) {
        const saved = state(section).get(section.id);
        if (saved !== undefined && section.open !== saved) section.open = saved;
      }
    });
  });
  observer.observe(document.documentElement, {childList: true, subtree: true,
    attributes: true, attributeFilter: ['open', 'data-market-scope']});
})();
