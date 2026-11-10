/* Remember a saved game before a history link leaves it, then reopen its market. */
(() => {
  let entries = {};
  let pending = null;
  let restoredElement = null;
  let historyPending = /^#historical-player\?/.test(location.hash) ? location.hash : null;
  let pendingFocus = null;
  let scheduled = false;
  let navigationPending = true;
  let controls = {};
  let lastNotice=null;
  const routeFor = hash => {
    const match=/^#(nba|nfl|ncaaf)-(game-(\d+)|(kalshi|polymarket)-game-([A-Z0-9_.-]+))$/.exec(hash||'');
    if(!match)return null;
    const sport=match[1].toUpperCase(),provider=match[4]||'market',game=match[3]||match[5];
    if(provider==='polymarket'&&!/^\d+$/.test(game)||provider==='kalshi'&&(!game.startsWith('KX'+sport+'GAME-')||game.length>200))return null;
    return {sport,provider,game,key:provider+':'+sport};
  };
  const copy = value => value && typeof value === 'object' && !Array.isArray(value) ? {...value} : {};
  const normalize = saved => {
    const result={};
    for(const [key,value] of Object.entries(saved?.v===2?copy(saved.entries):copy(saved))) {
      const r=routeFor(value?.route);if(!r||saved?.v===2&&key!==r.key)continue;
      const entry={route:value.route};
      for(const name of ['snapshot','category','section','prompt'])if(typeof value[name]==='string'&&value[name].length<=1500)entry[name]=value[name];
      if(['PPP','EPA','ELO'].includes(value.method))entry.method=value.method;
      if(['WHP','UO','OAM','PGE'].includes(value.player_method))entry.player_method=value.player_method;
      if(Number.isInteger(value.rows)&&value.rows>=0&&value.rows<=5000)entry.rows=value.rows;
      result[r.key]=entry;
    }
    return result; // At most three providers times three validated sports.
  };
  const savedState=()=>({v:2,entries:{...entries}});
  const roots={market:'market-game-detail',kalshi:'kalshi-detail',polymarket:'polymarket-detail'};
  const headers={market:'market-game-header',kalshi:'kalshi-game-header',polymarket:'polymarket-game-header'};
  const notice=message=>{if(message===lastNotice)return;lastNotice=message;
    window.dash_clientside?.set_props?.('market-return-status',{children:message});};

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
    const route=routeFor(pending.route);if(!route)return;
    const panel = document.getElementById(roots[route.provider]);
    if (!panel || !panel.getClientRects().length || getComputedStyle(panel).display === 'none') return;
    const shell=document.getElementById('app-shell');
    if(/quotes-pending|market-screen-pending/.test(shell?.className||'')||panel.closest?.('.market-screen-pending')||
       /quotes-pending/.test(panel.className||''))return;
    const selected=controls[route.provider];
    if(selected?.context&&(selected.context.sport!==route.sport||String(selected.context.id)!==route.game))return;
    if(selected&&(selected.snapshot!==pending.snapshot||selected.category!==pending.category)){
      notice('The previous snapshot or category is unavailable. Showing the available game view.');
      pending={...pending,snapshot:selected.snapshot,category:selected.category,section:headers[route.provider],rows:0};
      entries[route.key]={...entries[route.key],snapshot:selected.snapshot,category:selected.category,section:null};
      window.dash_clientside.set_props('market-return',{data:savedState()});
    }
    let element = pending.section&&(window.CSS?.escape&&panel.querySelector?.('#'+window.CSS.escape(pending.section))||document.getElementById(pending.section));
    if(element&&panel.contains&&!panel.contains(element))element=null;
    if(!element&&/quotes-pending|market-screen-pending/.test(shell?.className||''))return;
    if(!element){element=document.getElementById(headers[route.provider]);
      if(pending.section)notice('The previous prompt is no longer available. Showing the game header.');}
    if (!element || !element.getClientRects().length) return;
    if (getComputedStyle(element).visibility === 'hidden' || element.closest('[data-dash-is-loading="true"]')) {
      restoredElement = null;
      return;
    }
    if (element.tagName === 'DETAILS') element.open = true;
    // Previously expanded lazy pages use the existing bounded row callback.
    // Never pull quotes or fit a model merely to restore a scroll position.
    const rows=element.querySelectorAll?.('.market-outcome').length||0;
    const more=element.querySelector?.('button.market-load-more');
    if(element.querySelector?.('.lazy-market-body [data-ready="false"]'))return;
    if(more&&rows<(pending.rows||0)&&more.getClientRects().length&&!more.disabled){
      if(pending.lastRows!==rows){pending.lastRows=rows;more.click();}return;
    }
    if (element === restoredElement) return;
    const prompt=pending.prompt&&[...(element.querySelectorAll?.('a.market-history-link')||[])].find(link=>link.getAttribute('href')===pending.prompt);
    const landing=prompt?.closest?.('.market-outcome')||element;
    restoredElement = element;
    const request = pending;
    requestAnimationFrame(() => {
      if (pending === request && location.hash === request.route && element.isConnected) {
        landing.scrollIntoView({block: 'start', behavior: 'instant'});
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
        const all=arguments[4];controls=all||controls;
        entries = normalize(saved);
        const route = routeFor(hash);
        if(!route)return window.dash_clientside.no_update;
        const selected=all?.[route.provider]||{snapshot,category};
        if(all&&(!selected.context||selected.context.sport!==route.sport||String(selected.context.id)!==route.game))return window.dash_clientside.no_update;
        snapshot=selected.snapshot;category=selected.category;
        if(!snapshot||!category)return window.dash_clientside.no_update;
        const previous = entries[route.key];
        if(pending?.route===hash&&previous?.route===hash&&(snapshot!==previous.snapshot||category!==previous.category)){
          // Wait for the server's validated snapshot/category restoration before
          // replacing the remembered location with transient old controls.
          return window.dash_clientside.no_update;
        }
        if (previous && previous.route === hash && previous.snapshot === snapshot && previous.category === category&&
            previous.method===selected.method&&previous.player_method===selected.player_method) {
          schedule();
          return window.dash_clientside.no_update;
        }
        // A different game, snapshot or category starts a new location.
        entries[route.key] = {route: hash, snapshot, category, section: null,
          ...(selected.method?{method:selected.method}:{}),...(selected.player_method?{player_method:selected.player_method}:{})};
        if (pending && pending.route === hash && pending.snapshot !== snapshot) {
          // A removed snapshot falls back to the game's available saved data.
          pending = {...entries[route.key], section: headers[route.provider]};
          restoredElement = null;
        }
        return savedState();
      },
      route: routeFor,
      entry: function(saved,sport,provider='market'){return normalize(saved)[provider+':'+sport]||null;},
      href: function(saved,sport,provider='market'){return normalize(saved)[provider+':'+sport]?.route||
        '#'+sport.toLowerCase()+(provider==='market'?'-games':'-'+provider+'-market');}
    }
  });

  document.addEventListener('click', event => {
    if(/^#historical/.test(location.hash)&&event.target.closest?.('.dash-dropdown-option,.Select-option'))
      window.dash_clientside?.market_transition?.user_history_control?.();
    if (event.target.closest?.('#skip-navigation')) {
      document.getElementById('dashboard-main')?.focus();
      return;
    }
    const anchor = event.target.closest?.('a.market-history-link');
    const route = routeFor(location.hash);
    if (!anchor || !route || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const current = entries[route.key];
    if (!current || current.route !== location.hash) return;
    const section = anchor.closest('details.sportsbook-market');
    entries = {...entries, [route.key]: {...current, section: section?.id || headers[route.provider],
      rows:Math.min(5000,section?.querySelectorAll?.('.market-outcome').length||0),
      prompt:anchor.getAttribute?.('href')||''}};
    window.dash_clientside.set_props('market-return', {data: savedState()});
  }, true);

  window.addEventListener('hashchange', event => {
    const oldHash = event.oldURL.includes('#') ? event.oldURL.slice(event.oldURL.indexOf('#')) : '';
    const route = routeFor(location.hash);
    const entry = route && entries[route.key];
    pending = oldHash.startsWith('#historical') && entry?.route === location.hash ? {...entry} : null;
    notice('');
    if(pending&&route.provider==='kalshi'){
      const selected=controls.kalshi||{};
      for(const [name,field] of [['method','kalshi-game-method'],['player_method','kalshi-player-method']]){
        if(entry[name]&&entry[name]!==selected[name])window.dash_clientside.set_props(field,{value:entry[name]});
      }
    }
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
    const root=pending&&document.getElementById(roots[routeFor(pending.route)?.provider]);
    const relevant=!records.length||records.some(record=>['app-shell','historical-results','career-panel','market-page','kalshi-page','polymarket-page'].includes(record.target.id)||record.target.closest?.('.page-toolbar')||
      !record.target.closest?.('.dash-graph,.js-plotly-plot')&&root?.contains?.(record.target));
    if ((pending||historyPending||pendingFocus||navigationPending)&&relevant) schedule();
  }).observe(document.documentElement, {childList: true, subtree: true, attributes: true,
    attributeFilter: ['style', 'class', 'href', 'data-dash-is-loading']});
  const stop = () => { pending = null; historyPending = null; pendingFocus = null; restoredElement = null; };
  window.addEventListener('wheel', stop, {passive: true});
  window.addEventListener('touchstart', stop, {passive: true});
  window.addEventListener('keydown', event=>{
    if(/^#historical/.test(location.hash)&&['Enter',' ','ArrowUp','ArrowDown'].includes(event?.key)&&
        event.target?.closest?.('.analysis-filters,.dataset-builder,.dash-dropdown-content,.Select-menu-outer'))
      window.dash_clientside?.market_transition?.user_history_control?.();
    stop();
  });
  window.addEventListener('pointerdown', () => {
    if (pending && location.hash === pending.route || historyPending || pendingFocus) stop();
  });
  schedule();
})();
