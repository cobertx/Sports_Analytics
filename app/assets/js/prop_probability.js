/* Delegate clicks for Dash rows and native quote tables alike. */
(() => {
  const pending = new WeakMap();
  let sequence = 0;
  document.addEventListener('click', event => {
    const row = event.target.closest?.('[data-prop-chart]');
    if (!row || !window.dash_clientside?.set_props) return;
    const control = event.target.closest('button,a,input,select,textarea,label,details,[role="button"]');
    if (control && row.contains(control) && !control.matches('.prop-chart-button')) return;
    const selection = JSON.parse(row.dataset.propChart);
    const result = document.querySelector(`[data-prop-chart-result="${selection.view}"]`);
    if (!result) return;

    const click = ++sequence;
    pending.get(result)?.disconnect();
    result.setAttribute('aria-busy', 'true');
    const observer = new MutationObserver(() => {
      const display = result.firstElementChild;
      if (display && display.dataset.propChartClick !== String(click)) return;
      observer.disconnect();
      pending.delete(result);
      result.removeAttribute('aria-busy');
      if (!display?.getClientRects().length) return;
      display.focus({preventScroll:true});
      display.scrollIntoView({block:'start', behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
    });
    pending.set(result, observer);
    observer.observe(result, {childList:true, subtree:true, attributes:true, attributeFilter:['data-prop-chart-click']});
    const request = ['summary','kalshi'].includes(selection.view) ? `${selection.view}-prop-request` : {type:'prop-chart-request', view:selection.view};
    window.dash_clientside.set_props(request, {'data-selection': {...selection, click}});
  });
})();
