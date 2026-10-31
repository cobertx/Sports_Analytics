/* Pure event/DOM harness: no browser, source requests or installed JS packages. */
function checkMarketNavigation(source) {
  let checks = 0;
  const check = (condition, message) => { if (!condition) throw new Error(message); checks++; };
  const documentEvents = {}, windowEvents = {}, writes = [], scrolls = [];
  const location = {hash: '#nfl-game-123'};
  const nodes = {};
  const panel = {getClientRects: () => [{}]};
  nodes['market-game-detail'] = panel;
  let observer;
  const document = {
    documentElement: {},
    getElementById: id => nodes[id],
    addEventListener: (name, callback) => { documentEvents[name] = callback; }
  };
  const noUpdate = {};
  const window = {
    dash_clientside: {no_update: noUpdate, set_props: (id, props) => writes.push({id, props})},
    addEventListener: (name, callback) => { windowEvents[name] = callback; }
  };
  function MutationObserver(callback) { observer = callback; this.observe = () => {}; }
  const style = element => ({display: element.hidden ? 'none' : 'block', visibility: element.loading ? 'hidden' : 'visible'});
  new Function('window', 'document', 'location', 'MutationObserver', 'getComputedStyle', 'requestAnimationFrame', source)(
    window, document, location, MutationObserver, style, callback => callback());
  const remember = window.dash_clientside.market_navigation.remember;
  const sectionId = 'market-section-abc';
  const element = id => ({id, tagName: 'DETAILS', open: false, isConnected: true,
    closest: () => null, getClientRects() { return this.hidden ? [] : [{}]; }, scrollIntoView: () => scrolls.push(id)});
  const anchor = {closest: () => ({id: sectionId})};
  const click = overrides => documentEvents.click({target: {closest: selector => selector === 'a.market-history-link' ? anchor : null}, button: 0, ...overrides});
  const change = (oldHash, newHash) => {
    location.hash = newHash;
    windowEvents.hashchange({oldURL: 'https://local.invalid/' + oldHash});
  };

  let saved = remember(location.hash, 'saved-snapshot', 'Receiving', {NBA: {route: '#nba-game-456'}});
  check(saved.NFL.route === location.hash && saved.NFL.snapshot === 'saved-snapshot', 'Remember exact game and snapshot');
  check(saved.NFL.category === 'Receiving' && saved.NBA.route === '#nba-game-456', 'Keep category and separate sport context');
  check(remember(location.hash, 'saved-snapshot', 'Receiving', saved) === noUpdate, 'Avoid redundant store updates');
  click({ctrlKey: true});
  check(writes.length === 0, 'Do not capture new-tab clicks');
  click();
  check(writes.length === 1 && writes[0].id === 'market-return', 'Capture header link before navigation');
  saved = writes[0].props.data;
  check(saved.NFL.section === sectionId, 'Capture exact player/market section');
  change('#nfl-game-123', '#historical-player?sport=NFL&name=Jerry+Jeudy');
  check(remember(location.hash, null, null, saved) === noUpdate, 'Historical callbacks preserve the market origin');
  change('#historical-player?sport=NFL&name=Jerry+Jeudy', '#nfl-game-123');
  check(scrolls.length === 0, 'Wait for market content to exist');
  const market = element(sectionId);market.loading = true;nodes[sectionId] = market;
  observer();
  check(scrolls.length === 0, 'Wait for loading content to become visible');
  market.loading = false;observer();
  check(market.open && scrolls.length === 1 && scrolls[0] === sectionId, 'Open and scroll to origin market');
  observer();
  check(scrolls.length === 1, 'Do not repeatedly scroll an unchanged market node');
  const replacement = element(sectionId);nodes[sectionId] = replacement;observer();
  check(replacement.open && scrolls.length === 2, 'Restore after Dash replaces the market node');
  replacement.loading = true;observer();replacement.loading = false;observer();
  check(scrolls.length === 3, 'Restore once more when an existing node finishes reloading');
  windowEvents.wheel();nodes[sectionId] = element(sectionId);observer();
  check(scrolls.length === 3, 'Stop restoring when the user scrolls');
  saved = remember(location.hash, 'different-snapshot', 'Receiving', saved);
  check(saved.NFL.section === null, 'Do not reuse a section from a different snapshot');
  check(remember('#nfl-games', null, null, saved) === noUpdate, 'Schedule navigation preserves the last game');
  check(remember('#nba-game-999', 'nba-snapshot', 'Points', saved).NBA.route === '#nba-game-999', 'Capture NBA categories independently');
  const beforeHistory=scrolls.length;
  nodes['historical-performance-panel']=element('historical-performance-panel');
  change('#nfl-game-123','#historical-player?sport=NFL&name=Jerry+Jeudy');
  check(scrolls.length===beforeHistory+1&&scrolls.at(-1)==='historical-performance-panel','Open linked player history at the performance chart');
  return checks;
}

