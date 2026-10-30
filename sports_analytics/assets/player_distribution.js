/* Reuse fitted mass; instantiate Plotly graphs only in expanded player markets. */
(() => {
  const component=(namespace,type,props)=>({namespace,type,props});
  window.dash_clientside=Object.assign({},window.dash_clientside,{
    player_distribution: {
      render: function(clicks,data) {
        if(!clicks||!data)return window.dash_clientside.no_update;
        const style={template:'plotly_dark',paper_bgcolor:'#111d2e',plot_bgcolor:'#111d2e',height:345,
          font:{family:'Segoe UI, sans-serif',color:'#b8c8dc'},margin:{l:60,r:20,t:25,b:65},
          hovermode:'x unified',showlegend:false,
          xaxis:{title:{text:data.stat},gridcolor:'#223047',...(data.x.length<45?{dtick:1}: {})}};
        const graph=(title,trace,yTitle)=>component('dash_html_components','Div',{
          className:'player-distribution-chart',children:[
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
              marker:{color:'#57dbc4'},name:'Exact outcome',hovertemplate:'Exactly %{x}: %{y:.2%}<extra></extra>'},'Probability of exact outcome'),
            graph(`${data.method} · Cumulative probability`,{type:'scatter',mode:'lines',x:data.x,y:data.cumulative,
              line:{color:'#8197ff',width:2.5,shape:'hv'},fill:'tozeroy',fillcolor:'rgba(129,151,255,.16)',
              name:'At or below',hovertemplate:'At most %{x}: %{y:.2%}<extra></extra>'},'P(outcome ≤ value)') ]})];
      }
    }
  });
  const requested=new WeakMap();
  let scheduled=false;
  function schedule() {
    if(scheduled)return;
    scheduled=true;
    requestAnimationFrame(()=>{
      scheduled=false;
      if(!window.dash_clientside?.set_props)return;
      for(const button of document.querySelectorAll('details.sportsbook-market[open] .player-distribution-trigger')) {
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
    });
  }
  document.addEventListener('toggle',schedule,true);
  window.addEventListener('hashchange',schedule);
  new MutationObserver((records=[])=>{
    if(!records.length||records.some(r=>r.type==='childList'||['app-shell','market-page'].includes(r.target.id)))schedule();
  }).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['class','style']});
  schedule();
})();
