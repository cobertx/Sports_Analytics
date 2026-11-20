/* Real navigation assets in a controlled DOM; no requests or JS dependencies. */
const fs=require('node:fs'),path=require('node:path');
const assets=name=>fs.readFileSync(path.join(__dirname,'../app/assets/js',name),'utf8');
let checks=0;
const check=(ok,label)=>{if(!ok)throw Error(label);checks++;};
function setup(){
  const events={},docEvents={},frames=[],writes=[],scrolls=[],nodes={};let observe;
  const location={hash:'#nfl-game-123'};
  const element=id=>({id,isConnected:true,open:false,tagName:'DIV',contains:()=>true,
    getClientRects(){return this.hidden?[]:[{}];},closest:()=>null,scrollIntoView(){scrolls.push(id);},focus(){}});
  for(const id of ['app-shell','dashboard-main','market-game-detail','market-game-header','kalshi-detail','kalshi-game-header',
    'polymarket-detail','polymarket-game-header','historical-performance-panel','prompt'])nodes[id]=element(id);
  nodes.prompt.tagName='DETAILS';
  const document={documentElement:{},getElementById:id=>nodes[id],querySelectorAll:()=>[],
    addEventListener:(name,fn)=>docEvents[name]=fn};
  const window={location,dash_clientside:{no_update:{},set_props:(id,props)=>{
    writes.push({id,props});if(id==='app-shell')nodes[id].className=props.className;
  }},addEventListener:(name,fn)=>(events[name]||=[]).push(fn)};
  const emit=(name,data={})=>(events[name]||[]).forEach(fn=>fn(data));
  const flush=()=>{let n=0;while(frames.length&&n++<30)frames.shift()();check(!frames.length,'Frame queue settles');};
  function MutationObserver(fn){observe=fn;this.observe=()=>{};}
  new Function('window',assets('market_transition.js'))(window);
  new Function('window','document','location','MutationObserver','getComputedStyle','requestAnimationFrame',assets('market_navigation.js'))(
    window,document,location,MutationObserver,n=>({display:n.hidden?'none':'block',visibility:'visible'}),fn=>frames.push(fn));
  const change=hash=>{const oldURL='http://local.invalid/'+location.hash;location.hash=hash;emit('hashchange',{oldURL});};
  return {window,location,nodes,writes,scrolls,docEvents,change,emit,flush,observe:()=>observe([])};
}

for(const provider of ['market','kalshi','polymarket']){
  const h=setup(),api=h.window.dash_clientside,game={market:'123',kalshi:'KXNFLGAME-TEST',polymarket:'456'}[provider];
  const route=provider==='market'?'#nfl-game-'+game:'#nfl-'+provider+'-game-'+game;
  h.location.hash=route;
  const args=[route,'NFL',null,null,null,null,null,null,provider];
  args[{market:2,kalshi:3,polymarket:4}[provider]]={sport:'NFL',id:game};
  if(provider==='market'){args[9]={context:[route,'snapshot'],view_key:['Receiving','PPP','WHP']};args[10]='snapshot';args[11]='Receiving';}
  else{const i=provider==='kalshi'?12:14;args[i]={context:['NFL',game,'Receiving']};args[i+1]='Receiving';}
  h.nodes['app-shell'].className=api.market_transition.screen(...args);
  const all={[provider]:{snapshot:'snapshot',category:'Receiving',context:{sport:'NFL',id:game},method:'PPP',player_method:'WHP'}};
  let saved=api.market_navigation.remember(route,'snapshot','Receiving',{},all);h.flush();
  check(saved.v===2&&saved.entries[provider+':NFL'].route===route,'Provider-scoped return state');
  const anchor={closest:()=>h.nodes.prompt,getAttribute:()=> '#historical-player?sport=NFL&name=Player'};
  let count=56,waiting=false,moreClicks=0;
  h.nodes.prompt.querySelectorAll=selector=>selector==='.market-outcome'?Array(count).fill({}):[];
  const more={getClientRects:()=>[{}],click:()=>{moreClicks++;count=56;h.observe();}};
  h.nodes.prompt.querySelector=selector=>selector.includes('data-ready')?(waiting?{}:null):selector==='button.market-load-more'?more:null;
  h.docEvents.click({button:0,target:{closest:selector=>selector==='a.market-history-link'?anchor:null}});
  saved=h.writes.findLast(w=>w.id==='market-return').props.data;
  check(saved.entries[provider+':NFL'].section==='prompt','Capture exact prompt for '+provider);
  h.change('#historical-player?sport=NFL&name=Player');h.flush();count=0;waiting=true;h.change(route);h.flush();
  check(h.scrolls.at(-1)!=='prompt','Wait for lazy rows before restoring the prompt');
  waiting=false;count=6;h.observe();h.flush();
  check(moreClicks===1,'Restore the previously expanded row batch once');
  check(h.nodes.prompt.open&&h.scrolls.at(-1)==='prompt','Return opens the original provider prompt');
  const before=h.scrolls.length;h.emit('wheel');h.observe();h.flush();
  check(h.scrolls.length===before,'User scrolling cancels restoration');
  check(api.market_navigation.href(saved,'NFL',provider)===route,'Provider sidebar returns to its game');
  const other={...all,[provider]:{...all[provider],context:{sport:'NFL',id:'wrong'}}};
  check(api.market_navigation.remember(route,'snapshot','Receiving',saved,other)===api.no_update,'Reject stale controls from another game');
  if(provider==='kalshi'){
    const failed={...all,kalshi:{...all.kalshi,snapshot:'live'}};
    h.change('#historical-player?sport=NFL&name=Player');h.flush();h.change(route);
    api.market_navigation.remember(route,'snapshot','Receiving',saved,failed);h.flush();
    check(h.writes.some(w=>w.id==='market-return-status'&&w.props.children.includes('unavailable')),'Missing saved view has an explicit fallback');
  }
}

