/* Real client renderer and accordion events; no browser, HTTP or model calls. */
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../sports_analytics/assets/player_distribution.js'),'utf8');
let checks=0;
const check=(ok,label)=>{if(!ok)throw new Error(label);checks++;};
const writes=[],events={},noUpdate={};
let buttons=[],lazyPanels=[],observer,visibilityObserver,contentObserver;
const shell={className:'app-shell market-screen market-screen-market market-screen-detail'};
let scans=0;
const root={contains:()=>true,querySelectorAll:selector=>{scans++;return selector.includes('.lazy-market-body')?lazyPanels:buttons;}};
const window={dash_clientside:{no_update:noUpdate,set_props:(id,props)=>writes.push({id,props})},
  addEventListener:(name,handler)=>events[name]=handler};
const document={documentElement:{},getElementById:id=>id==='app-shell'?shell:id==='market-game-detail'?root:null,
  addEventListener:(name,handler)=>events[name]=handler};
let observerIndex=0;
function MutationObserver(handler){if(observerIndex++===0){contentObserver=handler;observer=()=>handler([{target:{closest:()=>null},addedNodes:[{matches:()=>true}]}]);}
  else if(observerIndex===2)visibilityObserver=handler;
  this.observe=()=>{};this.disconnect=()=>{};}
new Function('window','document','MutationObserver','requestAnimationFrame',source)(window,document,MutationObserver,fn=>fn());
const render=window.dash_clientside.player_distribution.render;
check(render(0,{})===noUpdate,'Closed markets never construct charts');
check(writes.length===0,'No request without an open market');
const button=key=>({id:JSON.stringify({type:'player-distribution-trigger',key}),ready:false,
  parentElement:{querySelector:()=>buttons.find(b=>b.id.includes(key))?.ready?{}:null,
    getClientRects:()=>buttons.find(b=>b.id.includes(key))?.hidden?[]:[{}]}});
buttons=[button('first')];
events.toggle({target:{open:true,matches:()=>true,querySelectorAll:root.querySelectorAll}});events.toggle({target:{open:true,matches:()=>true,querySelectorAll:root.querySelectorAll}});observer();
check(writes.length===1&&writes[0].id.key==='first','Request an open accordion once');
buttons[0].ready=true;observer();buttons[0].ready=false;observer();observer();
check(writes.length===2,'Restore a chart after criteria rerender the existing market');
check(writes[1].props.n_clicks===2,'Restoring uses a fresh client callback event');
buttons=[...buttons,button('changed-method')];
observer();
check(writes.length===3,'New selected methods request their own current distribution');
const hidden=button('other-page');hidden.hidden=true;buttons.push(hidden);observer();
check(writes.length===3,'Hidden retained market pages do not mount charts');
hidden.hidden=false;visibilityObserver();
check(writes.length===4,'Ready page visibility reveals charts even after the forecast was initially hidden');
events.hashchange();
check(writes.length===4,'Repeated native navigation does not duplicate pending charts');
for(const method of ['WHP','UO','OAM','PGE']){
  const value={player:'Player A',stat:'Points',method,x:[5,6,7],mass:[.2,.3,.5],cumulative:[.2,.5,1],mean:6.3,sd:.8,below:0,above:0};
  const output=render(1,value);
  const plots=output[1].props.children.map(c=>c.props.children[1]);
  check(plots.length===2,'Build PMF and CDF together');
  check(plots[0].type==='Graph'&&plots[0].props.figure.data[0].type==='bar','Discrete outcome probability is a bar chart');
  check(plots[1].props.figure.data[0].line.shape==='hv','CDF uses integer steps');
  check(JSON.stringify(plots[0].props.figure.data[0].y)===JSON.stringify(value.mass),'Never change fitted mass');
  check(JSON.stringify(plots[1].props.figure.data[0].y)===JSON.stringify(value.cumulative),'Never condition or rescale cumulative probabilities');
  check(output[0].props.children.includes(method),'Identify the selected forecast method');
  check(plots.every(p=>p.props.responsive&&p.props.config.responsive),'Responsive plot sizing');
}
const tail=render(1,{player:'Player A',stat:'Yards',method:'WHP',x:[1],mass:[.9],cumulative:[.95],mean:1,sd:1,below:.05,above:.05});
check(tail[0].props.children.includes('not rescaled'),'Label omitted analytic tails');
window.getComputedStyle=()=>({getPropertyValue:name=>({'--probability':'#75baff','--comparison':'#b7b6ff','--panel':'#111d2e'}[name]||'')});
const themed=render(1,{player:'Player A',stat:'Points',method:'WHP',x:[1],mass:[1],cumulative:[1],mean:1,sd:0,below:0,above:0})[1].props.children;
check(themed[0].props.children[1].props.figure.data[0].marker.color==='#75baff','PMF uses shared probability token');
check(themed[1].props.children[1].props.figure.data[0].line.color==='#b7b6ff','CDF uses distinct comparison token');
check(themed.every(p=>p.props.role==='group'&&p.props['aria-label'].includes('Player A')),'Both charts have accessible player context');
const section={},body={dataset:{ready:'false'}};
const makeControl=type=>({id:JSON.stringify({type,view:'first-view',section:2})});
let trigger=makeControl('lazy-market-trigger'),more=makeControl('lazy-market-more');
lazyPanels=[{getClientRects:()=>[{}],closest:()=>section,querySelector:selector=>
  selector==='[data-ready]'?body:selector.includes('lazy-market-trigger')?trigger:more}];
const prior=writes.length;events.toggle({target:{open:true,matches:()=>true,querySelectorAll:root.querySelectorAll}});events.toggle({target:{open:true,matches:()=>true,querySelectorAll:root.querySelectorAll}});
check(writes.length===prior+1&&writes.at(-1).id.type==='lazy-market-trigger','An expanded lazy section requests its first rows once');
body.dataset.ready='true';events.toggle({target:{open:true,matches:()=>true,querySelectorAll:root.querySelectorAll}});
check(writes.length===prior+1,'Ready sections do not make duplicate requests');
events.click({target:{closest:()=>({...more,closest:()=>section})}});
more=makeControl('lazy-market-more');more.id=JSON.stringify({type:'lazy-market-more',view:'updated-view',section:2});
events.toggle({target:{open:true,matches:()=>true,querySelectorAll:root.querySelectorAll}});
check(writes.at(-1).id.view==='updated-view'&&writes.at(-1).props.n_clicks===1,'A replacement quote view restores the opened page count');
const beforeScans=scans;
contentObserver([{target:{closest:()=>null},addedNodes:[{matches:()=>false,querySelector:()=>null}]}]);
check(scans===beforeScans,'Unrelated progress markup never scans market panels');
contentObserver([{target:{closest:selector=>selector.includes('.dash-graph')?{}:null},addedNodes:[]}]);
check(scans===beforeScans,'Plotly internal mutations never rescan its market');
shell.className='app-shell workspace-screen workspace-screen-historical';visibilityObserver();
observer();events.hashchange();
check(scans===beforeScans,'Hidden market pages perform no scans');
shell.className='app-shell market-screen market-screen-market market-screen-detail';visibilityObserver();
check(scans>beforeScans,'Returning to a market resumes its own pending mounts');
console.log(`Passed ${checks} player distribution and lazy row checks.`);
