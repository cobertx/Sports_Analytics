/* Apply formatted model values to read-only native quote tables, once per batch. */
(() => {
  const applied=new WeakMap(),rendered=new WeakMap(),pending=new Set();let scheduled=false;
  function schedule(table){
    if(!table)return;pending.add(table);
    if(scheduled)return;scheduled=true;
    requestAnimationFrame(()=>{
      scheduled=false;
      for(const table of pending){
        if(!table.isConnected)continue;
        const source=table.dataset.markup;
        if(rendered.get(table)!==source){
          const scroll=table.querySelector('.summary-table-scroll')?.scrollLeft||0;
          const opened=new Set([...table.querySelectorAll('details[open]')].map(details=>
            (details.closest('tr')?.dataset.summaryRow||'')+'|'+(details.closest('[data-provider]')?.dataset.provider||'')));
          table.innerHTML=source||'';rendered.set(table,source);
          const region=table.querySelector('.summary-table-scroll');if(region)region.scrollLeft=scroll;
          for(const details of table.querySelectorAll('details'))if(opened.has(
            (details.closest('tr')?.dataset.summaryRow||'')+'|'+(details.closest('[data-provider]')?.dataset.provider||'')))details.open=true;
        }
        const body=table.querySelector('tbody'),data=table.dataset.models;
        const previous=applied.get(table);
        if(!body||!data||previous?.body===body&&previous.data===data)continue;
        let values;try{values=JSON.parse(data);}catch{continue;}
        for(const row of body.rows){
          const metrics=values[row.dataset.summaryRow];if(!metrics)continue;
          const cells=row.querySelectorAll('.summary-model-values');
          for(let i=0;i<cells.length;i++)if(cells[i].innerHTML!==metrics[i])cells[i].innerHTML=metrics[i];
        }
        applied.set(table,{body,data});
      }
      pending.clear();
    });
  }
  const observer=new MutationObserver(records=>{
    for(const record of records){
      if(record.target.closest?.('.summary-model-values'))continue;
      const table=record.target.closest?.('.summary-native-table');
      if(table){schedule(table);continue;}
      for(const node of record.addedNodes||[]){
        if(node.matches?.('.summary-native-table'))schedule(node);
        for(const child of node.querySelectorAll?.('.summary-native-table')||[])schedule(child);
      }
    }
  });
  function connect(){const roots=['summary-market-sections','summary-quick-results'].map(id=>document.getElementById(id));if(roots.some(root=>!root))return false;
    for(const root of roots){observer.observe(root,{childList:true,subtree:true,attributes:true,attributeFilter:['data-models','data-markup']});
      for(const table of root.querySelectorAll('.summary-native-table'))schedule(table);}return true;}
  if(!connect()){const bootstrap=new MutationObserver(()=>{if(connect())bootstrap.disconnect();});bootstrap.observe(document.documentElement,{childList:true,subtree:true});}
})();
