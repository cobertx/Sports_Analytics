/* Real navigation assets in a controlled DOM; no requests or JS dependencies. */
const fs=require('node:fs'),path=require('node:path');
const assets=name=>fs.readFileSync(path.join(__dirname,'../sports_analytics/assets',name),'utf8');
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
const errorArgs=['#nba-kalshi-game-KXNBAGAME-TEST','NBA',null,{sport:'NBA',id:'KXNBAGAME-TEST'},null,null,null,null,'kalshi'];
errorArgs[12]={context:['NBA','KXNBAGAME-TEST','Game Lines'],view_scope:['live','PPP','WHP']};errorArgs[13]='Game Lines';
errorArgs[19]='live';errorArgs[20]='PPP';errorArgs[21]='WHP';
check(!api.screen(...errorArgs).includes('exchange-quotes-pending'),'A scoped empty/error quote render does not spin indefinitely');
errorArgs[23]={kalshi:{sport:'NBA',id:'KXNBAGAME-TEST',category:'Game Lines',selected:'live',updating:true,has_rows:false}};
check(api.screen(...errorArgs).includes('exchange-quotes-pending'),'An empty in-progress quote load stays a placeholder');
errorArgs[23].kalshi.has_rows=true;
check(!api.screen(...errorArgs).includes('exchange-quotes-pending')&&api.screen(...errorArgs).includes('market-view-updating'),
  'Usable partial quotes stay visible with an updating label');
console.log(`Passed ${checks} scoped UX checks (controlled DOM; browser unverified).`);
