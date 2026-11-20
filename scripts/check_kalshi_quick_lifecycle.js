/* Exercise the actual registered client callback with pending/terminal jobs. */
const fs=require('node:fs'),config=JSON.parse(fs.readFileSync(0,'utf8'));
const window={location:{hash:'#nfl-kalshi-game-KXNFLGAME-TEST'},dash_clientside:{no_update:null},addEventListener(){}};
new Function('window',fs.readFileSync(require('node:path').join(__dirname,'../app/assets/js/market_transition.js'),'utf8'))(window);
for(const source of config.inline)new Function('window',source)(window);
const fn=window.dash_clientside[config.client.namespace][config.client.function_name];
let checks=0;
const check=(args,expected,message)=>{
  if(JSON.stringify(fn(...args))!==JSON.stringify(expected))throw Error(message);
  checks++;
};
const game={sport:'NFL',id:'KXNFLGAME-TEST'},scan={job:'job1',sequence:1,context:['NFL',game.id]};
const args=['kalshi','Mispricing Filter',scan,game,null];
check(args,[false,true],'Start polling only the submitted job');
for(const status of ['complete','error','changed']){
  const state={job:'job1',sequence:1,status};
  check([...args.slice(0,4),state],[true,false],'Terminal jobs stop polling and unlock retries');
  check([...args.slice(0,3),{...game,title:'New discovery metadata'},state],[true,false],
    'Discovery refresh must not restart a terminal job');
}
check([...args.slice(0,4),{job:'old',sequence:1,status:'complete'}],[false,true],
  'A late completion cannot stop the current job');
check([...args.slice(0,2),{...scan,sequence:2},game,{job:'job1',sequence:1,status:'complete'}],[false,true],
  'An explicit retry restarts polling');
check(['market',...args.slice(1)],[true,false],'Leaving Kalshi stops view polling');
check(['kalshi','Game Lines',...args.slice(2)],[true,false],'Leaving Quick Filtering stops view polling');
check([...args.slice(0,3),{...game,id:'another'},null],[true,false],'Never poll another game');
const kickoff=window.dash_clientside[config.kickoff.namespace][config.kickoff.function_name];
const [request,scope]=kickoff(2,'Mispricing Filter',null);
const hash=window.location.hash;
if(request.location!==hash||request.sequence!==2||request.selection!=='live'||scope!==JSON.stringify([hash,'live'])){
  throw Error('Click must immediately capture the browser game and the live snapshot default');
}
checks++;
check([...args,request,false,hash,'live'],[true,true],'Submitting shows local feedback and prevents duplicate clicks');
check([...args,request,true,hash,'live'],[true,false],'An earlier scan cannot poll for a newer click');
const accepted={...scan,sequence:2,scope:['NFL',game.id,'live']};
check(['kalshi','Mispricing Filter',accepted,null,null,request,true,hash,null],[false,true],
  'The visible route can poll even while its matchup store catches up');
check(['kalshi','Mispricing Filter',{request,error:'missing inputs'},game,null,request,true,hash,'live'],[true,false],
  'Rejected submissions stop polling and unlock retries');
console.log(`Passed ${checks} Quick Filtering lifecycle checks.`);
