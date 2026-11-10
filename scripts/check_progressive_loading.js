/* Independent section guards and native navigation; no remote data or packages. */
const fs=require('node:fs'),path=require('node:path');
const events={},writes=[],no={},window={location:{hash:''},dash_clientside:{no_update:no,
  set_props:(id,props)=>writes.push({id,props})},addEventListener:(name,fn)=>events[name]=fn};
new Function('window',fs.readFileSync(path.join(__dirname,'../sports_analytics/assets/market_transition.js'),'utf8'))(window);
const api=window.dash_clientside.market_transition;
let checks=0;function check(value,label){if(!value)throw Error(label);checks++;}
const hash='#nfl-kalshi-game-KXNFLGAME-TEST';window.location.hash=hash;
check(api.shell(hash,'NFL','kalshi').includes('market-screen-kalshi'),'Shell renders its destination without result inputs');
check(!api.shell(hash,'NFL','kalshi').includes('pending'),'Shell is independent of pending work');
check(api.shell(hash,'NBA','kalshi')==='app-shell','Sport tab changes do not force the old sport route');
check(api.shell(hash,'ODDS_API','kalshi').includes('workspace-screen-odds'),'Top-level workspaces override a retained sport hash');
const game={sport:'NFL',id:'KXNFLGAME-TEST'};
check(api.page_guard('kalshi',hash,'NFL',null,null).includes('pending'),'New game hides incompatible header');
check(!api.page_guard('kalshi',hash,'NFL',game,null).includes('pending'),'Matching header appears without forecasts or quotes');
const view={context:['NFL',game.id,'Game Lines'],view_scope:['live','PPP','WHP']};
const quotes={sport:'NFL',game_id:game.id,category:'Game Lines',selection:'live',rows:[{}]};
check(!api.quote_guard('kalshi',hash,'live','Game Lines',view,quotes).includes('pending'),'Ready quotes do not depend on models');
const context=['NFL',game.id,null,null,null,false,'v1','r1','live','PPP','WHP'];
check(api.model_guard(hash,'live','PPP','WHP',{context,pending:true}).includes('pending'),'Only probabilities wait on a pending model');
check(api.model_guard(hash,'live','PPP','WHP',{context,pending:false})==='','Completed matching model is revealed');
check(api.model_guard(hash,'live','EPA','WHP',{context}).includes('pending'),'Old method probabilities stay hidden');
check(api.quote_guard('kalshi',hash,'saved','Game Lines',view,quotes).includes('pending'),'Old snapshot quotes stay hidden');
check(api.quote_guard('kalshi',hash,'live','Game Lines',view,{...quotes,rows:[],updating:true}).includes('pending'),'Empty pending quotes reserve a placeholder');
check(!api.quote_guard('kalshi',hash,'live','Game Lines',view,{...quotes,rows:[],error:'offline'}).includes('pending'),'Terminal empty/error quotes terminate the placeholder');
check(api.page_guard('kalshi',hash,'NFL',null,{sport:'NFL',games:[]}).includes('unavailable'),'Missing game terminates loading');
const d={key:'fixture',revision:'v1',sport:'NBA',kind:'player',season:2025,phase:'regular'},route='#historical-player';
window.location.hash=route;
const scope={v:2,route,key:d.key,revision:d.revision,sport:'NBA',kind:'player',season:2025,phase:'regular',week:null,
  source_team:'',entity:'001',metric:'pts',window:5,team:null};
const figure={layout:{meta:{analysis_scope:scope,link_ready:true}}};
check(api.history_state(figure,null,d,'001','pts',5,null,route,'NBA','player',2025,'regular',null,null,false,null).ready,
  'Main historical chart is ready while the career is missing');
const careerScope={route,sport:'NBA',kind:'player',phase:'regular',entity:'001',metric:'pts'};
check(api.career_guard({layout:{meta:{career_scope:careerScope,pending:true}}},d,'001','pts',route,'NBA','player').includes('pending'),
  'Career guard remains local while career is pending');
check(!api.career_guard({layout:{meta:{career_scope:careerScope}}},d,'001','pts',route,'NBA','player').includes('pending'),
  'Completed career independently becomes ready');
check(api.career_guard({layout:{meta:{career_scope:careerScope}}},d,'002','pts',route,'NBA','player').includes('pending'),
  'Old player career cannot appear under new player');
window.location.hash=hash;
check(api.career_guard({},d,'001','pts',route,'NBA','player')===no,'Late career visibility cannot replace a newer route');
api.shell(hash,'NFL','kalshi');api.page_guard('kalshi',hash,'NFL',game,null);
window.location.hash='#nfl-polymarket-game-456';events.hashchange();
check(writes.findLast(w=>w.id==='app-shell').props.className.includes('market-screen-polymarket'),'Native route paints without waiting for Dash');
check(writes.findLast(w=>w.id==='polymarket-page').props.className.includes('pending'),'Native route immediately hides the old game');
check(writes.findLast(w=>w.id==='historical-results').props.className.includes('pending'),'Native route invalidates the old history');
check(api.page_guard('kalshi',hash,'NFL',game,null)===no,'Out-of-order quote header guard is rejected');
console.log(`Passed ${checks} progressive loading checks (controlled JavaScript; not browser timings).`);
