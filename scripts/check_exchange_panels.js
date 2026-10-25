/* Native toggle bridge for panels that are deliberately empty while closed. */
function checkExchangePanels(source) {
  let handler, checks = 0;
  const writes = [];
  const check = (ok, message) => { if (!ok) throw new Error(message); checks++; };
  const document = {addEventListener: (event, callback, capture) => {
    check(event === 'toggle' && capture === true, 'Capture non-bubbling native details toggles');
    handler = callback;
  }};
  const window = {dash_clientside: {set_props: (id, props) => writes.push({id, props})}};
  new Function('document', 'window', source)(document, window);
  for (const prefix of ['kalshi', 'polymarket']) {
    for (const panel of ['detail-table', 'rules-panel']) {
      const id = `${prefix}-${panel}`;
      handler({target: {id, open: true}});
      check(writes.at(-1).id === id && writes.at(-1).props.open === true, 'Opening loads the requested panel');
      handler({target: {id, open: false}});
      check(writes.at(-1).props.open === false, 'Closing stops panel refreshes');
    }
  }
  const count = writes.length;
  handler({target: {id: 'market-section-abc', open: true}});
  handler({target: {open: true}});
  check(writes.length === count, 'Other market accordions never trigger a server callback');
  delete window.dash_clientside;
  handler({target: {id: 'kalshi-rules-panel', open: true}});
  check(writes.length === count, 'Startup before Dash initialization is safe');
  return checks;
}

if (typeof module !== 'undefined' && module.exports) {
  const fs = require('node:fs');
  const path = require('node:path');
  const source = fs.readFileSync(path.join(__dirname, '../sports_analytics/assets/exchange_panels.js'), 'utf8');
  console.log(`Passed ${checkExchangePanels(source)} exchange panel checks.`);
}
