/* Gate each response against the live selection, including responses that arrive after navigation. */
(() => {
  const providers = ['market', 'kalshi', 'summary'];
  function currentScope(provider, selection, hash) {
    const route = window.dash_clientside?.market_transition?.route(hash);
    const matches = provider === 'summary' ? ['summary', 'draftkings'].includes(route?.provider) :
      route?.provider === provider && route.game;
    return matches && selection ? JSON.stringify([hash, selection]) : null;
  }

  function connect() {
    const feedback = providers.map(provider => document.getElementById(`${provider}-quick-feedback`));
    if (feedback.some(section => !section)) return false;
    providers.forEach((provider, index) => {
      const section = feedback[index];
      const slots = [...document.querySelectorAll(`.quick-result-slot[data-quick-provider="${provider}"]`)];
      const update = () => {
        const scope = currentScope(provider, section.dataset.quickSelection, window.location.hash);
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
    });
    return true;
  }

  if (!connect()) {
    const bootstrap = new MutationObserver(() => { if (connect()) bootstrap.disconnect(); });
    bootstrap.observe(document.documentElement, {childList: true, subtree: true});
  }
})();
