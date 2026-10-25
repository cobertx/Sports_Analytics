/* Report native details toggles so large tables/rules load only when opened. */
document.addEventListener('toggle', event => {
  const panel = event.target;
  if (!/^(kalshi|polymarket)-(detail-table|rules-panel)$/.test(panel.id || '')) return;
  if (window.dash_clientside?.set_props) {
    window.dash_clientside.set_props(panel.id, {open: panel.open});
  }
}, true);