const h=setup(),api=h.window.dash_clientside.market_transition;
const d={key:'player-data',revision:'v1',sport:'NBA',kind:'player',season:2026,phase:'regular'};
const hash='#historical-player?sport=NBA&name=Player';h.location.hash=hash;
const scope={v:2,route:hash,key:d.key,revision:'v1',sport:'NBA',kind:'player',season:2026,phase:'regular',week:null,source_team:'',entity:'p',metric:'pts',window:5,team:'__all__'};
const fig={layout:{meta:{analysis_scope:scope,link_ready:true}}};
const career={layout:{meta:{career_scope:{route:hash,sport:'NBA',kind:'player',phase:'regular',entity:'p',metric:'pts'}}}};
const args=[fig,career,d,'p','pts',5,'__all__',hash,'NBA','player',2026,'regular',null,null,false,null];
check(api.history_state(...args).ready,'Matching linked history becomes ready');
for(const [index,value] of [[3,'other'],[4,'reb'],[5,10],[6,'OTHER'],[10,2025],[11,'postseason'],[2,{...d,revision:'v2'}]]){
  const changed=args.slice();changed[index]=value;
  check(!api.history_state(...changed).ready,'Different entity/stat/window/team/query/revision rejects old history');
}
const refreshing=args.slice();refreshing[14]=true;
check(api.history_state(...refreshing).ready&&api.history_state(...refreshing).updating,'Same-scope refresh keeps the valid result');
const oldCareer=args.slice();oldCareer[1]={};
check(api.history_state(...oldCareer).ready&&!api.history_state(...oldCareer).career_ready,'Career uses its own readiness gate');
h.location.hash=hash+'&market=player_rebounds';
check(api.history_state(...args)===h.window.dash_clientside.no_update,'Late callback cannot replace the current history intent');
const empty=args.slice();empty[0]={layout:{meta:{analysis_scope:{route:h.location.hash}}}};empty[2]=null;empty[7]=h.location.hash;
check(api.history_state(...empty).ready,'Failed/empty history terminates its placeholder');
const manual=args.slice();manual[7]=hash+'&market=player_points';h.location.hash=manual[7];
manual[0]={layout:{meta:{analysis_scope:{...scope,route:manual[7]},link_ready:false}}};
check(!api.history_state(...manual).ready,'Hydration cannot reveal an unconfirmed linked selection');
api.user_history_control();manual[4]='reb';manual[0]={layout:{meta:{analysis_scope:{...scope,route:manual[7],metric:'reb'},link_ready:false}}};
check(api.history_state(...manual).ready,'An explicit control change during hydration can choose a different statistic');
for(const [sport,entity,metric,week,school] of [
  ['NBA','Cleveland Cavaliers','pts',null,''],['NFL','Cleveland Browns','pf',7,''],
  ['NCAAF','Michigan','points',0,'Michigan'],
]){
  const route=`#historical-team?sport=${sport}&name=${encodeURIComponent(entity)}`;
  h.location.hash=route;
  const teamData={...d,key:entity,sport,kind:'team',week,team:school};
  const teamScope={...scope,route,key:entity,sport,kind:'team',week,source_team:school,entity,metric,team:null};
  const teamFigure={layout:{meta:{analysis_scope:teamScope,link_ready:true}}};
  const teamArgs=[teamFigure,null,teamData,entity,metric,5,null,route,sport,'team',2026,'regular',week,school+' ',false,null];
  check(api.history_state(...teamArgs).ready,`${sport} linked team scoring renders without a player-team selection`);
  const legacy=Object.fromEntries(Object.entries({...teamScope,team:'__all__'}).reverse());
  const legacyFigure={layout:{meta:{analysis_scope:legacy,link_ready:true}}};
  check(api.history_state(legacyFigure,...teamArgs.slice(1)).ready,`${sport} accepts retained team results with legacy metadata`);
  const stale=teamArgs.slice();stale[3]='another team';
  check(!api.history_state(...stale).ready,`${sport} team guard still rejects another team's chart`);
  if(sport==='NCAAF'){
    const wrongSchool=teamArgs.slice();wrongSchool[13]='Ohio State';
    check(!api.history_state(...wrongSchool).ready,'College history still checks the source school');
  }
}
const errorArgs=['#nba-kalshi-game-KXNBAGAME-TEST','NBA',null,{sport:'NBA',id:'KXNBAGAME-TEST'},null,null,null,null,'kalshi'];
errorArgs[12]={context:['NBA','KXNBAGAME-TEST','Game Lines'],view_scope:['live','PPP','WHP']};errorArgs[13]='Game Lines';
errorArgs[19]='live';errorArgs[20]='PPP';errorArgs[21]='WHP';
check(!api.screen(...errorArgs).includes('exchange-quotes-pending'),'A scoped empty/error quote render does not spin indefinitely');
errorArgs[23]={kalshi:{sport:'NBA',id:'KXNBAGAME-TEST',category:'Game Lines',selected:'live',updating:true,has_rows:false}};
check(api.screen(...errorArgs).includes('exchange-quotes-pending'),'An empty in-progress quote load stays a placeholder');
errorArgs[23].kalshi.has_rows=true;
check(!api.screen(...errorArgs).includes('exchange-quotes-pending')&&api.screen(...errorArgs).includes('market-view-updating'),
  'Usable partial quotes stay visible with an updating label');
