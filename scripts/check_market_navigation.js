/* Pure event/DOM harness: no browser, source requests or installed JS packages. */
function checkMarketNavigation(source) {
  let checks = 0;
  const check = (condition, message) => { if (!condition) throw new Error(message); checks++; };
  const documentEvents = {}, windowEvents = {}, writes = [], scrolls = [];
  const location = {hash: '#nfl-game-123'};
  const nodes = {};
  const panel = {getClientRects: () => [{}]};
  nodes['market-game-detail'] = panel;
  let observer;
  const document = {
    documentElement: {},
    getElementById: id => nodes[id],
    addEventListener: (name, callback) => { documentEvents[name] = callback; }
  };
  const noUpdate = {};
  const window = {
    dash_clientside: {no_update: noUpdate, set_props: (id, props) => writes.push({id, props})},
    addEventListener: (name, callback) => { windowEvents[name] = callback; }
  };
  function MutationObserver(callback) { observer = callback; this.observe = () => {}; }
  const style = element => ({display: element.hidden ? 'none' : 'block', visibility: element.loading ? 'hidden' : 'visible'});
  new Function('window', 'document', 'location', 'MutationObserver', 'getComputedStyle', 'requestAnimationFrame', source)(
    window, document, location, MutationObserver, style, callback => callback());
  const remember = window.dash_clientside.market_navigation.remember;
  const sectionId = 'market-section-abc';
  const element = id => ({id, tagName: 'DETAILS', open: false, isConnected: true,
    closest: () => null, scrollIntoView: () => scrolls.push(id)});
  const anchor = {closest: () => ({id: sectionId})};
  const click = overrides => documentEvents.click({target: {closest: () => anchor}, button: 0, ...overrides});
  const change = (oldHash, newHash) => {
    location.hash = newHash;
    windowEvents.hashchange({oldURL: 'https://local.invalid/' + oldHash});
  };

  let saved = remember(location.hash, 'saved-snapshot', 'Receiving', {NBA: {route: '#nba-game-456'}});
  check(saved.NFL.route === location.hash && saved.NFL.snapshot === 'saved-snapshot', 'Remember exact game and snapshot');
  check(saved.NFL.category === 'Receiving' && saved.NBA.route === '#nba-game-456', 'Keep category and separate sport context');
  check(remember(location.hash, 'saved-snapshot', 'Receiving', saved) === noUpdate, 'Avoid redundant store updates');
  click({ctrlKey: true});
  check(writes.length === 0, 'Do not capture new-tab clicks');
  click();
  check(writes.length === 1 && writes[0].id === 'market-return', 'Capture header link before navigation');
  saved = writes[0].props.data;
  check(saved.NFL.section === sectionId, 'Capture exact player/market section');
  change('#nfl-game-123', '#historical-player?sport=NFL&name=Jerry+Jeudy');
  check(remember(location.hash, null, null, saved) === noUpdate, 'Historical callbacks preserve the market origin');
  change('#historical-player?sport=NFL&name=Jerry+Jeudy', '#nfl-game-123');
  check(scrolls.length === 0, 'Wait for market content to exist');
  const market = element(sectionId);market.loading = true;nodes[sectionId] = market;
  observer();
  check(scrolls.length === 0, 'Wait for loading content to become visible');
  market.loading = false;observer();
  check(market.open && scrolls.length === 1 && scrolls[0] === sectionId, 'Open and scroll to origin market');
  observer();
  check(scrolls.length === 1, 'Do not repeatedly scroll an unchanged market node');
  const replacement = element(sectionId);nodes[sectionId] = replacement;observer();
  check(replacement.open && scrolls.length === 2, 'Restore after Dash replaces the market node');
  replacement.loading = true;observer();replacement.loading = false;observer();
  check(scrolls.length === 3, 'Restore once more when an existing node finishes reloading');
  windowEvents.wheel();nodes[sectionId] = element(sectionId);observer();
  check(scrolls.length === 3, 'Stop restoring when the user scrolls');
  saved = remember(location.hash, 'different-snapshot', 'Receiving', saved);
  check(saved.NFL.section === null, 'Do not reuse a section from a different snapshot');
  check(remember('#nfl-games', null, null, saved) === noUpdate, 'Schedule navigation preserves the last game');
  check(remember('#nba-game-999', 'nba-snapshot', 'Points', saved).NBA.route === '#nba-game-999', 'Capture NBA categories independently');
  const beforeHistory=scrolls.length;
  nodes['historical-performance-panel']=element('historical-performance-panel');
  change('#nfl-game-123','#historical-player?sport=NFL&name=Jerry+Jeudy');
  check(scrolls.length===beforeHistory+1&&scrolls.at(-1)==='historical-performance-panel','Open linked player history at the performance chart');
  return checks;
}

if (typeof module !== 'undefined' && module.exports) {
  const fs = require('node:fs');
  const path = require('node:path');
  const source = fs.readFileSync(path.join(__dirname, '../sports_analytics/assets/market_navigation.js'), 'utf8');
  console.log(`Passed ${checkMarketNavigation(source)} market navigation checks.`);
}
