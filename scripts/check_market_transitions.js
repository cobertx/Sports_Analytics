/* Exercise actual browser-side screen selection without data requests or DOM replacement. */
function checkMarketTransitions(source) {
  let checks=0;
  const check=(ok,message)=>{if(!ok)throw new Error(message);checks++;};
  const events={},writes=[];
  const window={location:{hash:''},dash_clientside:{set_props:(id,props)=>writes.push({id,props})},
    addEventListener:(name,handler)=>{events[name]=handler;}};
  new Function('window',source)(window);
  const {screen,route}=window.dash_clientside.market_transition;
  for(const sport of ['NBA','NFL','NCAAF']) {
    const slug=sport.toLowerCase(),identity='KX'+sport+'GAME-TEST';
    const contexts=[{sport,id:'123'},{sport,id:identity},{sport,id:'456'}];
    const catalogs=[{sport},{sport},{sport}];
    check(route('#'+slug+'-market-summary').provider==='summary','Route each sport summary');
    check(screen('#'+slug+'-market-summary',sport)==='app-shell market-screen market-screen-summary','Show summary without waiting on provider contexts');
    check(route('#'+slug+'-filtered-results').provider==='filtered','Route each sport filtered results');
    check(screen('#'+slug+'-filtered-results',sport)==='app-shell market-screen market-screen-filtered','Show local results without provider contexts');
    for(const provider of ['market','kalshi','polymarket']) {
      const index=['market','kalshi','polymarket'].indexOf(provider);
      const list=provider==='market'?'#'+slug+'-games':'#'+slug+'-'+provider+'-market';
      const detail=provider==='market'?'#'+slug+'-game-123':'#'+slug+'-'+provider+'-game-'+contexts[index].id;
      for(const hash of [list,detail]) {
        const shown=screen(hash,sport,...contexts,...catalogs);
        check(shown.includes('market-screen-'+provider),'Select the requested provider immediately');
        check(shown.includes(hash===list?'market-screen-list':'market-screen-detail'),'Choose list/detail without a server result');
        check(!shown.includes('market-screen-pending'),'Show an already-loaded game header immediately');
        check(route(hash).sport===sport,'Keep sport-specific routes');
      }
      const wrong=contexts.map(c=>({...c,id:'wrong'}));
      check(screen(detail,sport,...wrong,...catalogs).includes('pending'),'Hide the previous game until the correct context arrives');
      check(screen(list,sport,...contexts,null,null,null).includes('pending'),'Show a local loading status on cold entry');
        check(!screen(list,sport,...contexts,...catalogs).includes('market-screen-pending'),'Reveal the list when its catalog arrives');
    }
    check(screen('#'+slug+'-games','RESEARCH',...contexts,...catalogs).includes('workspace-screen-backtesting'),'Show Research immediately on a sport-tab change');
    check(screen('#'+slug+'-games','ODDS_API',...contexts,...catalogs).includes('workspace-screen-odds'),'Show Odds API immediately on a sport-tab change');
  }
  for(const hash of ['#nba-kalshi-game-KXNFLGAME-TEST',
      '#nba-kalshi-game-../bad','#nba-polymarket-game-abc','#nfl-game-123?extra=1']) {
    check(route(hash)===null,'Only valid market URLs own market visibility');
    check(screen(hash,'NBA')==='app-shell','Leave other workspaces under normal navigation');
  }
  check(screen('#nba-games','NFL')==='app-shell','A sport-tab change cannot reveal the previous sport catalog');
  check(screen('#nba-kalshi-game-KXNBAGAME-TEST','NBA',null,null,null,null,{sport:'NBA',games:[]})
    .includes('market-screen-unavailable'),'An absent game shows an unavailable status instead of an endless loader');
  const context=Object.freeze({sport:'NBA',id:'123'}),catalog=Object.freeze({sport:'NBA'});
  for(let i=0;i<1000;i++)screen('#nba-game-123','NBA',context,null,null,catalog);
  check(context.id==='123'&&catalog.sport==='NBA','Repeated switches never mutate game, category or snapshot state');
  // No Dash callback or server response occurs between these native URL events.
  screen('#nba-games','NBA',context,{sport:'NBA',id:'KXNBAGAME-TEST'},{sport:'NBA',id:'456'},catalog,catalog,catalog);
  for(const hash of ['#nba-kalshi-game-KXNBAGAME-TEST','#nba-market-summary','#nba-polymarket-game-456','#nba-game-123']) {
    window.location.hash=hash;events.hashchange();
    const updated=writes.at(-1);
    check(updated.id==='app-shell','Native navigation changes only the screen visibility prop');
    check(updated.props.className.includes('market-screen-'+route(hash).provider),'Switch providers before pending server callbacks finish');
    check(!updated.props.className.includes('market-screen-pending'),'Reuse the retained provider context during rapid switches');
  }
  window.location.hash='#historical-team';events.hashchange();
  check(writes.at(-1).props.className.includes('workspace-screen-historical'),'Leaving markets shows Historical Data before server callbacks');
  const api=window.dash_clientside.market_transition;
  for(const [hash,sport,page] of [
    ['#historical-player?sport=NFL&name=Jerry+Jeudy','NFL','historical'],
    ['#historical-team','NBA','historical'],['#backtesting-performance','NBA','backtesting'],
    ['#backtesting-player-stats-oam','NBA','backtesting'],['#research','RESEARCH','backtesting'],
    ['#nba-in-game-data','NBA','in-game'],['#nfl-information-weather','NFL','information'],
    ['#nba-information-roster','NBA','information'],['#pnl-history','PNL','pnl'],
    ['#backtesting-summary-nfl-player-stats-whp-'+'a'.repeat(64),'NFL','backtesting'],
    ['#backtesting-summary-nba-game-outcome-elo-'+'a'.repeat(64)+'?target=total&basis=all','NBA','backtesting'],
    ['#backtesting-summary-nfl-game-outcome-elo-'+'a'.repeat(64)+'?target=margin&basis=common&reports=elo%3A'+'a'.repeat(64),'NFL','backtesting']
  ]) {
    check(api.destination(hash).page===page,'Resolve destination without a server request');
    check(screen(hash,sport).includes('workspace-screen-'+page),'Hide the prior page immediately for '+hash);
  }
  for(const hash of ['#nba-information-weather','#nfl-information-roster','#unrecognized'])check(api.destination(hash)===null,'Reject unsupported page routes');
  const info={context:['#nba-game-123','snap'],view_key:['Points']};
  const detailArgs=['#nba-game-123','NBA',context,null,null,catalog,null,null,'market',info,'snap','Points'];
  check(!screen(...detailArgs).includes('market-quotes-pending'),'Keep current quote headers visible and interactive');
  for(const [index,value] of [[9,{...info,context:['#nba-game-456','snap']}],[10,'other-snapshot'],[11,'Assists'],[9,null],[10,null]]) {
    const stale=detailArgs.slice();stale[index]=value;
    check(screen(...stale).includes('market-quotes-pending'),'Hide quotes belonging to the previous game, snapshot or category');
  }
  for(const [provider,id,index] of [['kalshi','KXNBAGAME-TEST',12],['polymarket','456',14]]) {
    const args=['#nba-'+provider+'-game-'+id,'NBA',null,{sport:'NBA',id:'KXNBAGAME-TEST'},{sport:'NBA',id:'456'},null,null,null,provider,null,null,null];
    args[index]={context:['NBA',id,'Game Lines']};args[index+1]='Game Lines';
    check(!screen(...args).includes('exchange-quotes-pending'),'Reveal only the exchange game that finished rendering');
    args[index]={context:['NBA','wrong-game','Game Lines']};
    check(screen(...args).includes('exchange-quotes-pending'),'Hide exchange rows from the last game even when the new header is ready');
    args[index]={context:['NBA',id,'Passing Props']};
    check(screen(...args).includes('exchange-quotes-pending'),'Hide exchange rows from the previous category');
  }
  window.location.hash='#historical-player?sport=NFL&name=Jerry+Jeudy';events.hashchange();
  check(writes.some(value=>value.id==='sport'&&value.props.value==='NFL'),'Update sport immediately on a cross-sport history link');
  check(writes.some(value=>value.id==='workspace-page'&&value.props.value==='historical'),'Start Historical Data work locally');
  const noUpdate={};window.dash_clientside.no_update=noUpdate;
  check(api.reconcile(...detailArgs)===noUpdate,'An outdated URL callback cannot restore the previous market page');
  const count=writes.length;delete window.dash_clientside.set_props;events.hashchange();
  check(writes.length===count,'Startup before Dash initializes the event bridge is safe');
  return checks;
}

if(typeof module!=='undefined'&&module.exports) {
  const fs=require('node:fs'),path=require('node:path');
  const source=fs.readFileSync(path.join(__dirname,'../sports_analytics/assets/market_transition.js'),'utf8');
  console.log(`Passed ${checkMarketTransitions(source)} market transition checks.`);
}