function checkQuickFiltering() {
  const events = {}, observers = new Map();
  const window = {location: {hash: '#nfl-game-123'},
    addEventListener(name, handler) { (events[name] ??= []).push(handler); }};
  new Function('window', assets('market_transition.js'))(window);
  const providers = ['market','kalshi','summary'];
  const feedback = Object.fromEntries(providers.map(provider => [provider, {dataset: {quickSelection: 'old'}}]));
  const slots = Object.fromEntries(providers.map(provider => [provider, [{children: []}, {children: []}]]));
  const document = {
    getElementById: id => feedback[id.split('-')[0]],
    querySelectorAll: selector => slots[selector.split('"')[1]],
  };
  class MutationObserver {
    constructor(callback) { this.callback = callback; }
    observe(node) { observers.set(node, this.callback); }
    disconnect() {}
  }
  new Function('window','document','MutationObserver',assets('quick_filter_visibility.js'))(window,document,MutationObserver);
  const move = (hash, event='hashchange') => {
    window.location.hash = hash;
    events[event].forEach(handler => handler());
  };
  const select = (provider, selection) => {
    const target = feedback[provider];
    target.dataset.quickSelection = selection;
    observers.get(target)([{type:'attributes',target}]);
  };
  const key = (hash, selection) => JSON.stringify([hash,selection]);
  const publish = (slot, hash, selection) => {
    const node = {dataset:{quickScope:key(hash,selection)},
      toggleAttribute(name, show) { this.visible = show; }};
    slot.children = [node];
    observers.get(slot)([{type:'childList',target:slot}]);
    return node;
  };

  for (const [provider, oldGame, newGame] of [
    ['market','#nfl-game-123','#nfl-game-456'],
    ['kalshi','#nfl-kalshi-game-KXNFLGAME-OLD','#nfl-kalshi-game-KXNFLGAME-NEW'],
  ]) {
    const [results,status] = slots[provider];
    move(oldGame);
    const old = publish(results,oldGame,'old');
    const notice = publish(status,oldGame,'old');
    check(old.visible && notice.visible,`${provider} shows the matching scan`);
    move(newGame, '_dashprivate_pushstate');
    check(!old.visible && !notice.visible,`${provider} clears old results and status before the server responds`);
    check(!publish(results,oldGame,'old').visible,`${provider} hides a late response for the previous game`);
    select(provider,'new');
    const current = publish(results,newGame,'new');
    check(current.visible,`${provider} reveals the new scan when it arrives`);
    current.dataset.quickScope = key(oldGame,'old');
    observers.get(current)([{type:'attributes',target:current}]);
    check(!current.visible,`${provider} hides a stale response even when Dash reuses the same node`);
    move(oldGame,'popstate');
    check(!current.visible,`${provider} keeps old rows hidden until the saved snapshot also matches`);
    select(provider,'old');
    check(current.visible,`${provider} can restore valid results when returning`);
  }

  const [results,chart] = slots.summary;
  const summary = '#nfl-market-summary', draftkings = '#nfl-draftkings-market';
  move(summary);
  const old = publish(results,summary,'old');
  const oldChart = publish(chart,summary,'old');
  check(old.visible && oldChart.visible,'Market Summary shows the selected game and chart');
  select('summary','new');
  check(!old.visible && !oldChart.visible,'Changing the dropdown clears the previous game immediately');
  check(!publish(results,summary,'old').visible,'A delayed scan cannot restore the previous game');
  const next = publish(results,summary,'new');
  check(next.visible,'The newly selected game can publish results');
  move(draftkings);
  check(!next.visible,'Changing the provider view invalidates the previous scan for the same game ID');
  check(publish(results,draftkings,'new').visible,'DraftKings can publish its own results');
}

checkQuickFiltering();
console.log(`Passed ${checks} scoped UX checks (controlled DOM; browser unverified).`);
