/* Remember a saved game before a history link leaves it, then reopen its market. */
(() => {
  let entries = {};
  let pending = null;
  let restoredElement = null;
  let historyPending = /^#historical-player\?/.test(location.hash) ? location.hash : null;
  let pendingFocus = null;
  let scheduled = false;
  let navigationPending = true;
  const sportFor = hash => /^#(nba|nfl|ncaaf)-game-\d+$/.exec(hash || '')?.[1]?.toUpperCase();
  const copy = value => value && typeof value === 'object' && !Array.isArray(value) ? {...value} : {};

  function restore() {
    syncNavigation();
    if (pendingFocus === location.hash) {
      const main = document.getElementById('dashboard-main');
      if (main?.getClientRects().length) {
        main.focus({preventScroll: true});
        pendingFocus = null;
      }
    }
    if (historyPending === location.hash) {
      const element = document.getElementById('historical-performance-panel');
      if (element?.getClientRects().length && !element.closest('[data-dash-is-loading="true"]')) {
        element.scrollIntoView({block: 'start', behavior: 'instant'});
        historyPending = null;
      }
    }
    if (!pending || location.hash !== pending.route) return;
    const panel = document.getElementById('market-game-detail');
    if (!panel || !panel.getClientRects().length || getComputedStyle(panel).display === 'none') return;
    const element = document.getElementById(pending.section || 'market-game-header');
    if (!element || !element.getClientRects().length) return;
    if (getComputedStyle(element).visibility === 'hidden' || element.closest('[data-dash-is-loading="true"]')) {
      restoredElement = null;
      return;
    }
    if (element === restoredElement) return;
    if (element.tagName === 'DETAILS') element.open = true;
    restoredElement = element;
    const request = pending;
    requestAnimationFrame(() => {
      if (pending === request && location.hash === request.route && element.isConnected) {
        element.scrollIntoView({block: 'start', behavior: 'instant'});
      }
    });
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; restore(); });
  }

  function syncNavigation() {
    const api = window.dash_clientside?.market_transition;
    if (!api) return;
    const destination = api.destination(location.hash);
    if (!destination) { navigationPending = false; return; }
    const primary = {historical: 'historical-page-link', market: 'market-page-link', kalshi: 'kalshi-page-link',
      polymarket: 'polymarket-page-link', 'market-summary': 'market-summary-page-link',
      'filtered-results': 'filtered-results-page-link', backtesting: 'backtesting-page-link', 'in-game': 'in-game-page-link'};
    const primaryIds = new Set(Object.values(primary));
    const links = document.querySelectorAll('.page-toolbar a[href]');
    navigationPending = !links.length;
    for (const link of links) {
      const active = primaryIds.has(link.id) ? link.id === primary[destination.page] : link.getAttribute('href') === location.hash;
      const value = active ? 'page' : 'false';
      if (link.getAttribute('aria-current') !== value) link.setAttribute('aria-current', value);
    }
  }

  window.dash_clientside = Object.assign({}, window.dash_clientside, {
    market_navigation: {
      remember: function(hash, snapshot, category, saved) {
        entries = copy(saved);
        const sport = sportFor(hash);
        if (!sport || !snapshot || !category) return window.dash_clientside.no_update;
        const previous = entries[sport];
        if (previous && previous.route === hash && previous.snapshot === snapshot && previous.category === category) {
          schedule();
          return window.dash_clientside.no_update;
        }
        // A different game, snapshot or category starts a new location.
        entries[sport] = {route: hash, snapshot, category, section: null};
        if (pending && pending.route === hash && pending.snapshot !== snapshot) {
          // A removed snapshot falls back to the game's available saved data.
          pending = {...entries[sport], section: 'market-game-header'};
          restoredElement = null;
        }
        return {...entries};
      }
    }
  });

  document.addEventListener('click', event => {
    if (event.target.closest?.('#skip-navigation')) {
      document.getElementById('dashboard-main')?.focus();
      return;
    }
    const anchor = event.target.closest?.('a.market-history-link');
    const sport = sportFor(location.hash);
    if (!anchor || !sport || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const current = entries[sport];
    if (!current || current.route !== location.hash) return;
    const section = anchor.closest('details.sportsbook-market');
    entries = {...entries, [sport]: {...current, section: section?.id || 'market-game-header'}};
    window.dash_clientside.set_props('market-return', {data: entries});
  }, true);

  window.addEventListener('hashchange', event => {
    const oldHash = event.oldURL.includes('#') ? event.oldURL.slice(event.oldURL.indexOf('#')) : '';
    const sport = sportFor(location.hash);
    const entry = sport && entries[sport];
    pending = oldHash.startsWith('#historical') && entry?.route === location.hash ? {...entry} : null;
    restoredElement = null;
    historyPending = /^#historical-player\?/.test(location.hash) ? location.hash : null;
    pendingFocus = window.dash_clientside?.market_transition?.destination(location.hash) ? location.hash : null;
    navigationPending = true;
    schedule();
  });

  // Dash replaces the market subtree after forecasts arrive. Reapply the
  // position to the new node, but stop as soon as the user interacts again.
  new MutationObserver((records = []) => {
    // Coalesce Plotly/Dash mutation bursts; do no layout reads for unrelated
    // updates once restoration has finished.
    if (pending || historyPending || pendingFocus || navigationPending || records.some(record =>
        record.target.id === 'app-shell' || record.target.closest?.('.page-toolbar'))) schedule();
  }).observe(document.documentElement, {childList: true, subtree: true, attributes: true,
    attributeFilter: ['style', 'class', 'href', 'data-dash-is-loading']});
  const stop = () => { pending = null; historyPending = null; pendingFocus = null; restoredElement = null; };
  window.addEventListener('wheel', stop, {passive: true});
  window.addEventListener('touchstart', stop, {passive: true});
  window.addEventListener('keydown', stop);
  window.addEventListener('pointerdown', () => {
    if (pending && location.hash === pending.route || historyPending || pendingFocus) stop();
  });
  schedule();
})();
