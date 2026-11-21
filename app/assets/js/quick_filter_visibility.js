/* Gate each response against the live selection, including responses that arrive after navigation. */
(() => {
  const providers = ['market', 'kalshi', 'summary'];
  const connected = new Set();
  const submissions = new Map();
  const controls = {market:'market-snapshot', kalshi:'kalshi-snapshot', summary:'summary-game'};
  const startingMessage = 'Starting Quick Filtering…';
  const waitForSubmission = 30000;

  function selectedValue(provider, section) {
    const props = window.dash_component_api?.getLayout?.(controls[provider])?.props;
    if (!props) return section.dataset.quickSelection;
    const selection = props.value ?? (provider === 'kalshi' ? 'live' : null);
    return selection === null ? null : String(selection);
  }

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
      const starter = document.getElementById(`${provider}-starting-quick-progress`);
      const buttonId = provider === 'summary' ? 'summary-quick-run' : `${provider}-quick-filter`;
      let submission = null;

      function finishStarting() {
        if (submission?.timer == null) return;
        window.clearTimeout(submission.timer);
        submission.timer = null;
        section.removeAttribute('data-quick-starting');
      }

      const update = () => {
        const hash = window.location.hash;
        const request = provider === 'kalshi' ? submittedScope(starter, hash) : null;
        const scope = currentScope(provider, selectedValue(provider, section), hash) ?? request;
        if (submission && submission.scope !== scope) {
          finishStarting();
          submission = null;
        }
        for (const slot of slots) {
          for (const child of slot.children) {
            const sequence = child.dataset.quickSequence;
            const matches = scope !== null && child.dataset.quickScope === scope
              && (!sequence || !submission || sequence === submission.sequence);
            child.toggleAttribute('data-quick-visible', matches);
            if (submission && matches && sequence === submission.sequence) finishStarting();
          }
        }
      };

      submissions.set(buttonId, event => {
        const scope = currentScope(provider, selectedValue(provider, section), window.location.hash);
        if (!scope || !starter) return;
        if (submission?.scope === scope && submission.timer !== null && !submission.slow) {
          event.preventDefault();
          event.stopPropagation();
          return;
        }
        finishStarting();
        // Capture runs before Dash increments n_clicks and queues the submission.
        const clicks = window.dash_component_api?.getLayout?.(buttonId)?.props?.n_clicks ?? 0;
        const message = starter.querySelector('[role="status"]');
        message.textContent = startingMessage;
        submission = {scope, sequence: String(clicks + 1), slow: false};
        submission.timer = window.setTimeout(() => {
          submission.slow = true;
          message.textContent = 'Still waiting for Quick Filtering to respond…';
        }, waitForSubmission);
        section.setAttribute('data-quick-starting', '');
      });

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
            observer.observe(child, {attributes: true, attributeFilter: ['data-quick-scope', 'data-quick-sequence']});
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

  document.addEventListener('click', event => {
    const button = event.target.closest?.('button');
    if (button && !button.disabled) submissions.get(button.id)?.(event);
  }, true);

  if (!connect()) {
    const bootstrap = new MutationObserver(() => { if (connect()) bootstrap.disconnect(); });
    bootstrap.observe(document.documentElement, {childList: true, subtree: true});
  }
})();
