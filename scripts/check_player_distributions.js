/* Real client renderer and accordion events; no browser, HTTP or model calls. */
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../sports_analytics/assets/player_distribution.js'),'utf8');
let checks=0;
const check=(ok,label)=>{if(!ok)throw new Error(label);checks++;};
const writes=[],events={},noUpdate={};
let buttons=[],observer;
const window={dash_clientside:{no_update:noUpdate,set_props:(id,props)=>writes.push({id,props})},
  addEventListener:(name,handler)=>events[name]=handler};
const document={documentElement:{},querySelectorAll:()=>buttons,addEventListener:(name,handler)=>events[name]=handler};
function MutationObserver(handler){observer=handler;this.observe=()=>{};}
new Function('window','document','MutationObserver','requestAnimationFrame',source)(window,document,MutationObserver,fn=>fn());
const render=window.dash_clientside.player_distribution.render;
check(render(0,{})===noUpdate,'Closed markets never construct charts');
check(writes.length===0,'No request without an open market');
const button=key=>({id:JSON.stringify({type:'player-distribution-trigger',key}),ready:false,
  parentElement:{querySelector:()=>buttons.find(b=>b.id.includes(key))?.ready?{}:null,
    getClientRects:()=>buttons.find(b=>b.id.includes(key))?.hidden?[]:[{}]}});
buttons=[button('first')];
events.toggle();events.toggle();observer();
check(writes.length===1&&writes[0].id.key==='first','Request an open accordion once');
buttons[0].ready=true;observer();buttons[0].ready=false;observer();observer();
check(writes.length===2,'Restore a chart after criteria rerender the existing market');
check(writes[1].props.n_clicks===2,'Restoring uses a fresh client callback event');
buttons=[...buttons,button('changed-method')];
observer();
check(writes.length===3,'New selected methods request their own current distribution');
const hidden=button('other-page');hidden.hidden=true;buttons.push(hidden);observer();
check(writes.length===3,'Hidden retained market pages do not mount charts');
hidden.hidden=false;observer([{type:'attributes',target:{id:'app-shell'}}]);
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
console.log(`Passed ${checks} player distribution checks.`);
