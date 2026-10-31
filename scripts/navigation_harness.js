/* Execute the app's registered client callbacks, without a browser or packages. */
const fs=require('node:fs');
function run(config){
  const no={_dash_no_update:'_dash_no_update'},window={location:{hash:''},
    dash_clientside:{no_update:no},addEventListener(){}};
  new Function('window',config.asset)(window);
  for(const script of config.inline)new Function('window',script)(window);
  const callback=fragment=>{
    const entry=config.clients.find(item=>item.output.includes(fragment));
    if(!entry)throw Error('Missing client callback: '+fragment);
    return window.dash_clientside[entry.clientside_function.namespace][entry.clientside_function.function_name];
  };
  let checks=0;
  for(const item of config.references||[]){
    const value=callback(item.output)(...item.args);
    if(JSON.stringify(value)!==JSON.stringify(item.expected))throw Error('Client/reference mismatch: '+item.output+' '+JSON.stringify(item.args));
    checks++;
  }
  const scope=callback('nav-historical.data'),select=window.dash_clientside.market_transition.select;
  const previous=Array(config.pages.length*2+1).fill(null),values={
    'market-url':'#nfl-game-123','sport':'NFL','workspace-page':'market','market-category':'Game'};
  function advance(step){
    const changed=[];
    if(step.hash!==undefined&&step.hash!==values['market-url']){
      window.location.hash=step.hash;values['market-url']=step.hash;changed.push('market-url');
      const selected=select(step.hash,values.sport,values['workspace-page']);
      for(const [index,key] of ['sport','workspace-page'].entries())if(selected[index]!==no){values[key]=selected[index];changed.push(key);}
    }
    for(const [key,value] of Object.entries(step.set||{}))if(values[key]!==value){values[key]=value;changed.push(key);}
    const result=scope(values['market-url'],values.sport,values['workspace-page'],...previous),scopeChanges=[];
    result.forEach((value,index)=>{if(value===no)return;previous[index]=value;
      if(index<config.pages.length*2){const key='nav-'+config.pages[Math.floor(index/2)]+(index%2?'-active':'');values[key]=value;scopeChanges.push(key);}});
    return {label:step.label,page:values['workspace-page'],updates:[...changed,...scopeChanges],values:{...values},
      before:config.before.filter(item=>item.inputs.some(input=>changed.includes(input.id))).map(item=>item.name),
      after:config.after.filter(item=>item.inputs.some(input=>[...changed,...scopeChanges].includes(input.id))).map(item=>item.name)};
  }
  advance({});
  const steps=(config.steps||[]).map(advance);
  return {checks,steps};
}
if(require.main===module)process.stdout.write(JSON.stringify(run(JSON.parse(fs.readFileSync(0,'utf8')))));
module.exports={run};
