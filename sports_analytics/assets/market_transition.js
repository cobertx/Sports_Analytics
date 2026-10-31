/* URL-driven visibility runs locally, before any data or forecasting callback. */
(() => {
let latest=null;
const plain = 'app-shell';
window.dash_clientside = Object.assign({},window.dash_clientside,{
  market_transition: {
    select: function(hash,sport,page) {
      if(window.location.hash&&hash!==window.location.hash)return [window.dash_clientside.no_update,window.dash_clientside.no_update];
      const value=window.dash_clientside.market_transition.destination(hash),no=window.dash_clientside.no_update;
      if(!value)return [no,no];
      return [value.sport&&value.sport!==sport?value.sport:no,value.page!==page?value.page:no];
    },
    readiness: function(game,kalshi,poly,scope,kalshiGames,polyGames,forecast,kalshiView,polyView,previous) {
      const context=value=>value?{sport:value.sport,id:value.id}:null;
      const catalog=value=>value?{sport:value.sport,...(Array.isArray(value.games)?{games:value.games.map(game=>({id:game.id}))}:{})}:null;
      const view=value=>value?{context:value.context,view_scope:value.view_scope}:null;
      const value={game:context(game),kalshi:context(kalshi),poly:context(poly),scope:catalog(scope),
        kalshiGames:catalog(kalshiGames),polyGames:catalog(polyGames),forecast:forecast?{context:forecast.context,view_key:forecast.view_key}:null,
        kalshiView:view(kalshiView),polyView:view(polyView)};
      return JSON.stringify(value)===JSON.stringify(previous)?window.dash_clientside.no_update:value;
    },
    compact: function(hash,sport,page,ready,snapshot,category,kalshiCategory,polyCategory,historyScope,method,playerMethod,kalshiSnapshot,kalshiMethod,kalshiPlayerMethod,polySnapshot) {
      const r=ready||{};
      return window.dash_clientside.market_transition.reconcile(hash,sport,r.game,r.kalshi,r.poly,r.scope,r.kalshiGames,r.polyGames,
        page,r.forecast,snapshot,category,r.kalshiView,kalshiCategory,r.polyView,polyCategory,historyScope,
        method,playerMethod,kalshiSnapshot,kalshiMethod,kalshiPlayerMethod,polySnapshot);
    },
    destination: function(hash) {
      const market=window.dash_clientside.market_transition.route(hash);
      if(market)return {sport:market.sport,page:{summary:'market-summary',filtered:'filtered-results'}[market.provider]||market.provider};
      const historical=/^#historical(?:-(team|player))?(?:\?(.*))?$/.exec(hash||'');
      if(historical) {
        const value=new URLSearchParams(historical[2]||'').get('sport');
        return {sport:['NBA','NFL','NCAAF'].includes(value)?value:null,page:'historical'};
      }
      if(hash==='#pnl-history')return {sport:'PNL',page:'pnl'};
      if(/^#research(?:-(game-outcome|player-stats)-[a-z0-9-]+)?$/.test(hash||''))return {sport:'RESEARCH',page:'backtesting'};
      const information=/^#(nba|nfl)-information-(injuries|roster|news|weather)$/.exec(hash||'');
      if(information&&(information[1]==='nba'?['injuries','roster']:['news','weather']).includes(information[2]))return {sport:information[1].toUpperCase(),page:'information'};
      const visual=/^#(nba|nfl)-in-game-data$/.exec(hash||'');
      if(visual)return {sport:visual[1].toUpperCase(),page:'in-game'};
      const summary=/^#backtesting-summary(?:-(nba|nfl|ncaaf)-(game-outcome|player-stats)-[a-z]+-[0-9a-f]{64}(?:\?[^#]*)?)?$/.exec(hash||'');
      if(summary)return {sport:summary[1]?.toUpperCase()||null,page:'backtesting'};
      if(/^#backtesting(?:-(game-outcome|player-stats)-[a-z0-9-]+|-performance)?$/.test(hash||''))return {sport:null,page:'backtesting'};
      return null;
    },
    route: function(hash) {
      const summary=/^#(nba|nfl|ncaaf)-market-summary$/.exec(hash||'');
      if(summary)return {sport:summary[1].toUpperCase(),provider:'summary',game:null};
      const filtered=/^#(nba|nfl|ncaaf)-filtered-results$/.exec(hash||'');
      if(filtered)return {sport:filtered[1].toUpperCase(),provider:'filtered',game:null};
      let match=/^#(nba|nfl|ncaaf)-(games|game-(\d+))$/.exec(hash||'');
      if(match)return {sport:match[1].toUpperCase(),provider:'market',game:match[3]||null};
      match=/^#(nba|nfl|ncaaf)-(kalshi|polymarket)-(market|game-(.+))$/.exec(hash||'');
      if(!match)return null;
      const sport=match[1].toUpperCase(),game=match[4]||null;
      if(game && (match[2]==='polymarket' ? !/^\d+$/.test(game) :
          !/^[A-Z0-9][A-Z0-9_.-]{0,199}$/.test(game)||!game.startsWith('KX'+sport+'GAME-')))return null;
      return {sport,provider:match[2],game};
    },
    screen: function(hash,sport,game,kalshi,polymarket,scope,kalshiGames,polymarketGames,page,forecast,snapshot,category,kalshiView,kalshiCategory,polyView,polyCategory,historyScope,method,playerMethod,kalshiSnapshot,kalshiMethod,kalshiPlayerMethod,polySnapshot) {
      latest=[hash,sport,game,kalshi,polymarket,scope,kalshiGames,polymarketGames,page,forecast,snapshot,category,kalshiView,kalshiCategory,polyView,polyCategory,historyScope,method,playerMethod,kalshiSnapshot,kalshiMethod,kalshiPlayerMethod,polySnapshot];
      const special={RESEARCH:'backtesting',PNL:'pnl',ODDS_API:'odds'}[sport];
      if(special)return plain+' workspace-screen workspace-screen-'+special;
      const route=window.dash_clientside.market_transition.route(hash);
      if(!route) {
        const destination=window.dash_clientside.market_transition.destination(hash);
        const historyKind=/^#historical(?:-(team|player))?(?:\?|$)/.exec(hash||'')?.[1]||'team';
        if(destination&&(!destination.sport||destination.sport===sport))return plain+' workspace-screen workspace-screen-'+destination.page+
          (destination.page==='historical'&&(historyScope?.route!==hash||historyScope?.sport&&historyScope.sport!==sport||
            historyScope?.kind&&historyScope.kind!==historyKind)?' history-view-pending':'');
        return plain;
      }
      if(route.sport!==sport)return plain;
      if(route.provider==='summary')return 'app-shell market-screen market-screen-summary';
      if(route.provider==='filtered')return 'app-shell market-screen market-screen-filtered';
      const context={market:game,kalshi,polymarket}[route.provider];
      const catalog={market:scope,kalshi:kalshiGames,polymarket:polymarketGames}[route.provider];
      const ready=route.game ? context?.sport===route.sport&&String(context.id)===route.game : catalog?.sport===route.sport;
      const unavailable=route.game&&route.provider!=='market'&&catalog?.sport===route.sport&&Array.isArray(catalog.games)&&
        !catalog.games.some(game=>String(game.id)===route.game);
      const stale=ready&&route.game&&route.provider==='market'&&
        category!=='Mispricing'&&category!=='Additional Information'&&
        (forecast?.context?.[0]!==hash||forecast?.context?.[1]!==snapshot||forecast?.view_key?.[0]!==category||
          method!==undefined&&forecast?.view_key?.[1]!==method||playerMethod!==undefined&&forecast?.view_key?.[2]!==playerMethod);
      const exchangeView=route.provider==='kalshi'?kalshiView:polyView;
      const exchangeCategory=route.provider==='kalshi'?kalshiCategory:polyCategory;
      const exchangeStale=ready&&route.game&&['kalshi','polymarket'].includes(route.provider)&&exchangeCategory!=='Mispricing Filter'&&
        (exchangeView?.context?.[0]!==sport||String(exchangeView?.context?.[1])!==route.game||exchangeView?.context?.[2]!==exchangeCategory||
          route.provider==='kalshi'&&kalshiSnapshot!==undefined&&JSON.stringify(exchangeView?.view_scope)!==JSON.stringify([kalshiSnapshot,kalshiMethod,kalshiPlayerMethod])||
          route.provider==='polymarket'&&polySnapshot!==undefined&&exchangeView?.view_scope?.[0]!==polySnapshot);
      return 'app-shell market-screen market-screen-'+route.provider+' market-screen-'+(route.game?'detail':'list')+
        (ready?'':unavailable?' market-screen-unavailable':' market-screen-pending')+(stale?' market-quotes-pending':'')+
        (exchangeStale?' exchange-quotes-pending':'');
    },
    reconcile: function(...args) {
      // Ignore a delayed client callback for a URL the user has already left.
      if(window.location.hash&&args[0]!==window.location.hash)return window.dash_clientside.no_update;
      return window.dash_clientside.market_transition.screen(...args);
    }
  }
});

// A normal clientside callback can wait behind its data inputs. Handle the URL
// immediately; the registered callback reconciles visibility as stores arrive.
window.addEventListener('hashchange',()=>{
  const api=window.dash_clientside;
  if(!api?.set_props)return;
  const args=latest?latest.slice():Array(8).fill(null);
  args[0]=window.location.hash;
  const destination=api.market_transition.destination(args[0]);
  if(destination) {
    // Start page work immediately rather than waiting for the server router.
    // The registered hash-only clientside router owns these controls. This
    // bridge only paints immediately, avoiding duplicate callback cascades.
    args[1]=destination.sport||args[1];args[8]=destination.page;
  }
  api.set_props('app-shell',{className:api.market_transition.screen(...args)});
});
})();
