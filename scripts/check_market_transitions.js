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
    for(const provider of ['market','kalshi','polymarket']) {
      const index=['market','kalshi','polymarket'].indexOf(provider);
      const list=provider==='market'?'#'+slug+'-games':'#'+slug+'-'+provider+'-market';
      const detail=provider==='market'?'#'+slug+'-game-123':'#'+slug+'-'+provider+'-game-'+contexts[index].id;
      for(const hash of [list,detail]) {
        const shown=screen(hash,sport,...contexts,...catalogs);
        check(shown.includes('market-screen-'+provider),'Select the requested provider immediately');
        check(shown.includes(hash===list?'market-screen-list':'market-screen-detail'),'Choose list/detail without a server result');
        check(!shown.includes('pending'),'Show an already-loaded view immediately');
        check(route(hash).sport===sport,'Keep sport-specific routes');
      }
      const wrong=contexts.map(c=>({...c,id:'wrong'}));
      check(screen(detail,sport,...wrong,...catalogs).includes('pending'),'Hide the previous game until the correct context arrives');
      check(screen(list,sport,...contexts,null,null,null).includes('pending'),'Show a local loading status on cold entry');
      check(screen(list,sport,...contexts,...catalogs).indexOf('pending')===-1,'Reveal the list when its catalog arrives');
    }
    check(screen('#'+slug+'-games','RESEARCH',...contexts,...catalogs)==='app-shell','Do not override Research tab visibility');
    check(screen('#'+slug+'-games','ODDS_API',...contexts,...catalogs)==='app-shell','Do not override Odds API visibility');
  }
  for(const hash of ['#historical-team','#backtesting','#nba-in-game-data','#research','#nba-kalshi-game-KXNFLGAME-TEST',
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
    check(!updated.props.className.includes('pending'),'Reuse the retained provider context during rapid switches');
  }
  window.location.hash='#historical-team';events.hashchange();
  check(writes.at(-1).props.className==='app-shell','Leaving markets immediately releases the market visibility override');
  const count=writes.length;delete window.dash_clientside.set_props;events.hashchange();
  check(writes.length===count,'Startup before Dash initializes the event bridge is safe');
  return checks;
}

if(typeof module!=='undefined'&&module.exports) {
  const fs=require('node:fs'),path=require('node:path');
  const source=fs.readFileSync(path.join(__dirname,'../sports_analytics/assets/market_transition.js'),'utf8');
  console.log(`Passed ${checkMarketTransitions(source)} market transition checks.`);
}
