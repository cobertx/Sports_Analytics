/* Remember a saved game before a history link leaves it, then reopen its market. */
(() => {
  let entries = {};
  let pending = null;
  let restoredElement = null;
  const sportFor = hash => /^#(nba|nfl|ncaaf)-game-\d+$/.exec(hash || '')?.[1]?.toUpperCase();
  const copy = value => value && typeof value === 'object' && !Array.isArray(value) ? {...value} : {};

  function restore() {
    if (!pending || location.hash !== pending.route) return;
    const panel = document.getElementById('market-game-detail');
    if (!panel || !panel.getClientRects().length || getComputedStyle(panel).display === 'none') return;
    const element = document.getElementById(pending.section || 'market-game-header');
    if (!element) return;
    if (getComputedStyle(element).visibility === 'hidden' || element.closest('[data-dash-is-loading="true"]')) {
      restoredElement = null;
      return;
    }
    if (element === restoredElement) return;
    if (element.tagName === 'DETAILS') element.open = true;
    restoredElement = element;
    requestAnimationFrame(() => {
      if (pending && location.hash === pending.route && element.isConnected) {
        element.scrollIntoView({block: 'start', behavior: 'instant'});
      }
    });
  }

  window.dash_clientside = Object.assign({}, window.dash_clientside, {
    market_navigation: {
      remember: function(hash, snapshot, category, saved) {
        entries = copy(saved);
        const sport = sportFor(hash);
        if (!sport || !snapshot || !category) return window.dash_clientside.no_update;
        const previous = entries[sport];
        if (previous && previous.route === hash && previous.snapshot === snapshot && previous.category === category) {
          restore();
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
    restore();
    if (/^#historical-player\?/.test(location.hash)) {
      const target = location.hash;
      requestAnimationFrame(() => {
        if (location.hash === target) {
          document.getElementById('historical-performance-panel')?.scrollIntoView({block: 'start', behavior: 'instant'});
        }
      });
    }
  });

  // Dash replaces the market subtree after forecasts arrive. Reapply the
  // position to the new node, but stop as soon as the user interacts again.
  new MutationObserver(restore).observe(document.documentElement, {childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'data-dash-is-loading']});
  const stop = () => { pending = null; restoredElement = null; };
  window.addEventListener('wheel', stop, {passive: true});
  window.addEventListener('touchstart', stop, {passive: true});
  window.addEventListener('keydown', stop);
  window.addEventListener('pointerdown', () => {
    if (pending && location.hash === pending.route) stop();
  });
})();
