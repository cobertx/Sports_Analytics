/* Offline native accordion intent survives metadata rebuilds and is scoped. */
const fs = require('node:fs'), path = require('node:path');
let click, observe, frames = [], sections = [], checks = 0;
const check = (ok, message) => { if (!ok) throw Error(message); checks++; };
const document = {documentElement: {}, addEventListener: (name, handler) => { click = handler; },
  querySelectorAll: () => sections};
function MutationObserver(handler) { observe = handler; this.observe = () => {}; }
new Function('document', 'MutationObserver', 'requestAnimationFrame',
  fs.readFileSync(path.join(__dirname, '../sports_analytics/assets/exchange_accordions.js'), 'utf8'))(
    document, MutationObserver, fn => frames.push(fn));
const flush = () => { observe(); while (frames.length) frames.shift()(); };
const section = (scope, open = false) => ({id: 'spread', dataset: {marketScope: scope}, open, matches: () => true});
const toggle = node => { click({target: {closest: () => ({parentElement: node})}}); node.open = !node.open; };
let spread = section('Kalshi|NFL|game1|Game Lines|live'); sections = [spread];
toggle(spread); flush(); check(spread.open, 'Alternative Spreads opens immediately');
spread.open = false; flush(); check(spread.open, 'A refresh cannot close the selected market');
spread = section('Kalshi|NFL|game1|Game Lines|live'); sections = [spread]; flush();
check(spread.open, 'A full metadata rebuild restores the open selection');
toggle(spread); flush(); spread.open = true; flush();
check(!spread.open, 'An intentionally closed market stays closed during refresh');
for (const scope of ['Kalshi|NFL|game2|Game Lines|live', 'Kalshi|NBA|game1|Game Lines|live',
  'Kalshi|NFL|game1|Player Props|live', 'Kalshi|NFL|game1|Game Lines|saved']) {
  const other = section(scope, true); sections = [other]; flush();
  check(other.open, 'Other game, sport, category and snapshot retain their defaults');
}
let poly = section('Polymarket|NBA|100|Game Lines|live'); sections = [poly];
toggle(poly); poly.open = false; flush();
check(poly.open, 'Polymarket market stays open through a quote refresh');
poly = section('Polymarket|NBA|100|Game Lines|live'); sections = [poly]; flush();
check(poly.open, 'Polymarket market stays open through a metadata rebuild');
const otherProvider = section('Kalshi|NBA|100|Game Lines|live'); sections = [otherProvider]; flush();
check(!otherProvider.open, 'Polymarket choices do not affect Kalshi');
console.log(`Passed ${checks} exchange accordion checks.`);
