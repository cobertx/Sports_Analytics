/* Report native toggles for panels that load on demand. */
document.addEventListener('toggle', event => {
  const panel = event.target;
  if (panel.id !== 'summary-game-probabilities' &&
      !/^(kalshi|polymarket)-(detail-table|rules-panel)$/.test(panel.id || '')) return;
  if (window.dash_clientside?.set_props) {
    window.dash_clientside.set_props(panel.id, {open: panel.open});
  }
}, true);
