/* Gate each response against the live selection, including responses that arrive after navigation. */
(() => {
  const providers = ['market', 'kalshi', 'summary'];
  const connected = new Set();
  function currentScope(provider, selection, hash) {
    const route = window.dash_clientside?.market_transition?.route(hash);
    const matches = provider === 'summary' ? ['summary', 'draftkings'].includes(route?.provider) :
      route?.provider === provider && route.game;
    return matches && selection ? JSON.stringify([hash, selection]) : null;
  }

  function submittedScope(starter, hash) {
    const scope = starter?.dataset.quickScope;
    if (!scope) return null;
    // The click captures the snapshot even if Dash is still refreshing the
    // view dropdown and its mirrored selection has yet to arrive.
    return JSON.parse(scope)[0] === hash ? scope : null;
  }

  function connect() {
    for (const provider of providers) {
      if (connected.has(provider)) continue;
      const section = document.getElementById(`${provider}-quick-feedback`);
      if (!section) continue;
      const slots = [...document.querySelectorAll(`.quick-result-slot[data-quick-provider="${provider}"]`)];
      const starter = provider === 'kalshi' ? document.getElementById('kalshi-starting-quick-progress') : null;
      const update = () => {
        const hash = window.location.hash;
        const request = submittedScope(starter, hash);
        const scope = currentScope(provider, section.dataset.quickSelection, hash) ?? request;
        for (const slot of slots) {
          for (const child of slot.children) {
            child.toggleAttribute('data-quick-visible', scope !== null && child.dataset.quickScope === scope);
          }
        }
      };
      const observer = new MutationObserver(() => {
        observe();
        update();
      });
      function observe() {
        observer.disconnect();
        observer.observe(section, {attributes: true, attributeFilter: ['data-quick-selection']});
        for (const slot of slots) {
          observer.observe(slot, {childList: true});
          for (const child of slot.children) {
            observer.observe(child, {attributes: true, attributeFilter: ['data-quick-scope']});
          }
        }
      }
      observe();
      for (const event of ['hashchange', 'popstate', '_dashprivate_pushstate']) {
        window.addEventListener(event, update);
      }
      update();
      connected.add(provider);
    }
    return connected.size === providers.length;
  }

  if (!connect()) {
    const bootstrap = new MutationObserver(() => { if (connect()) bootstrap.disconnect(); });
    bootstrap.observe(document.documentElement, {childList: true, subtree: true});
  }
})();
