/* Reuse fitted mass; instantiate Plotly graphs only in expanded player markets. */
(() => {
  const component=(namespace,type,props)=>({namespace,type,props});
  window.dash_clientside=Object.assign({},window.dash_clientside,{
    player_distribution: {
      render: function(clicks,data) {
        if(!clicks||!data)return window.dash_clientside.no_update;
        const tokens=window.getComputedStyle?.(document.getElementById('app-shell')||document.documentElement);
        const token=(name,fallback)=>tokens?.getPropertyValue(name).trim()||fallback;
        const panel=token('--panel','#111d2e'),muted=token('--muted','#a8b8cc');
        const probability=token('--probability','#75baff'),comparison=token('--comparison','#b7b6ff');
        const style={paper_bgcolor:panel,plot_bgcolor:panel,height:345,autosize:true,
          font:{family:'Segoe UI, Arial, sans-serif',size:12,color:muted},margin:{l:60,r:20,t:25,b:65},
          hoverlabel:{bgcolor:token('--raised','#17253a'),font:{size:13,color:token('--text','#e7edf6')}},
          hovermode:'x unified',showlegend:false,
          xaxis:{title:{text:data.stat},gridcolor:'#223047',...(data.x.length<45?{dtick:1}: {})}};
        const graph=(title,trace,yTitle)=>component('dash_html_components','Div',{
          className:'player-distribution-chart',role:'group','aria-label':`${data.player}: ${title}`,children:[
            component('dash_html_components','H4',{children:title}),
            component('dash_core_components','Graph',{figure:{data:[trace],layout:{...style,
              yaxis:{title:{text:yTitle},tickformat:'.0%',gridcolor:'#223047',range:[0,trace.type==='bar'?Math.min(1,Math.max(...data.mass)*1.12||1):1]} }},
              config:{displaylogo:false,responsive:true},style:{width:'100%'},responsive:true})]});
        const tail=data.below+data.above;
        return [
          component('dash_html_components','P',{className:'chart-note',children:
            `${data.player} · ${data.method} · Mean ${data.mean.toFixed(2)} · SD ${data.sd.toFixed(2)}. `+
            'Each bar is the probability of exactly that integer outcome; cumulative means at or below that value. Integer-line push mass is included.'+
            (tail>1e-10?` The visible range omits ${(100*tail).toFixed(6)}% tail probability; probabilities are not rescaled.`:'')}),
          component('dash_html_components','Div',{className:'player-distribution-grid',children:[
            graph(`${data.method} · Probability distribution`,{type:'bar',x:data.x,y:data.mass,
              marker:{color:probability},name:'Exact outcome',hovertemplate:'Exactly %{x}: %{y:.2%}<extra></extra>'},'Probability of exact outcome'),
            graph(`${data.method} · Cumulative probability`,{type:'scatter',mode:'lines',x:data.x,y:data.cumulative,
              line:{color:comparison,width:2.5,shape:'hv'},fill:'tozeroy',fillcolor:'rgba(183,182,255,.16)',
              name:'At or below',hovertemplate:'At most %{x}: %{y:.2%}<extra></extra>'},'P(outcome ≤ value)') ]})];
      }
    }
  });
  const requested=new WeakMap();
  const lazyRequested=new WeakSet(), lazySections=new WeakMap();
  document.addEventListener('click',event=>{
    const button=event.target.closest?.('button.market-load-more');
    const section=button?.closest('details.sportsbook-market');
    if(section){
      const state=lazySections.get(section)||{pages:0};
      state.pages++;state.more=button;lazySections.set(section,state);
    }
  },true);
  let scheduled=false, active=null, all=true;
  const sections=new Set();
  const relevant='.sportsbook-market,.lazy-market-body,.player-distribution-trigger,.market-load-more';
  const contentObserver=new MutationObserver(records=>{
    for(const record of records){
      if(record.target.closest?.('.dash-graph,.js-plotly-plot'))continue;
      const section=record.target.closest?.('details.sportsbook-market');
      if(section){schedule(section);continue;}
      if([...record.addedNodes||[]].some(node=>node.matches?.(relevant)||node.querySelector?.(relevant)))schedule();
    }
  });
  function currentRoot(){
    const classes=document.getElementById('app-shell')?.className||'';
    if(!classes.includes('market-screen-detail')||classes.includes('market-screen-pending'))return null;
    const provider=['market','kalshi','polymarket'].find(name=>classes.includes('market-screen-'+name+' '));
    return document.getElementById({market:'market-game-detail',kalshi:'kalshi-detail',polymarket:'polymarket-detail'}[provider]);
  }
  function schedule(section) {
    if(section)sections.add(section);else all=true;
    if(scheduled)return;
    scheduled=true;
    requestAnimationFrame(()=>{
      scheduled=false;
      const root=currentRoot();
      if(root!==active){contentObserver.disconnect();active=root;all=true;sections.clear();
        if(active)contentObserver.observe(active,{childList:true,subtree:true});}
      if(!active||!window.dash_clientside?.set_props){sections.clear();return;}
      const roots=all?[active]:[...sections].filter(section=>section.open&&active.contains(section));
      all=false;sections.clear();
      for(const scope of roots){
      const opened=scope===active?'details.sportsbook-market[open] ':'';
      for(const panel of scope.querySelectorAll(opened+'.lazy-market-body')) {
        if(!panel.getClientRects().length)continue;
        const body=panel.querySelector('[data-ready]');
        const button=panel.querySelector('button[id*="lazy-market-trigger"]');
        const more=panel.querySelector('button.market-load-more');
        const section=panel.closest('details.sportsbook-market');
        const state=lazySections.get(section)||{pages:0};
        if(more&&state.more!==more){
          state.more=more;lazySections.set(section,state);
          if(state.pages)window.dash_clientside.set_props(JSON.parse(more.id),{n_clicks:state.pages});
        }
        if(button&&body?.dataset.ready==='false'&&!lazyRequested.has(button)) {
          lazyRequested.add(button);
          window.dash_clientside.set_props(JSON.parse(button.id),{n_clicks:1});
        }
      }
      for(const button of scope.querySelectorAll(opened+'.player-distribution-trigger')) {
        if(!button.parentElement.getClientRects().length)continue;
        const state=requested.get(button);
        const ready=Boolean(button.parentElement.querySelector('.dash-graph'));
        if(state&&(!state.ready||ready)) {
          if(ready)state.ready=true;
          continue;
        }
        // Applying criteria can replace children while retaining the native
        // accordion/button node. Restore that chart once after replacement.
        const clicks=(state?.clicks||0)+1;
        requested.set(button,{ready:false,clicks});
        window.dash_clientside.set_props(JSON.parse(button.id),{n_clicks:clicks});
      }
      }
    });
  }
  document.addEventListener('toggle',event=>{
    if(event.target.matches?.('details.sportsbook-market'))schedule(event.target);
  },true);
  window.addEventListener('hashchange',()=>schedule());
  const visibilityObserver=new MutationObserver(()=>schedule());
  const bootstrap=new MutationObserver(connect);
  function connect(){
    const shell=document.getElementById('app-shell');
    if(!shell)return;
    bootstrap.disconnect();visibilityObserver.observe(shell,{attributes:true,attributeFilter:['class']});schedule();
  }
  if(document.getElementById('app-shell'))connect();
  else bootstrap.observe(document.documentElement,{childList:true,subtree:true});
})();
