/* URL-driven visibility runs locally, before any data or forecasting callback. */
(() => {
let latest=null;
window.dash_clientside = Object.assign({},window.dash_clientside,{
  market_transition: {
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
    screen: function(hash,sport,game,kalshi,polymarket,scope,kalshiGames,polymarketGames) {
      latest=[hash,sport,game,kalshi,polymarket,scope,kalshiGames,polymarketGames];
      const route=window.dash_clientside.market_transition.route(hash);
      if(!route||route.sport!==sport)return 'app-shell';
      if(route.provider==='summary')return 'app-shell market-screen market-screen-summary';
      if(route.provider==='filtered')return 'app-shell market-screen market-screen-filtered';
      const context={market:game,kalshi,polymarket}[route.provider];
      const catalog={market:scope,kalshi:kalshiGames,polymarket:polymarketGames}[route.provider];
      const ready=route.game ? context?.sport===route.sport&&String(context.id)===route.game : catalog?.sport===route.sport;
      const unavailable=route.game&&route.provider!=='market'&&catalog?.sport===route.sport&&Array.isArray(catalog.games)&&
        !catalog.games.some(game=>String(game.id)===route.game);
      return 'app-shell market-screen market-screen-'+route.provider+' market-screen-'+(route.game?'detail':'list')+
        (ready?'':unavailable?' market-screen-unavailable':' market-screen-pending');
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
  const route=api.market_transition.route(args[0]);
  if(route)args[1]=route.sport;
  api.set_props('app-shell',{className:api.market_transition.screen(...args)});
});
})();
