/* URL-driven visibility runs locally, before any data or forecasting callback. */
(() => {
let latest=null;
let shellLatest=null;
const guards={};
let historyRoute=null,historyHydrated=false,historyManual=false,historySelection=null;
const plain = 'app-shell';
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
window.dash_clientside = Object.assign({},window.dash_clientside,{
  market_transition: {
    shell: function(hash,sport,page) {
      if(window.location.hash&&hash!==window.location.hash)return window.dash_clientside.no_update;
      shellLatest=[hash,sport,page];
      const api=window.dash_clientside.market_transition,destination=api.destination(hash);
      const special={RESEARCH:'backtesting',PNL:'pnl',ODDS_API:'odds'}[sport],route=api.route(hash);
      if(special)return 'app-shell workspace-screen workspace-screen-'+special;
      if(route&&route.sport!==sport)return plain;
      if(route)return 'app-shell market-screen market-screen-'+route.provider+' market-screen-'+(route.game?'detail':'list');
      const target=destination?.page||page||'historical';
      return 'app-shell workspace-screen workspace-screen-'+target;
    },
    page_guard: function(provider,hash,sport,context,catalog,schedule) {
      if(window.location.hash&&hash!==window.location.hash)return window.dash_clientside.no_update;
      guards[provider]=[provider,hash,sport,context,catalog,schedule];
      const route=window.dash_clientside.market_transition.route(hash),base=provider==='market'?'market-page':'kalshi-page';
      if(!route||route.provider!==provider)return base;
      if(sport&&route.sport!==sport)return base+' market-sport-list'+(catalog?.sport===sport?'':' market-screen-pending');
      const ready=route.game?context?.sport===route.sport&&String(context.id)===route.game:catalog?.sport===route.sport;
      const games=provider==='market'?schedule:catalog?.games;
      const unavailable=!ready&&route.game&&catalog?.sport===route.sport&&!catalog.updating&&Array.isArray(games)&&
        !games.some(game=>String(game.id)===route.game);
      return base+(ready?'':unavailable?' market-screen-unavailable':' market-screen-pending');
    },
    market_guard: function(hash,selection,category,method,player,history) {
      if(window.location.hash&&hash!==window.location.hash)return window.dash_clientside.no_update;
      guards.marketResults=[hash,selection,category,method,player,history];
      const active=/^#(?:nba|nfl|ncaaf)-game-\d+$/.test(hash||'')&&!['Mispricing','Additional Information'].includes(category);
      const stale=active&&(!same(history?.context,[hash,selection])||history?.view_key?.[0]!==category);
      const model=active&&!stale&&(history?.view_key?.[1]!==method||history?.view_key?.[2]!==player||history?.pending&&!history?.keep_visible);
      return 'market-detail-view'+(stale?' market-quotes-pending':'')+(model?' market-model-pending':'')+
        (active&&!stale&&history?.pending&&history?.keep_visible?' market-view-updating':'');
    },
    quote_guard: function(provider,hash,selection,category,view,quotes) {
      if(window.location.hash&&hash!==window.location.hash)return window.dash_clientside.no_update;
      guards[provider+'Results']=[provider,hash,selection,category,view,quotes];
      const route=window.dash_clientside.market_transition.route(hash);
      const base='sportsbook-view kalshi-sportsbook';
      if(!route||route.provider!==provider||!route.game||category==='Mispricing Filter')return base;
      const matching=view?.context?.[0]===route.sport&&String(view?.context?.[1])===route.game&&view?.context?.[2]===category&&view?.view_scope?.[0]===selection;
      const q=quotes?.sport===route.sport&&String(quotes?.game_id)===route.game&&quotes?.category===category&&
        (quotes?.selection===undefined||quotes.selection===selection);
      return base+(!matching||q&&quotes.updating&&!quotes.rows?.length?' exchange-quotes-pending':'')+
        (matching&&q&&quotes.updating&&quotes.rows?.length?' market-view-updating':'');
    },
    model_guard: function(hash,selection,method,player,models) {
      if(window.location.hash&&hash!==window.location.hash)return window.dash_clientside.no_update;
      const route=window.dash_clientside.market_transition.route(hash),m=models?.context;
      const ready=route&&m?.[0]===route.sport&&String(m?.[1])===route.game&&m?.[8]===selection&&m?.[9]===method&&m?.[10]===player;
      return !ready||models?.pending&&!models?.keep_visible?'exchange-model-pending':'';
    },
    career_guard: function(figure,dataset,entity,metric,hash,sport,kind) {
      if(window.location.hash&&hash!==window.location.hash)return window.dash_clientside.no_update;
      const expected={route:hash||'',sport:dataset?.sport??null,kind:dataset?.kind??null,phase:dataset?.phase??null,entity:entity??null,metric:metric??null};
      const ready=same(figure?.layout?.meta?.career_scope,expected)&&!figure?.layout?.meta?.pending&&
        (!dataset||dataset.sport===sport&&dataset.kind===kind);
      return 'panel chart-panel career-panel'+(ready?'':' career-view-pending');
    },
    user_history_control: function(){historyManual=true;},
    history_state: function(figure,career,dataset,entity,metric,windowSize,team,hash,sport,kind,season,phase,week,sourceTeam,pending,previous) {
      if(window.location?.hash&&hash!==window.location.hash)return window.dash_clientside.no_update;
      if(hash&&!/^#historical/.test(hash))return window.dash_clientside.no_update;
      if(historyRoute!==hash){historyRoute=hash;historyHydrated=false;historyManual=false;historySelection=null;}
      const selection=[entity,metric,windowSize,team,sport,kind,season,phase,week,sourceTeam];
      if(historyManual&&historySelection&&!same(selection,historySelection)){historyHydrated=true;historyManual=false;}
      historySelection=selection;
      const d=dataset||{},requested={v:2,route:hash||'',key:d.key??null,revision:d.revision||d.fetched_at||null,
        sport:d.sport??null,kind:d.kind??null,season:d.season??null,phase:d.phase??null,
        week:d.week??null,source_team:d.team||'',entity:entity??null,metric:metric??null,window:windowSize??null,team:team??null};
      const rendered=figure?.layout?.meta?.analysis_scope,old=figure?.layout?.meta?.navigation_scope;
      const linked=new URLSearchParams((hash||'').split('?')[1]||'').has('name');
      const compatible=!!dataset&&d.sport===sport&&d.kind===kind&&Number(d.season)===Number(season)&&d.phase===phase&&
        (sport==='NBA'||(d.week??null)===(week??null))&&(sport!=='NCAAF'||kind!=='team'||(d.team||'')===(sourceTeam||''));
      const matched=compatible&&same(rendered,requested);
      if(matched&&(!linked||figure?.layout?.meta?.link_ready))historyHydrated=true;
      const empty=!dataset&&!pending&&(rendered?.route===(hash||'')||old?.route===(hash||''));
      const ready=empty||matched&&(!linked||historyHydrated);
      const expectedCareer={route:hash||'',sport:d.sport??null,kind:d.kind??null,phase:d.phase??null,
        entity:entity??null,metric:metric??null};
      const value={v:2,route:hash,sport,kind,ready,career_ready:empty||same(career?.layout?.meta?.career_scope,expectedCareer),
        updating:ready&&!!pending,requested};
      return same(value,previous)?window.dash_clientside.no_update:value;
    },
    select: function(hash,sport,page) {
      if(window.location.hash&&hash!==window.location.hash)return [window.dash_clientside.no_update,window.dash_clientside.no_update];
      const value=window.dash_clientside.market_transition.destination(hash),no=window.dash_clientside.no_update;
      if(!value)return [no,no];
      return [value.sport&&value.sport!==sport?value.sport:no,value.page!==page?value.page:no];
    },
    readiness: function(game,kalshi,poly,scope,kalshiGames,polyGames,forecast,kalshiView,polyView,previous,extra) {
      const context=value=>value?{sport:value.sport,id:value.id}:null;
      const catalog=value=>value?{sport:value.sport,...(Array.isArray(value.games)?{games:value.games.map(game=>({id:game.id}))}:{}),
        ...(value.updating?{updating:true}:{}),...(value.error?{error:true}:{})}:null;
      const view=value=>value?{context:value.context,view_scope:value.view_scope}:null;
      const value={game:context(game),kalshi:context(kalshi),poly:context(poly),scope:catalog(scope),
        kalshiGames:catalog(kalshiGames),polyGames:catalog(polyGames),forecast:forecast?{context:forecast.context,view_key:forecast.view_key,pending:!!forecast.pending,error:!!forecast.error,keep_visible:!!forecast.keep_visible}:null,
        kalshiView:view(kalshiView),polyView:view(polyView)};
      if(extra){
        const quote=q=>q?{sport:q.sport,id:q.game_id,category:q.category,selected:q.selection,updating:!!q.updating,
          has_rows:!!q.rows?.length,error:!!q.error}:null;
        value.status={kalshi:quote(extra.kq),polymarket:quote(extra.pq),models:extra.models?.context||null};
        if(value.scope&&Array.isArray(extra.schedule)){
          value.scope.games=extra.schedule.map(game=>({id:game.id}));value.scope.checked=typeof scope.checked_at==='number';
        }
      }
      return JSON.stringify(value)===JSON.stringify(previous)?window.dash_clientside.no_update:value;
    },
    compact: function(hash,sport,page,ready,snapshot,category,kalshiCategory,polyCategory,historyScope,method,playerMethod,kalshiSnapshot,kalshiMethod,kalshiPlayerMethod,polySnapshot) {
      const r=ready||{};
      return window.dash_clientside.market_transition.reconcile(hash,sport,r.game,r.kalshi,r.poly,r.scope,r.kalshiGames,r.polyGames,
        page,r.forecast,snapshot,category,r.kalshiView,kalshiCategory,r.polyView,polyCategory,historyScope,
        method,playerMethod,kalshiSnapshot,kalshiMethod,kalshiPlayerMethod,polySnapshot,r.status);
    },
    feedback: function(hash,classes,previous){
      const route=window.dash_clientside.market_transition.route(hash),name=route?{market:'FanDuel',kalshi:'Kalshi',polymarket:'Polymarket'}[route.provider]:null;
      const value=/market-screen-unavailable/.test(classes||'')?'Game unavailable. Use the game list’s refresh control, or choose another game.':
        /market-screen-pending/.test(classes||'')?'Loading '+(route?.sport||'')+' '+(name||'market')+(route?.game?' game…':' games…'):
        /quotes-pending/.test(classes||'')?'Loading the selected market category…':
        /model-pending/.test(classes||'')?'Quotes available · Calculating probabilities for the selected method…':
        /history-view-pending/.test(classes||'')?'Loading the selected historical performance…':
        /history-view-updating|market-view-updating/.test(classes||'')?'Updating · Previously loaded data remains visible with its timestamp.':'';
      return value===previous?window.dash_clientside.no_update:value;
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
    screen: function(hash,sport,game,kalshi,polymarket,scope,kalshiGames,polymarketGames,page,forecast,snapshot,category,kalshiView,kalshiCategory,polyView,polyCategory,historyScope,method,playerMethod,kalshiSnapshot,kalshiMethod,kalshiPlayerMethod,polySnapshot,status) {
      latest=[hash,sport,game,kalshi,polymarket,scope,kalshiGames,polymarketGames,page,forecast,snapshot,category,kalshiView,kalshiCategory,polyView,polyCategory,historyScope,method,playerMethod,kalshiSnapshot,kalshiMethod,kalshiPlayerMethod,polySnapshot,status];
      const special={RESEARCH:'backtesting',PNL:'pnl',ODDS_API:'odds'}[sport];
      if(special)return plain+' workspace-screen workspace-screen-'+special;
      const route=window.dash_clientside.market_transition.route(hash);
      if(!route) {
        const destination=window.dash_clientside.market_transition.destination(hash);
        const historyKind=/^#historical(?:-(team|player))?(?:\?|$)/.exec(hash||'')?.[1]||'team';
        if(destination&&(!destination.sport||destination.sport===sport))return plain+' workspace-screen workspace-screen-'+destination.page+
          (destination.page==='historical'&&(historyScope?.route!==hash||historyScope?.sport&&historyScope.sport!==sport||
            historyScope?.kind&&historyScope.kind!==historyKind||historyScope?.v===2&&!historyScope.ready)?' history-view-pending':'')+
          (destination.page==='historical'&&historyScope?.v===2&&!historyScope.career_ready?' career-view-pending':'')+
          (destination.page==='historical'&&historyScope?.updating?' history-view-updating':'');
        return plain;
      }
      if(route.sport!==sport)return plain;
      if(route.provider==='summary')return 'app-shell market-screen market-screen-summary';
      if(route.provider==='filtered')return 'app-shell market-screen market-screen-filtered';
      const context={market:game,kalshi,polymarket}[route.provider];
      const catalog={market:scope,kalshi:kalshiGames,polymarket:polymarketGames}[route.provider];
      const ready=route.game ? context?.sport===route.sport&&String(context.id)===route.game : catalog?.sport===route.sport;
      const unavailable=!ready&&route.game&&(route.provider!=='market'||catalog?.checked)&&!catalog?.updating&&catalog?.sport===route.sport&&Array.isArray(catalog.games)&&
        !catalog.games.some(game=>String(game.id)===route.game);
      const stale=ready&&route.game&&route.provider==='market'&&
        category!=='Mispricing'&&category!=='Additional Information'&&
        (forecast?.context?.[0]!==hash||forecast?.context?.[1]!==snapshot||forecast?.view_key?.[0]!==category);
      const modelStale=ready&&!stale&&route.provider==='market'&&category!=='Mispricing'&&category!=='Additional Information'&&
        (method!==undefined&&forecast?.view_key?.[1]!==method||playerMethod!==undefined&&forecast?.view_key?.[2]!==playerMethod||forecast?.pending&&!forecast?.keep_visible);
      const exchangeView=route.provider==='kalshi'?kalshiView:polyView;
      const exchangeCategory=route.provider==='kalshi'?kalshiCategory:polyCategory;
      const q=status?.[route.provider],quoteMatches=q?.sport===sport&&String(q.id)===route.game&&q.category===exchangeCategory;
      const exchangeStale=ready&&route.game&&['kalshi','polymarket'].includes(route.provider)&&exchangeCategory!=='Mispricing Filter'&&
        (exchangeView?.context?.[0]!==sport||String(exchangeView?.context?.[1])!==route.game||exchangeView?.context?.[2]!==exchangeCategory||
          route.provider==='kalshi'&&kalshiSnapshot!==undefined&&exchangeView?.view_scope?.[0]!==kalshiSnapshot||
          route.provider==='polymarket'&&polySnapshot!==undefined&&exchangeView?.view_scope?.[0]!==polySnapshot||quoteMatches&&q.updating&&!q.has_rows);
      const models=status?.models;
      const exchangeModelStale=ready&&!exchangeStale&&route.provider==='kalshi'&&exchangeCategory!=='Mispricing Filter'&&
        (exchangeView?.view_scope?.[1]!==kalshiMethod||exchangeView?.view_scope?.[2]!==kalshiPlayerMethod||
         status!==undefined&&(!models||models[0]!==sport||String(models[1])!==route.game||models[8]!==kalshiSnapshot||models[9]!==kalshiMethod||models[10]!==kalshiPlayerMethod));
      const updating=ready&&!exchangeStale&&quoteMatches&&q.updating;
      return 'app-shell market-screen market-screen-'+route.provider+' market-screen-'+(route.game?'detail':'list')+
        (ready?'':unavailable?' market-screen-unavailable':' market-screen-pending')+(stale?' market-quotes-pending':'')+
        (exchangeStale?' exchange-quotes-pending':'')+(modelStale?' market-model-pending':'')+
        (exchangeModelStale?' exchange-model-pending':'')+(updating||ready&&!stale&&forecast?.pending&&forecast?.keep_visible&&route.provider==='market'?' market-view-updating':'');
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
  if(shellLatest){
    const api=window.dash_clientside.market_transition,hash=window.location.hash;
    for(const provider of ['market','kalshi','polymarket']){
      const state=guards[provider]?.slice()||[provider,hash,null,null,null];state[1]=hash;
      state[2]=api.destination(hash)?.sport||state[2];
      window.dash_clientside.set_props(provider+'-page',{className:api.page_guard(...state)});
      const results=guards[provider==='market'?'marketResults':provider+'Results']?.slice();
      if(results){results[provider==='market'?0:1]=hash;
        window.dash_clientside.set_props(provider==='market'?'market-game-detail':provider+'-detail',{
          className:provider==='market'?api.market_guard(...results):api.quote_guard(...results)});}
    }
    window.dash_clientside.set_props('historical-results',{className:'historical-results history-view-pending'});
    window.dash_clientside.set_props('career-panel',{className:'panel chart-panel career-panel career-view-pending'});
    const destination=api.destination(hash);
    window.dash_clientside.set_props('app-shell',{className:api.shell(hash,destination?.sport||shellLatest[1],destination?.page||shellLatest[2])});
    return;
  }
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