function checkDeferredNavigation(source,transitionSource) {
  let checks=0;
  const check=(ok,message)=>{if(!ok)throw new Error(message);checks++;};
  const windowEvents={},documentEvents={},frames=[],scrolls=[],focuses=[],writes=[];
  const location={hash:'#nfl-game-123'};
  let observer,observerOptions;
  const element=id=>({id,isConnected:true,tagName:'DIV',hidden:false,loading:false,
    getClientRects(){return this.hidden?[]:[{}];},closest(){return this.loading?{}:null;},
    scrollIntoView(){scrolls.push(id);},focus(options){focuses.push({id,options});}});
  const nodes=Object.fromEntries(['dashboard-main','market-game-detail','market-game-header',
    'historical-performance-panel','market-section-test'].map(id=>[id,element(id)]));
  nodes['market-section-test'].tagName='DETAILS';
  const links=['historical-page-link','market-page-link','kalshi-page-link','backtesting-page-link'].map(id=>({
    id,attrs:{href:'#unused'},getAttribute(key){return this.attrs[key];},setAttribute(key,value){this.attrs[key]=value;}}));
  const document={documentElement:{},getElementById:id=>nodes[id],querySelectorAll:()=>links,
    addEventListener:(name,handler)=>{documentEvents[name]=handler;}};
  const window={location,dash_clientside:{no_update:{},set_props:(id,props)=>writes.push({id,props})},
    addEventListener:(name,handler)=>{(windowEvents[name]||=[]).push(handler);}};
  function MutationObserver(callback){observer=callback;this.observe=(_node,options)=>{observerOptions=options;};}
  const raf=callback=>frames.push(callback);
  const flush=()=>{for(let i=0;frames.length&&i<20;i++)frames.shift()();check(!frames.length,'Animation frames settle');};
  const emit=(name,event={})=>(windowEvents[name]||[]).forEach(fn=>fn(event));
  const change=hash=>{const oldURL='http://local.invalid/'+location.hash;location.hash=hash;emit('hashchange',{oldURL});};
  new Function('window',transitionSource)(window);
  new Function('window','document','location','MutationObserver','getComputedStyle','requestAnimationFrame',source)(
    window,document,location,MutationObserver,node=>({display:node.hidden?'none':'block',visibility:node.loading?'hidden':'visible'}),raf);
  const remember=window.dash_clientside.market_navigation.remember;
  remember(location.hash,'snapshot','Passing',{});flush();
  check(observerOptions.attributeFilter.includes('class'),'Visibility-class changes wake delayed restoration');
  nodes['historical-performance-panel'].hidden=true;
  change('#historical-player?sport=NFL&name=Player+A');
  change('#historical-player?sport=NFL&name=Player+B');
  for(let i=0;i<100;i++)observer([{target:{id:'app-shell'}}]);
  check(frames.length===1,'One frame handles a burst of mutations and rapid route changes');flush();
  check(scrolls.length===0,'Do not scroll to a chart hidden by the route guard');
  check(focuses.length===1&&focuses[0].options.preventScroll,'Focus the content region once without disrupting scroll restoration');
  check(links[0].attrs['aria-current']==='page'&&links[1].attrs['aria-current']==='false','Active-route accessibility updates without a server response');
  nodes['historical-performance-panel'].hidden=false;
  observer([{target:{id:'app-shell'}}]);flush();
  check(scrolls.at(-1)==='historical-performance-panel','Scroll once when delayed historical content becomes visible');
  const scrollCount=scrolls.length;
  observer([{target:{id:'unrelated'}}]);
  check(frames.length===0&&scrolls.length===scrollCount,'Unrelated updates do not rescan or move settled history');
  change('#historical-player?sport=NFL&name=Player+C');emit('keydown',{key:'Tab'});flush();
  check(scrolls.length===scrollCount&&focuses.length===1,'A user keyboard action cancels pending focus and automatic scroll');
  const originalHash=location.hash;
  documentEvents.click({target:{closest:selector=>selector==='#skip-navigation'?{}:null}});
  check(location.hash===originalHash&&focuses.at(-1).id==='dashboard-main','Skip navigation focuses content without changing the routed URL');
  change('#nfl-game-123');flush();
  const anchor={closest:()=>nodes['market-section-test']};
  documentEvents.click({button:0,target:{closest:selector=>selector==='a.market-history-link'?anchor:null}});
  check(writes.some(item=>item.id==='market-return'&&item.props.data.NFL.section==='market-section-test'),'Store the exact market before leaving');
  change('#historical-player?sport=NFL&name=Player+B');flush();
  nodes['market-game-detail'].hidden=true;
  change('#nfl-game-123');flush();
  check(!nodes['market-section-test'].open,'Back navigation waits until the market is visible');
  nodes['market-game-detail'].hidden=false;observer([{target:{id:'app-shell'}}]);flush();
  check(nodes['market-section-test'].open&&scrolls.at(-1)==='market-section-test','Back navigation restores the original expanded prompt');
  emit('wheel');observer([{target:{id:'unrelated'}}]);
  check(frames.length===0,'User scrolling ends restoration work');
  change('#historical-player?sport=NFL&name=Player+D');
  change('#nfl-kalshi-market');flush();
  check(links[2].attrs['aria-current']==='page'&&links[0].attrs['aria-current']==='false','Rapid forward navigation leaves only the latest page selected');
  return checks;
}

if (typeof module !== 'undefined' && module.exports) {
  const fs = require('node:fs');
  const path = require('node:path');
  const source = fs.readFileSync(path.join(__dirname, '../sports_analytics/assets/market_navigation.js'), 'utf8');
  const transitionSource=fs.readFileSync(path.join(__dirname,'../sports_analytics/assets/market_transition.js'),'utf8');
  console.log(`Passed ${checkMarketNavigation(source)+checkDeferredNavigation(source,transitionSource)} market navigation checks.`);
}
